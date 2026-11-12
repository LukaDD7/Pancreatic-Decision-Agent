"""Stage 2 — organize candidate expressions; do not rediscover their scope.

Discovery has already supplied located spans and a three-state research-relevance
judgment. ``scope=no`` is retained in the discovery artifact and never reaches
this stage. Organization may still demote a located candidate when original text
overturns its proposed interpretation; that annotation never deletes the span.
"""
from __future__ import annotations

from ..tools import Tool, ToolError

ORDERING_SYSTEM_PROMPT = """你在组织发现阶段 LLM 提交的候选管理判断表述之间的关系，用于PDAC路径的候选过程重建。
候选带原文边界和 scope 判断，但不是临床真值或专家Reference；scope=no 的排除索引不在本阶段。
不要重新执行发现阶段的研究范围分类。只有原文明确推翻某候选的表述解释时才可以降级，并须引用原文。

工作方式：先列出候选，再按需读实际 span 和原文。判断先后只能基于可比时间证据；
来源检索时钟不等于临床选择时刻，同一分钟/同一秒不可定序，未知就写未知。
管理过程相连看是否服务同一患者管理目标，不是日期相同、标题相同或同一次查房。
发现阶段不决定组合表述与具体表述的拆合；本阶段也不得以 same_event 冒充包含或子动作关系。

每个关系都须有可呈现原文依据，不能把看起来合理的配对当成事实。找不到连接证据时保留 uncertain。
先加载 ordering 技能了解完整规则与常见陷阱。"""

ORDERING_SKILL = "ordering"


class EventIndex:
    """Read-only view over confirmed events for the ordering turn."""

    def __init__(self, events, *, source_index):
        self.events = {event["event_id"]: event for event in events}
        self.source_index = source_index
        self.order = {event_id: event for event_id, event in self.events.items()}

    def require(self, event_id):
        event = self.events.get(event_id)
        if event is None:
            raise ToolError(
                "UNKNOWN_EVENT_ID: %r. Call list_events first." % (event_id,))
        return event

    def _summary(self, event):
        clock = (event.get("clock") or {}).get("value")
        return dict(
            event_id=event["event_id"],
            discovery_unit_id=event.get("discovery_unit_id"),
            origin_unit_id=event.get("origin_unit_id"),
            origin_source_ids=list(event.get("origin_source_ids") or ()),
            span=dict(event.get("span") or {}),
            record_function=event.get("record_function"), scope=event.get("scope"),
            candidate_kind=event.get("candidate_kind", "management_expression"),
            object_kind=event.get("object_kind"),
            clock=clock, clock_basis=event.get("clock_basis"),
            source_ids=list(event.get("source_ids") or ()),
            discovery_source_ids=list(event.get("discovery_source_ids") or ()),
            attribution_problems=list(event.get("attribution_problems") or ()))

    def tools(self):
        def list_events(discovery_unit_id=None, origin_source_id=None,
                        record_function=None, offset=0, limit=50):
            rows = []
            for event in self.events.values():
                if (discovery_unit_id
                        and event.get("discovery_unit_id") != discovery_unit_id):
                    continue
                if (origin_source_id
                        and origin_source_id not in (event.get("origin_source_ids") or ())):
                    continue
                if record_function and event.get("record_function") != record_function:
                    continue
                rows.append(self._summary(event))
            rows.sort(key=lambda row: (row["clock"] or "9999", row["event_id"]))
            offset = max(0, int(offset))
            limit = max(1, min(200, int(limit)))
            page = rows[offset:offset + limit]
            return dict(total=len(rows), offset=offset, returned=len(page),
                        truncated=offset + len(page) < len(rows), events=page)

        def read_event(event_id):
            event = self.require(event_id)
            payload = dict(self._summary(event))
            payload.update(
                uncertainty=event.get("uncertainty"), rationale=event.get("rationale"),
                reason=event.get("reason"),
                source_inventory=list(event.get("source_inventory") or ()),
                evidence_selection_record_status=event.get(
                    "evidence_selection_record_status"),
                attribution_note=event.get("attribution_note"),
                citation_verdicts=event.get("citation_verdicts"))
            return payload

        def compare_clocks(left_event_id, right_event_id):
            left = self.require(left_event_id)
            right = self.require(right_event_id)
            return compare(left, right)

        def search_events(query, limit=20):
            if not isinstance(query, str) or not query.strip():
                raise ToolError("SEARCH_QUERY_REQUIRED")
            needle = query.strip()
            limit = max(1, min(100, int(limit)))
            hits = []
            for event in self.events.values():
                haystack = " ".join(str(event.get(key) or "") for key in (
                    "span", "rationale", "reason", "uncertainty"))
                if needle in haystack:
                    hits.append(self._summary(event))
            return dict(total=len(hits), returned=min(len(hits), limit),
                        truncated=len(hits) > limit, events=hits[:limit])

        return [
            Tool(name="list_events",
                 description="列出候选管理表述（保留 event_id 兼容键，不是已确认临床事件）。",
                 input_schema=dict(type="object", additionalProperties=False,
                                   properties=dict(
                                       discovery_unit_id=dict(type="string"),
                                       origin_source_id=dict(type="string"),
                                       record_function=dict(type="string"),
                                       offset=dict(type="integer", minimum=0),
                                       limit=dict(type="integer", minimum=1, maximum=200))),
                 run=list_events),
            Tool(name="read_event",
                 description="读一个事件的完整记录，包括引文与程序核验结果。",
                 input_schema=dict(type="object", additionalProperties=False,
                                   required=["event_id"],
                                   properties=dict(event_id=dict(type="string"))),
                 run=read_event),
            Tool(name="compare_clocks",
                 description=("比较两个事件的时间证据，返回 before/after/unknown/"
                              "same_time_unresolved 及双方精度。这是程序计算，不是你猜。"),
                 input_schema=dict(type="object", additionalProperties=False,
                                   required=["left_event_id", "right_event_id"],
                                   properties=dict(
                                       left_event_id=dict(type="string"),
                                       right_event_id=dict(type="string"))),
                 run=compare_clocks),
            Tool(name="search_events",
                 description="在候选原文片段/理由/不确定性文本中做字面检索。",
                 input_schema=dict(type="object", additionalProperties=False,
                                   required=["query"],
                                   properties=dict(
                                       query=dict(type="string", minLength=1),
                                       limit=dict(type="integer", minimum=1, maximum=100))),
                 run=search_events),
        ]


