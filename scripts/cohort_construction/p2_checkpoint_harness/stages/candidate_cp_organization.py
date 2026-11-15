"""Record relations between reprojected candidate Checkpoints and assemble Episodes.

The agent writes only supported CP-to-CP edges. Diagnostic before/after edges and
all Episode containers are derived by program code; no event-level splitting is
available in this stage.
"""
from __future__ import annotations

from copy import deepcopy
import json

from ..tools import Tool, ToolError
from .common import digest, finish_tool
from .ordering import compare


RELATIONS_FORMAT = "p2-candidate-cp-relations-0.1.0"
EPISODES_FORMAT = "p2-candidate-cp-episodes-0.1.0"
ORGANIZATION_SYSTEM_PROMPT = """你的唯一任务是依据原文记录候选Checkpoint（CP）之间有明确支持的关系。
输入节点已经由程序生成，checkpoint_id是唯一端点标识。不要重新发现候选、不要拆分member_event_ids、不要修改masked_spans、不要组织或预览Episode。
先加载candidate-cp-organization技能，用list_candidate_checkpoints读取CP；只有核对某条具体关系时才读取局部原文。发现明确关系立即record_relation。
本阶段只记录confidence=supported的边；不确定就不记录，程序会保留未连接CP。same_management_process必须填写shared_management_goal，并由两端原文支持同一管理目标，同日、同次查房或时间相近不够。
same_event仅指严格重复身份；contains/overlap不合并Episode。evidence_used只能由evidence_arrival_reassessment指向management_decision，必须有原文明确显示后续管理判断使用了该证据，不能由检查时间或内容相似自动推断。
检查前→报告后的evidence_arrival边由程序生成，不由你记录；证据到达不等于证据被使用。诊断CP没有管理span是正常情况，可引用其source_ids内报告的逐字原文。
每条边必须给两端逐字basis_quotes，每条引文标注其checkpoint_id和source_id。引文必须属于该CP的实际masked_spans或其来源原文，不得拼接、改写或引用第三份文书。完成后调用finish。"""


