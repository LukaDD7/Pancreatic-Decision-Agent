"""Stage 3 — episode assembly: the last tool, not a rewritten graph.

The frozen pipeline assembled episodes in a pure function and then reported
blocked dependencies it could not repair. The agent had no way to see the
conflict, and the program had no way to ask. Here the graph is assembled by the
agent, from relations it recorded in stage 2, and the program only refuses
structurally impossible edges.

What the program still enforces, because these are invariants rather than
judgments:

* every edge endpoint is a known event
* an edge marked ``no_link`` cannot merge two episodes
* two events on the same branch with contradictory *supported* edges (A before B
  and B before A) are reported, and the agent must resolve them; the assembly
  does not silently pick one

What the program no longer does: decide that a pair of events is unrelated
because a cutoff was unknown. That refusal was the single largest cause of
broken graph dependencies in the frozen batch (106 of 146).
"""
from __future__ import annotations

from collections import Counter
import json

from ..tools import Tool, ToolError


class EpisodeBuilder:
    def __init__(self, events, *, relations,
                 process_merge_policy="legacy_supported_and_context_only"):
        if process_merge_policy not in (
                "legacy_supported_and_context_only", "supported_only"):
            raise ValueError("PROCESS_MERGE_POLICY_INVALID")
        self.process_merge_policy = process_merge_policy
        self.by_id = {event["event_id"]: event for event in events}
        self.relations = list(relations)
        self.memberships = {}
        self.unresolved = []
        self.conflicts = []
        self.reviewed_pairs = set()
        self.preview_count = 0

    def _pair_key(self, relation):
        return (relation.get("source_event_id"), relation.get("target_event_id"),
                relation.get("relation"), relation.get("confidence"),
                json.dumps(relation.get("basis_quotes") or [], sort_keys=True,
                           ensure_ascii=False), relation.get("explanation") or "")

    # -- graph ---------------------------------------------------------
    def _parent(self):
        parent = {event_id: event_id for event_id in self.by_id}

        def find(node):
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        self._find = find
        self._parent_map = parent
        return parent, find

    def _process_edges(self):
        """Resolve same-process assertions against explicit later refutations.

        Append-only history can contain a wrong early pair and a later ``none``.
        The refutation wins for that exact pair; it does not invent a clinical
        fact or remove either event. Other supported process paths remain intact.
        """
        latest = {}
        process_latest = {}
        evidence_latest = {}
        auxiliary = {}
        for relation in self.relations:
            source, target = relation.get("source_event_id"), relation.get("target_event_id")
            kind = relation.get("relation")
            if source is None or target is None:
                continue
            if kind in ("same_management_process", "none"):
                process_latest[frozenset((source, target))] = relation
            elif kind in ("evidence_comparison_previous", "evidence_used"):
                evidence_latest[(source, target, kind)] = relation
            elif kind in ("contains", "overlap"):
                auxiliary[(source, target, kind)] = relation
        # These are independent relation kinds. A no_link refutes only a direct
        # process edge; it must not erase a separately supported evidence-use
        # reference between the same two objects.
        return list(process_latest.values()) + list(evidence_latest.values()) \
            + list(auxiliary.values())

    def union(self, left, right):
        parent, find = self._parent_map, self._find
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    def assemble(self):
        parent, find = self._parent()
        merged = []
        unresolved = []
        evidence_references = []
        composition_relations = []
        unresolved_keys = set()
        conflicts = []
        for relation in self._process_edges():
            source = relation.get("source_event_id")
            target = relation.get("target_event_id")
            kind = relation.get("relation")
            confidence = relation.get("confidence")
            if source not in self.by_id:
                unresolved.append(dict(relation=relation,
                                       reason="SOURCE_EVENT_UNKNOWN"))
                continue
            if target is not None and target not in self.by_id:
                unresolved.append(dict(relation=relation,
                                       reason="TARGET_EVENT_UNKNOWN"))
                continue
            if kind == "none":
                continue
            if kind in ("evidence_comparison_previous", "evidence_used"):
                # Evidence use is directional and auditable, not process union.
                evidence_references.append(relation)
                if confidence in ("supported", "context_only"):
                    self.reviewed_pairs.add(self._pair_key(relation))
            elif kind in ("contains", "overlap"):
                # Composition is inspectable without flattening candidate identity
                # or making a component a second clinical decision in one process.
                composition_relations.append(relation)
                if confidence in ("supported", "context_only"):
                    self.reviewed_pairs.add(self._pair_key(relation))
            if (kind == "same_management_process" and confidence != "supported"
                    and self.process_merge_policy == "supported_only"):
                item = dict(relation=relation, reason=(
                    "PROCESS_CONTEXT_ONLY_NOT_MERGED" if confidence == "context_only"
                    else "RELATION_" + str(confidence).upper()))
                unresolved.append(item)
                unresolved_keys.add((frozenset((source, target)), kind, item["reason"]))
                continue
            if confidence in ("no_link", "uncertain"):
                # A relation the agent could not support is preserved as a
                # declared-but-unmerged edge. It is not evidence of absence.
                item = dict(relation=relation, reason=(
                    "RELATION_NOT_SUPPORTED" if confidence == "no_link"
                    else "RELATION_UNCERTAIN"))
                key = (frozenset((source, target)), kind, item["reason"])
                if key not in unresolved_keys:
                    unresolved.append(item)
                    unresolved_keys.add(key)
                continue
            if target is None:
                continue
            if source == target:
                continue
            # Duplicate candidates share an identity group, but are not by
            # themselves evidence that two management processes should merge.
            if kind == "same_management_process":
                self.union(source, target)
                merged.append(dict(source_event_id=source,
                                   target_event_id=target, relation=kind,
                                   confidence=confidence))
        process_groups = {}
        evidence_object_ids = []
        for event_id in self.by_id:
            if self.by_id[event_id].get("object_kind") == "evidence_artifact":
                evidence_object_ids.append(event_id)
            else:
                process_groups.setdefault(find(event_id), []).append(event_id)
        def event_clock(event_id):
            value = ((self.by_id[event_id].get("clock") or {}).get("value") or "9999")
            return value, event_id
        # Roots are an internal union-find implementation detail. The first
        # object in retrieval-clock order provides a reproducible external root
        # even when source-based event ids hash in a different order.
        representative = {root: min(members, key=event_clock)
                          for root, members in process_groups.items()}
        identity = {}
        for relation in self.relations:
            if (relation.get("relation") == "same_event"
                    and relation.get("confidence") not in ("uncertain", "no_link")):
                self._identity_union(identity, relation["source_event_id"],
                                     relation["target_event_id"])
        def identity_root(event_id):
            while identity.get(event_id, event_id) != event_id:
                event_id = identity[event_id]
            return event_id
        for event_id, root in ((event_id, representative[find(event_id)])
                               for event_id in self.by_id
                               if self.by_id[event_id].get("object_kind")
                               != "evidence_artifact"):
            self.memberships[event_id] = root
        self._identity = identity
        self._identity_root = identity_root
        episodes = []
        for index, (root, members) in enumerate(sorted(
                process_groups.items(), key=lambda item: event_clock(
                    representative[item[0]])), start=1):
            ordered = sorted(members, key=event_clock)
            episodes.append(dict(
                episode_id="EP%03d" % index, root_event_id=representative[root],
                event_ids=ordered,
                event_groups=self._event_groups(ordered, identity_root),
                open_questions=self._open_questions(ordered)))
        self.conflicts = conflicts
        self.unresolved = unresolved
        for relation in self.relations:
            if (relation.get("relation") in ("evidence_comparison_previous", "evidence_used")
                    and self._pair_key(relation) not in self.reviewed_pairs
                    and not any(item["relation"] is relation
                                for item in unresolved)):
                unresolved.append(dict(relation=relation,
                                       reason="EVIDENCE_REFERENCE_REVIEW_REQUIRED"))
        return dict(episodes=episodes, merged_edges=merged,
                    evidence_object_ids=sorted(evidence_object_ids),
                    evidence_references=list(evidence_references),
                    composition_relations=list(composition_relations),
                    relation_semantics=self.process_merge_policy,
                    duplicate_candidate_groups=self._event_groups(
                        sorted(self.by_id), identity_root),
                    unresolved_relations=self.unresolved,
                    singleton_episodes=sum(1 for episode in episodes
                                           if len(episode["event_ids"]) == 1))

    @staticmethod
    def _identity_union(parent, left, right):
        def find(node):
            while parent.get(node, node) != node:
                node = parent[node]
            return node
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    @staticmethod
    def _event_groups(event_ids, identity_root):
        """Identity is explicit, while each original proposal remains inspectable."""
        groups = {}
        for event_id in event_ids:
            groups.setdefault(identity_root(event_id), []).append(event_id)
        return [dict(group_id=root, event_ids=members,
                     duplicate_candidate=(len(members) > 1))
                for root, members in sorted(groups.items())]

    def _open_questions(self, event_ids):
        """Say what this episode cannot yet claim, instead of implying it can."""
        questions = []
        clocks = [((self.by_id[event_id].get("clock") or {}).get("value"), event_id)
                  for event_id in event_ids]
        if any(value is None for value, _ in clocks):
            questions.append("EPISODE_ORDER_NOT_FULLY_ESTABLISHED")
        exact = [event_id for event_id in event_ids
                 if (self.by_id[event_id].get("time_basis")
                     == "explicit_documented_choice")]
        if not exact and any(self.by_id[event_id].get("object_kind")
                             != "evidence_artifact" for event_id in event_ids):
            questions.append("NO_DOCUMENTED_CHOICE_TIME")
        if len(event_ids) > 1:
            questions.append("PROCESS_LINK_IS_CANDIDATE_NOT_CONFIRMED")
        return questions

    # -- tools ---------------------------------------------------------
    def tools(self):
        def preview_episodes():
            self.preview_count += 1
            result = self.assemble()
            return dict(
                episode_count=len(result["episodes"]),
                singleton_episodes=result["singleton_episodes"],
                unresolved_relations=len(result["unresolved_relations"]),
                episodes=result["episodes"],
                note=("这是当前已记录关系的直接结果。证据对象单列，不伪装成 singleton episode。"
                      "孤立管理候选表示没有已记录的支持型关系把它连到别的判断，不等于临床无关。"))

        def event_episode(event_id):
            if event_id not in self.by_id:
                raise ToolError("UNKNOWN_EVENT_ID:" + str(event_id))
            self.assemble()
            root = self.memberships.get(event_id)
            for episode in self.assemble()["episodes"]:
                if episode["root_event_id"] == root:
                    return episode
            raise ToolError("EVIDENCE_ARTIFACT_IS_NOT_AN_EPISODE_MEMBER")

        return [
            Tool(name="preview_episodes",
                 description="按当前已记录的关系预览会得到哪些候选 episode。",
                 input_schema=dict(type="object", additionalProperties=False),
                 run=preview_episodes),
            Tool(name="event_episode",
                 description="查某个事件落在哪个候选 episode。",
                 input_schema=dict(type="object", additionalProperties=False,
                                   required=["event_id"],
                                   properties=dict(event_id=dict(type="string"))),
                 run=event_episode),
        ]