def compare(left, right):
    """Compare two events' time evidence without inventing precision.

    Returns a total order only when both sides have a comparable value. Anything
    else is named explicitly, because "unknown" and "same time" are different
    answers and both differ from "earlier".
    """
    from ...p2_pathway_replay.core import _compare_clocks

    left_clock = left.get("clock") or {}
    right_clock = right.get("clock") or {}
    if not left_clock.get("value") or not right_clock.get("value"):
        return dict(relation="unknown", reason="ONE_OR_BOTH_CLOCKS_UNKNOWN",
                    left=dict(value=left_clock.get("value"),
                              precision=left_clock.get("precision")),
                    right=dict(value=right_clock.get("value"),
                               precision=right_clock.get("precision")),
                    note=("缺少可比较的时钟。这不等于两者无关，也不等于其中之一更早。"))
    raw = _compare_clocks(right_clock, left_clock)
    if raw == "BEFORE":
        relation = "after"
    elif raw == "AFTER":
        relation = "before"
    elif raw in ("SAME_MINUTE_UNORDERED", "SAME_TIME_UNORDERED",
                 "SAME_DAY_UNORDERED"):
        relation = "same_time_unresolved"
    else:
        relation = "unknown"
    return dict(relation=relation, reason=raw,
                left=dict(value=left_clock.get("value"),
                          precision=left_clock.get("precision")),
                right=dict(value=right_clock.get("value"),
                           precision=right_clock.get("precision")),
                note=("这是检索时钟的比较，不是临床决策时间的比较。"
                      "选择时间未知时，两者先后仍可能无法确定。"))