class CandidateCheckpointIndex:
    def __init__(self, checkpoints):
        self.checkpoints = list(checkpoints)
        self.by_id = {}
        for checkpoint in self.checkpoints:
            checkpoint_id = checkpoint.get("checkpoint_id")
            if not isinstance(checkpoint_id, str) or not checkpoint_id:
                raise ValueError("CANDIDATE_CHECKPOINT_ID_REQUIRED")
            if checkpoint_id in self.by_id:
                raise ValueError("DUPLICATE_CANDIDATE_CHECKPOINT_ID:" + checkpoint_id)
            self.by_id[checkpoint_id] = checkpoint

    def require(self, checkpoint_id):
        checkpoint = self.by_id.get(checkpoint_id)
        if checkpoint is None:
            raise ToolError("UNKNOWN_CHECKPOINT_ID:" + str(checkpoint_id))
        return checkpoint

    @staticmethod
    def _summary(checkpoint):
        return dict(
            checkpoint_id=checkpoint["checkpoint_id"], kind=checkpoint.get("kind"),
            source_ids=list(checkpoint.get("source_ids") or ()),
            member_event_ids=list(checkpoint.get("member_event_ids") or ()),
            supporting_event_ids=list(checkpoint.get("supporting_event_ids") or ()),
            masked_spans=deepcopy(checkpoint.get("masked_spans") or []),
            clock=deepcopy(checkpoint.get("clock") or {}),
            time_basis=checkpoint.get("time_basis"),
            evaluation_time_role=checkpoint.get("evaluation_time_role"),
            diagnostic_event_id=checkpoint.get("diagnostic_event_id"),
            candidate_status=checkpoint.get("candidate_status"),
            review_status=checkpoint.get("review_status"))

    def tools(self):
        def list_candidate_checkpoints(kind=None, source_id=None, offset=0, limit=100):
            rows = [checkpoint for checkpoint in self.checkpoints
                    if (kind is None or checkpoint.get("kind") == kind)
                    and (source_id is None
                         or source_id in (checkpoint.get("source_ids") or ()))]
            rows.sort(key=lambda item: ((item.get("clock") or {}).get("value") or "9999",
                                        item["checkpoint_id"]))
            offset = max(0, int(offset))
            limit = max(1, min(200, int(limit)))
            page = rows[offset:offset + limit]
            return dict(total=len(rows), offset=offset, returned=len(page),
                        truncated=offset + len(page) < len(rows),
                        checkpoints=[self._summary(item) for item in page],
                        note=("每个管理CP可能已合并同一source的多个member_event_ids；"
                              "本阶段不得拆回event级节点。诊断CP可以没有masked_spans。"))

        def read_candidate_checkpoint(checkpoint_id):
            return self._summary(self.require(checkpoint_id))

        def compare_checkpoint_clocks(left_checkpoint_id, right_checkpoint_id):
            left = self.require(left_checkpoint_id)
            right = self.require(right_checkpoint_id)
            return compare(left, right)

        return [
            Tool(name="list_candidate_checkpoints",
                 description="列出候选CP及其masked_spans；端点只能使用checkpoint_id。",
                 input_schema=dict(type="object", additionalProperties=False,
                                   properties=dict(
                                       kind=dict(type="string"),
                                       source_id=dict(type="string"),
                                       offset=dict(type="integer", minimum=0),
                                       limit=dict(type="integer", minimum=1, maximum=200))),
                 run=list_candidate_checkpoints),
            Tool(name="read_candidate_checkpoint",
                 description="读取一个候选CP的来源、成员与masked_spans，不拆分成员事件。",
                 input_schema=dict(type="object", additionalProperties=False,
                                   required=["checkpoint_id"],
                                   properties=dict(checkpoint_id=dict(type="string"))),
                 run=read_candidate_checkpoint),
            Tool(name="compare_checkpoint_clocks",
                 description="比较CP资料检索时钟；结果不是临床选择时间或证据使用证明。",
                 input_schema=dict(type="object", additionalProperties=False,
                                   required=["left_checkpoint_id", "right_checkpoint_id"],
                                   properties=dict(left_checkpoint_id=dict(type="string"),
                                                   right_checkpoint_id=dict(type="string"))),
                 run=compare_checkpoint_clocks),
        ]


def _verify_relation_citation(citation, checkpoints, source_index):
    if not isinstance(citation, dict):
        raise ToolError("RELATION_CITATION_NOT_OBJECT")
    checkpoint_id = citation.get("checkpoint_id")
    checkpoint = checkpoints.get(checkpoint_id)
    if checkpoint is None:
        raise ToolError("RELATION_CITATION_UNKNOWN_CHECKPOINT:" + str(checkpoint_id))
    source_id = citation.get("source_id")
    if source_id not in (checkpoint.get("source_ids") or ()):
        raise ToolError("RELATION_CITATION_OUTSIDE_CHECKPOINT_SOURCES:" + str(checkpoint_id))
    verdict = source_index.check_quote(citation, label="candidate_cp_relation_basis")
    if not verdict.get("ok"):
        raise ToolError("RELATION_BASIS_NOT_VERIFIED:" + str(verdict.get("reason")))
    start, end = citation.get("start"), citation.get("end")
    if (start is None) != (end is None):
        raise ToolError("RELATION_CITATION_OFFSETS_REQUIRE_BOTH")
    if start is not None:
        if (type(start) is not int or type(end) is not int or start < 0
                or end != start + len(citation.get("quote") or "")):
            raise ToolError("RELATION_CITATION_OFFSET_INVALID")
        source = source_index.require(source_id)
        if (source.get("text") or "")[start:end] != citation.get("quote"):
            raise ToolError("RELATION_CITATION_OFFSET_MISMATCH")
    return verdict