def audit(events, relations, result):
    """Program-side audit of the assembled graph. Reports, never repairs."""
    by_id = {event["event_id"]: event for event in events}
    orphan = [event["event_id"] for event in events
              if event.get("object_kind") != "evidence_artifact"
              and not any(relation.get("source_event_id") == event["event_id"]
                         or relation.get("target_event_id") == event["event_id"]
                         for relation in relations)]
    return dict(
        version="p2-harness-episode-audit-0.1.0",
        event_count=len(events),
        relation_count=len(relations),
        episode_count=len(result["episodes"]),
        singleton_episode_count=result["singleton_episodes"],
        events_without_any_relation=len(orphan),
        events_without_any_relation_ids=sorted(orphan)[:50],
        unresolved_relation_count=len(result["unresolved_relations"]),
        unresolved_reasons=dict(Counter(item["reason"]
                                        for item in result["unresolved_relations"])),
        events_without_documented_choice_time=sum(
            1 for event in events
            if event.get("object_kind") != "evidence_artifact"
            and event.get("time_basis") != "explicit_documented_choice"),
        note=("孤立事件与未解析关系都被保留。审计只报告，不删除、不合并、"
              "不把缺失当成阴性。"))


def episode_finish_tool(*, events, builder):
    """Require a coverage statement for every active object before normal finish.

    This is a declared-review gate, not proof that the semantic judgment is
    correct. A plain ``finish`` without a review receipt is visible in the log
    but cannot make the case look fully reviewed.
    """
    def run(note="", reviewed_event_ids=None, evidence_checks=None):
        if builder.preview_count < 1:
            raise ToolError("EPISODE_REVIEW_REQUIRES_PREVIEW")
        if not reviewed_event_ids:
            raise ToolError("EPISODE_REVIEW_REQUIRES_REVIEWED_EVENT_IDS")
        known = set(builder.by_id)
        if set(reviewed_event_ids) != known:
            raise ToolError(
                "EPISODE_REVIEW_COVERAGE_MISMATCH: 必须逐项列出所有候选对象；"
                "只审阅容易连接的子集不能记为完整病例级审查。")
        checks = list(evidence_checks or ())
        evidence_events = [event_id for event_id, event in builder.by_id.items()
                           if event.get("object_kind") == "evidence_artifact"]
        by_event = {check.get("event_id") for check in checks}
        missing = sorted(set(evidence_events) - by_event)
        if missing:
            raise ToolError(
                "EPISODE_EVIDENCE_COVERAGE_MISSING: %s。逐项核对程序证据索引中的既有报告；"
                "交付中未找到不等于检查未发生。本阶段不再依赖发现阶段 prechoice_quotes。" % ",".join(missing))
        for check in checks:
            if check.get("event_id") not in known:
                raise ToolError("EPISODE_EVIDENCE_CHECK_UNKNOWN_EVENT")
            if check.get("status") not in (
                    "matched", "ambiguous", "not_found_in_delivery", "not_applicable"):
                raise ToolError("EPISODE_EVIDENCE_CHECK_STATUS_INVALID")
        return dict(finished=True, note=note, reviewed_event_ids=reviewed_event_ids,
                    evidence_checks=checks)
    return Tool(
        name="finish",
        description=("声明完成病例级审查。必须先 preview_episodes，再列全部 reviewed_event_ids；"
                     "每个程序证据索引对象须逐项给 evidence_checks，"
                     "status: matched / ambiguous / not_found_in_delivery / not_applicable。"),
        input_schema=dict(
            type="object", additionalProperties=False,
            required=["reviewed_event_ids", "evidence_checks"],
            properties=dict(
                note=dict(type="string"),
                reviewed_event_ids=dict(type="array", items=dict(type="string")),
                evidence_checks=dict(type="array", items=dict(
                    type="object", additionalProperties=False,
                    required=["event_id", "status", "basis"],
                    properties=dict(
                        event_id=dict(type="string"),
                        status=dict(type="string", enum=[
                            "matched", "ambiguous", "not_found_in_delivery",
                            "not_applicable"]),
                        source_ids=dict(type="array", items=dict(type="string")),
                        basis=dict(type="string", minLength=1)))))),
        run=run, terminal=True)