def relation_tools(registry, *, events, allow_demote=True, source_index):
    """Write tools for the ordering turn: one relation at a time, one demotion.

    ``same_event`` is a semantic identity claim, not a graph merge. Keep every
    event and let the episode builder retain its members and open questions.
    """
    known = {event["event_id"] for event in events}
    by_id = {event["event_id"]: event for event in events}
    relations = []
    demotions = []

    def record_relation(source_event_id, target_event_id, relation,
                        basis_quotes=None, confidence="uncertain", explanation=""):
        if relation not in ("same_management_process", "evidence_comparison_previous",
                            "same_event", "none"):
            raise ToolError("RELATION_KIND_INVALID:" + str(relation))
        if source_event_id not in known:
            raise ToolError("UNKNOWN_EVENT_ID:" + str(source_event_id))
        if target_event_id is not None and target_event_id not in known:
            raise ToolError("UNKNOWN_EVENT_ID:" + str(target_event_id))
        if source_event_id == target_event_id and relation != "same_event":
            raise ToolError("SELF_RELATION_NOT_ALLOWED")
        if relation in ("same_event", "same_management_process"):
            endpoints = (source_event_id, target_event_id)
            if any(by_id[event_id].get("object_kind") == "evidence_artifact"
                   for event_id in endpoints):
                raise ToolError(
                    "EVIDENCE_ARTIFACT_NOT_MANAGEMENT_ENDPOINT: 检查证据对象不是管理判断或"
                    "同事件候选；用 evidence_comparison_previous 连接到引用它的后续管理判断，"
                    "不要合并管理过程。")
        if relation == "evidence_comparison_previous" and target_event_id is None:
            raise ToolError("EVIDENCE_REFERENCE_REQUIRES_TARGET")
        if relation == "none" and confidence == "no_link":
            if target_event_id is None:
                raise ToolError("REFUTATION_REQUIRES_TARGET")
            if not basis_quotes or not explanation.strip():
                raise ToolError(
                    "REFUTATION_REQUIRES_BASIS: 否定既有连接须引用两端原文并解释管理目标为何不同。")
        if relation in ("same_event", "same_management_process",
                        "evidence_comparison_previous") \
                or (relation == "none" and confidence == "no_link"):
            if not basis_quotes:
                raise ToolError(
                    "RELATION_REQUIRES_BASIS: 该关系必须附支持连接的原文引文。")
            if confidence in ("supported", "context_only", "no_link"):
                for citation in basis_quotes:
                    verdict = source_index.check_quote(citation, label="relation_basis")
                    if not verdict.get("ok"):
                        raise ToolError(
                            "RELATION_BASIS_NOT_VERIFIED: %s: %s"
                            % (citation.get("source_id"), verdict.get("reason")))
                endpoint_sources = [
                    set(by_id[event_id].get("source_ids") or ())
                    for event_id in (source_event_id, target_event_id)]
                for citation in basis_quotes:
                    if not any(citation.get("source_id") in source_ids
                               for source_ids in endpoint_sources):
                        raise ToolError(
                            "RELATION_BASIS_OUTSIDE_ENDPOINT_SOURCES: %s 不是任一端点的已引用来源；"
                            "不要把第三份文书当作该关系的直接依据。"
                            % citation.get("source_id"))
                unmatched = [source_id for source_id, citations in zip(
                    (source_event_id, target_event_id), endpoint_sources)
                    if not any(citation.get("source_id") in citations
                               for citation in basis_quotes)]
                if unmatched:
                    raise ToolError(
                        "RELATION_BASIS_MISSING_ENDPOINT: %s 缺少本端点的支持引文。"
                        % ", ".join(unmatched))
                if relation == "evidence_comparison_previous" and target_event_id is None:
                    raise ToolError("EVIDENCE_REFERENCE_REQUIRES_TARGET")
        entry = dict(source_event_id=source_event_id, target_event_id=target_event_id,
                     relation=relation, basis_quotes=list(basis_quotes or ()),
                     confidence=confidence, explanation=explanation)
        if relation == "none" and confidence == "no_link":
            entry["refutes"] = "direct_same_management_process_only"
        relations.append(entry)
        return dict(recorded=True, index=len(relations) - 1, relation=entry,
                    note=("关系已记录。它不会被自动放行——后续阶段仍会核对"
                          "两端是否都存在、引文是否可定位。"))

    def demote_event(event_id, reason, basis_quotes=None, to_status="not_a_checkpoint"):
        if not allow_demote:
            raise ToolError("DEMOTION_NOT_PERMITTED_IN_THIS_TURN")
        if event_id not in known:
            raise ToolError("UNKNOWN_EVENT_ID:" + str(event_id))
        if not basis_quotes:
            raise ToolError(
                "DEMOTION_REQUIRES_BASIS: 降级必须引用推翻先前判断的原文。"
                "没有原文依据时请改用 uncertainty，不要降级。")
        for citation in basis_quotes:
            verdict = source_index.check_quote(citation, label="demotion_basis")
            if not verdict.get("ok"):
                raise ToolError(
                    "DEMOTION_BASIS_NOT_VERIFIED: %s: %s"
                    % (citation.get("source_id"), verdict.get("reason")))
        entry = dict(event_id=event_id, reason=reason,
                     basis_quotes=list(basis_quotes), to_status=to_status)
        demotions.append(entry)
        return dict(recorded=True, demotion=entry)

    registry.register(Tool(
        name="record_relation",
        description=("记录两个候选对象之间的关系及支持原文。same_event 只表示严格身份候选，"
                     "不表示组合/子动作；证据对象只可作 evidence_comparison_previous 端点。"
                     "confidence: supported / context_only / uncertain / no_link。"),
        input_schema=dict(type="object", additionalProperties=False,
                          required=["source_event_id", "relation", "confidence"],
                          properties=dict(
                              source_event_id=dict(type="string"),
                              target_event_id=dict(type="string"),
                              relation=dict(type="string", enum=[
                                  "same_management_process",
                                  "evidence_comparison_previous",
                                  "same_event", "none"]),
                              basis_quotes=dict(type="array", items=dict(
                                  type="object", additionalProperties=False,
                                  required=["source_id", "quote"],
                                  properties=dict(source_id=dict(type="string"),
                                                  quote=dict(type="string")))),
                              confidence=dict(type="string", enum=[
                                  "supported", "context_only", "uncertain", "no_link"]),
                              explanation=dict(type="string"))),
        run=record_relation, writes=True))
    if allow_demote:
        registry.register(Tool(
            name="demote_event",
            description=("给候选表述添加降级标注（例如原文其实仅是回述），不删除原片段。"
                         "必须给出推翻先前判断的原文引文。"),
            input_schema=dict(type="object", additionalProperties=False,
                              required=["event_id", "reason"],
                              properties=dict(
                                  event_id=dict(type="string"),
                                  reason=dict(type="string", minLength=1),
                                  basis_quotes=dict(type="array", items=dict(
                                      type="object", additionalProperties=False,
                                      required=["source_id", "quote"],
                                      properties=dict(
                                          source_id=dict(type="string"),
                                          quote=dict(type="string")))),
                                  to_status=dict(type="string", enum=[
                                      "not_a_checkpoint", "repeat",
                                      "out_of_scope", "uncertain"]))),
            run=demote_event, writes=True))
    return dict(relations=relations, demotions=demotions)