def candidate_cp_relation_tools(registry, *, checkpoints, source_index):
    known = {checkpoint["checkpoint_id"]: checkpoint for checkpoint in checkpoints}
    relations = []
    relation_kinds = {"same_event", "same_management_process", "contains", "overlap",
                      "evidence_used"}

    def record_relation(source_checkpoint_id, target_checkpoint_id, relation,
                        basis_quotes=None, confidence="supported", explanation="",
                        shared_management_goal=""):
        source = known.get(source_checkpoint_id)
        target = known.get(target_checkpoint_id)
        if source is None:
            raise ToolError("UNKNOWN_CHECKPOINT_ID:" + str(source_checkpoint_id))
        if target is None:
            raise ToolError("UNKNOWN_CHECKPOINT_ID:" + str(target_checkpoint_id))
        if source_checkpoint_id == target_checkpoint_id:
            raise ToolError("SELF_RELATION_NOT_ALLOWED")
        if relation not in relation_kinds:
            raise ToolError("RELATION_KIND_INVALID:" + str(relation))
        if confidence != "supported":
            raise ToolError("CANDIDATE_CP_RELATION_REQUIRES_SUPPORTED_CONFIDENCE")
        if not explanation.strip():
            raise ToolError("RELATION_REQUIRES_EXPLANATION")
        if relation == "same_management_process" and not shared_management_goal.strip():
            raise ToolError("SAME_MANAGEMENT_PROCESS_REQUIRES_SHARED_GOAL")
        if relation == "evidence_used":
            if (source.get("kind") != "evidence_arrival_reassessment"
                    or target.get("kind") != "management_decision"):
                raise ToolError(
                    "EVIDENCE_USE_DIRECTION_INVALID: source必须是报告可见CP，target必须是管理CP。")
            time_relation = compare(source, target)["relation"]
            if time_relation == "after":
                raise ToolError("EVIDENCE_USE_CLOCK_DIRECTION_INVALID")
        else:
            time_relation = None
        if not basis_quotes:
            raise ToolError("RELATION_REQUIRES_BASIS_AND_EXPLANATION")
        for citation in basis_quotes:
            _verify_relation_citation(citation, known, source_index)
        for endpoint in (source_checkpoint_id, target_checkpoint_id):
            if not any(citation.get("checkpoint_id") == endpoint
                       for citation in basis_quotes):
                raise ToolError("RELATION_BASIS_MISSING_ENDPOINT:" + endpoint)
        entry = dict(
            source_checkpoint_id=source_checkpoint_id,
            target_checkpoint_id=target_checkpoint_id,
            relation=relation, confidence="supported",
            basis_quotes=deepcopy(basis_quotes), explanation=explanation,
            relation_source="candidate_cp_agent_recording",
            program_diagnostic_pair=False)
        if relation == "same_management_process":
            entry["shared_management_goal"] = shared_management_goal.strip()
        if relation == "evidence_used":
            entry.update(
                retrieval_clock_comparison=time_relation,
                comparison_not_choice_time=True,
                evidence_use_basis="explicit_source_text_required_not_temporal_similarity")
        entry["relation_id"] = "CPR-" + digest(entry)[:20]
        if any(item["relation_id"] == entry["relation_id"] for item in relations):
            raise ToolError("RELATION_ALREADY_RECORDED:" + entry["relation_id"])
        relations.append(entry)
        return dict(recorded=True, relation=entry,
                    note=("程序已核验CP端点、方向和逐字引文；这不等于临床语义或Episode边界"
                          "已获专家确认。Episode由程序随后组装。"))

    citation_schema = dict(
        type="array", minItems=2,
        items=dict(type="object", additionalProperties=False,
                   required=["checkpoint_id", "source_id", "quote"],
                   properties=dict(
                       checkpoint_id=dict(type="string"), source_id=dict(type="string"),
                       quote=dict(type="string", minLength=1),
                       start=dict(type="integer", minimum=0),
                       end=dict(type="integer", minimum=0))))
    registry.register(Tool(
        name="record_relation",
        description=("记录两个候选CP之间有原文支持的边；仅confidence=supported。"
                     "evidence_used为报告可见CP→管理CP，不合并管理Episode。"),
        input_schema=dict(
            type="object", additionalProperties=False,
            required=["source_checkpoint_id", "target_checkpoint_id", "relation",
                      "confidence", "basis_quotes", "explanation"],
            properties=dict(
                source_checkpoint_id=dict(type="string"),
                target_checkpoint_id=dict(type="string"),
                relation=dict(type="string", enum=sorted(relation_kinds)),
                confidence=dict(type="string", enum=["supported"]),
                basis_quotes=citation_schema,
                explanation=dict(type="string", minLength=1),
                shared_management_goal=dict(type="string"))),
        run=record_relation, writes=True))
    return dict(relations=relations)