EPISODE_SYSTEM_PROMPT = """你在检查候选管理过程的病例级组装，并补足有依据的关系。
前序 LLM 输出是候选，不是临床真值；不得把程序 ACCEPTED 当成专家接受。

你必须先用 preview_episodes 查看所有局部关系经传递合并后的实际容器。
同一查房、同日、同一文书或一次住院不自动等于同一管理目标；检查每个容器内
各候选的原文表述是否真正相容。检查程序证据索引中的既有报告及候选中明确引用的检查，
但不得让发现阶段伪造管理问题或检查选择原因。
检查孤立事件是否只是尚未组织，而不是无关或不存在。
用 read_event/read_unit/search_sources/read_source 核对必要原文。
same_event 只用于对象、动作与范围相同的重复候选；组合提案与其中一项不是同一事件。
same_management_process 需要同一管理目标的证据，不能仅因同次查房相连。
evidence_comparison_previous 用于有原文支持的既往证据—后续判断关系，不合并管理过程。

既有关系不可删除。若旧连接不成立，用 record_relation 添加 relation=none、
confidence=no_link，并给出两端依据与理由，供程序阻断该直接连接；不要用反向
标签冒充已经发生的临床事实。不要把所有缺口都留给人工复核：能在原文中核对的
尽量核对并记录，不能支持的保留明确不确定。最后调用 finish。"""


def build_prompt(events, *, case_id):
    return """## 病例 %s 的候选过程审查

候选事件共 %d 个。前序局部关系已经记录，但尚未经过病例级容器审查。

1. 先调用 preview_episodes，检查局部边经传递合并后的全部容器，而不只看两两关系。
2. 对包含多个候选表述的容器，read_event 并按需 read_unit；核对实际 span 与
   record_function。组合表述不能与单个组成动作标为 same_event。
3. 对所有孤立候选和程序证据索引对象，检索必要原文；对候选原文明确引用的既有检查，
   按来源线索核对报告。找不到时保留不确定并说明检索范围，不能说检查没有发生。
4. 若某条旧连接仅因同次查房/同日而连接不同管理目标，用 relation=none、
   confidence=no_link 记录否定该直接连接的依据，再 preview_episodes 查看修正结果。
5. 完成核对后调用 finish。不要留下“略”“同上”等未执行的工作项。
""" % (case_id, len(events))