def build_prompt(events, *, case_id, coverage_note=""):
    lines = [
        "## 病例 %s 的事件组织" % case_id,
        "候选事件共 %d 个。" % len(events),
        "",
        "## 任务",
        "1. 用 list_events 浏览全部事件，再对你真正需要判断的那些用 read_event 细看。",
        "2. 对同单位或动作相近的事件先用 read_unit 核对完整原文。若不同 local_id 实际指向同一安排，记录 same_event；它们仍是保留的候选成员，不自动当成多次决策。",
        "3. 用 compare_clocks 计算任何一对事件的先后，不要凭印象。",
        "4. 对相关候选对调用 record_relation；same_event / same_management_process 必须附可核验的两端原文。same_event 只表示严格身份候选；组合表述与具体表述的包含/子动作/重叠尚无独立关系类型，不得强行标为 same_event。不要用第三份文书代替连接证据。",
        "5. 如果发现某个事件其实不成立，用 demote_event 降级并引用推翻它的原文。",
        "6. 判断完毕后调用 finish。",
        "",
        "注意：不需要为每一对事件都建立关系。只记录你能给出依据的那些；"
        "其余保持无关系，后续阶段会把它作为孤立事件明确列出，而不是静默丢掉。",
    ]
    if coverage_note:
        lines.append("\n" + coverage_note)
    return "\n".join(lines)