def program_diagnostic_pair_relations(projection):
    relations = []
    for event in projection.get("diagnostic_events") or []:
        ids = event.get("candidate_checkpoint_ids") or {}
        source = ids.get("pre_exam_evaluation")
        target = ids.get("evidence_arrival_reassessment")
        if not source or not target:
            continue
        entry = dict(
            source_checkpoint_id=source, target_checkpoint_id=target,
            relation="evidence_arrival", confidence="supported", basis_quotes=[],
            explanation="程序依据同一诊断事件的检查执行前节点与报告可见后节点保留方向边。",
            relation_source="program_diagnostic_event_pair",
            program_diagnostic_pair=True, diagnostic_event_id=event.get("event_id"),
            evidence_arrival_does_not_imply_evidence_used=True,
            management_span_required=False)
        entry["relation_id"] = "CPR-DIAG-" + digest(entry)[:20]
        relations.append(entry)
    return relations


def assemble_candidate_cp_episodes(checkpoints, relations):
    by_id = {checkpoint["checkpoint_id"]: checkpoint for checkpoint in checkpoints}
    parent = {checkpoint_id: checkpoint_id for checkpoint_id in by_id}

    def find(checkpoint_id):
        while parent[checkpoint_id] != checkpoint_id:
            parent[checkpoint_id] = parent[parent[checkpoint_id]]
            checkpoint_id = parent[checkpoint_id]
        return checkpoint_id

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    merged_edges, evidence_references, composition_relations = [], [], []
    identity_relations, unresolved = [], []
    for relation in relations:
        source = relation.get("source_checkpoint_id")
        target = relation.get("target_checkpoint_id")
        if source not in by_id or target not in by_id:
            unresolved.append(dict(relation=deepcopy(relation),
                                   reason="UNKNOWN_CHECKPOINT_ENDPOINT"))
            continue
        kind = relation.get("relation")
        supported = relation.get("confidence") == "supported"
        diagnostic_pair = (kind == "evidence_arrival"
                           and relation.get("program_diagnostic_pair") is True
                           and relation.get("relation_source") == "program_diagnostic_event_pair")
        if supported and (kind == "same_management_process" or diagnostic_pair):
            union(source, target)
            merged_edges.append(dict(
                source_checkpoint_id=source, target_checkpoint_id=target,
                relation=kind, relation_id=relation.get("relation_id"),
                merge_basis=("program_diagnostic_pair" if diagnostic_pair
                             else "agent_supported_same_management_goal")))
        elif kind == "evidence_used":
            evidence_references.append(deepcopy(relation))
        elif kind in ("contains", "overlap"):
            composition_relations.append(deepcopy(relation))
        elif kind == "same_event":
            identity_relations.append(deepcopy(relation))
        elif not supported:
            unresolved.append(dict(relation=deepcopy(relation),
                                   reason="RELATION_NOT_SUPPORTED"))

    groups = {}
    for checkpoint_id in by_id:
        groups.setdefault(find(checkpoint_id), []).append(checkpoint_id)

    def order_key(checkpoint_id):
        checkpoint = by_id[checkpoint_id]
        return ((checkpoint.get("clock") or {}).get("value") or "9999", checkpoint_id)

    episodes = []
    for index, (_, members) in enumerate(sorted(
            groups.items(), key=lambda item: min(map(order_key, item[1]))), start=1):
        ordered = sorted(members, key=order_key)
        open_questions = ["CANDIDATE_EPISODE_NOT_CLINICALLY_CONFIRMED"]
        if any(not (by_id[item].get("clock") or {}).get("value") for item in ordered):
            open_questions.append("EPISODE_ORDER_NOT_FULLY_ESTABLISHED")
        episodes.append(dict(
            episode_id="EP-CP-%04d" % index,
            root_checkpoint_id=min(ordered, key=order_key),
            checkpoint_ids=ordered,
            checkpoint_kinds={item: by_id[item].get("kind") for item in ordered},
            open_questions=open_questions))

    connected = {endpoint for relation in relations for endpoint in (
        relation.get("source_checkpoint_id"), relation.get("target_checkpoint_id"))}
    assigned = [item for episode in episodes for item in episode["checkpoint_ids"]]
    return dict(
        episodes=episodes, merged_edges=merged_edges,
        evidence_references=evidence_references,
        composition_relations=composition_relations,
        identity_relations=identity_relations,
        program_diagnostic_pair_edges=[deepcopy(relation) for relation in relations
                                       if relation.get("program_diagnostic_pair") is True],
        unresolved_relations=unresolved,
        singleton_episodes=sum(len(episode["checkpoint_ids"]) == 1
                               for episode in episodes),
        unconnected_checkpoint_ids=sorted(set(by_id) - connected),
        conservation=dict(candidate_checkpoint_count=len(by_id),
                          assigned_checkpoint_count=len(assigned),
                          unique_assigned_checkpoint_count=len(set(assigned)),
                          all_candidate_checkpoints_conserved=(
                              len(assigned) == len(by_id) and set(assigned) == set(by_id))),
        relation_semantics=(
            "仅Agent supported same_management_process与程序诊断pair合并；"
            "evidence_used、same_event、contains、overlap不合并；所有CP保留。"))


def build_prompt(checkpoints, *, case_id):
    return """## 病例 %s 的候选Checkpoint关系记录

候选CP共 %d 个。先加载candidate-cp-organization技能，再list_candidate_checkpoints(limit=200)。
CP已按程序投影组织，同一source的管理候选可能已经合并，禁止拆回member_event_ids另建节点。
只记录原文明确支持的CP关系并立即提交；不要记录不确定边，不要组织Episode。
诊断检查前→报告后边由程序另行保留，不要用evidence_used代替它。
没有可支持连接的CP会由原程序保留，完成有依据的边后直接finish。
""" % (case_id, len(checkpoints))


def recovery_context(relations):
    pairs = [[item["source_checkpoint_id"], item["target_checkpoint_id"],
              item["relation"], item["relation_id"]] for item in relations]
    return ("上次传输失败前以下CP关系已保存，不要重复记录；继续其他有依据的关系：\n"
            + json.dumps(pairs, ensure_ascii=False))


class CandidateCPOrganizationRunner:
    def __init__(self, *, harness, source_index, projection, case_id):
        self.harness = harness
        self.source_index = source_index
        self.projection = projection
        self.case_id = case_id

    def run(self):
        from .. import session as S
        from ..transport import TransportFailed

        checkpoints = self.projection.get("candidate_checkpoints") or []
        saved, recovery = [], dict(attempts=0, recovered=False,
                                  exhausted=False, sessions=[])
        stop, final, failure = ("answered", {}, None) if not checkpoints else (None, {}, None)
        limit = self.harness.config.max_transport_recovery_attempts
        for attempt in range(limit + 1 if checkpoints else 1):
            name = ("candidate_cp_organization" if not attempt
                    else "candidate_cp_organization-recovery-%d" % attempt)
            recovery["sessions"].append(name)
            recovery["attempts"] = attempt
            session = self.harness.session_for(name)
            registry = self.harness.new_registry(session=session)
            self.source_index.register(registry)
            for tool in CandidateCheckpointIndex(checkpoints).tools():
                registry.register(tool)
            recorded = candidate_cp_relation_tools(
                registry, checkpoints=checkpoints, source_index=self.source_index)
            agent = self.harness.agent(
                registry=registry, session=session,
                system_prompt=ORGANIZATION_SYSTEM_PROMPT)
            prompt = build_prompt(checkpoints, case_id=self.case_id)
            if attempt:
                prompt += "\n\n" + recovery_context([
                    relation for record in saved for relation in record["relations"]])
            try:
                outcome = agent.act(prompt, final_tool=finish_tool(label="候选CP关系记录"))
                stop, final = outcome.stop, outcome.final
                saved.append(recorded)
                recovery["recovered"] = bool(attempt)
                break
            except TransportFailed as exc:
                stop, final = "request_failed", {}
                failure = dict(error=type(exc).__name__, reason_codes=sorted({
                    item.get("reason_code") for item in exc.receipt.get("attempts", [])
                    if item.get("reason_code")}))
                saved.append(recorded)
                session.append(S.TURN_END, dict(
                    stop=stop, steps=len(agent.tools.calls), transport_failed=True,
                    recovery_attempt=attempt), turn=agent.turns)
                if attempt < limit and self.harness.case_budget.exhausted() is None:
                    continue
                recovery["exhausted"] = True
                break
        agent_relations = []
        for record in saved:
            for relation in record["relations"]:
                if all(item["relation_id"] != relation["relation_id"]
                       for item in agent_relations):
                    agent_relations.append(relation)
        diagnostic_relations = program_diagnostic_pair_relations(self.projection)
        relations = agent_relations + diagnostic_relations
        episodes = assemble_candidate_cp_episodes(checkpoints, relations)
        stage = dict(stage="candidate_cp_organization", stop=stop, final=final,
                     failure=failure, recovery=recovery,
                     agent_task="record_supported_candidate_cp_relations_only",
                     container_assembly="program_derived_not_agent_reviewed",
                     candidate_checkpoint_count=len(checkpoints),
                     agent_relation_count=len(agent_relations),
                     program_diagnostic_pair_count=len(diagnostic_relations))
        return dict(relations=relations, agent_relations=agent_relations,
                    program_diagnostic_pair_relations=diagnostic_relations,
                    episodes=episodes, stage=stage)


def relations_document(*, case_id, projection, projection_sha256, result):
    return dict(
        format=RELATIONS_FORMAT, case_id=case_id,
        candidate_projection_sha256=projection_sha256,
        candidate_checkpoints=deepcopy(projection["candidate_checkpoints"]),
        relations=result["relations"], agent_relations=result["agent_relations"],
        program_diagnostic_pair_relations=result["program_diagnostic_pair_relations"],
        organization=result["stage"], clinical_reference_created=False,
        note=("Agent只记录supported CP关系；诊断pair由程序生成。"
              "引文核验不表示临床关系已获专家确认。"))


def episodes_document(*, case_id, projection_sha256, result):
    return dict(format=EPISODES_FORMAT, case_id=case_id,
                candidate_projection_sha256=projection_sha256,
                **result["episodes"], clinical_reference_created=False,
                note="Episode成员只使用checkpoint_ids；未连接CP作为singleton保留。")


__all__ = [
    "CandidateCheckpointIndex", "CandidateCPOrganizationRunner",
    "ORGANIZATION_SYSTEM_PROMPT", "assemble_candidate_cp_episodes",
    "build_prompt", "candidate_cp_relation_tools", "episodes_document",
    "program_diagnostic_pair_relations", "relations_document",
]
