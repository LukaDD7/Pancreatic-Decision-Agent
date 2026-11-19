"""Relate located candidates and evidence; container assembly is not agent work.

The agent sees nodes and records supported edges with source citations. Program
code derives containers and unresolved nodes afterwards. It does not ask the
agent to preview containers, demote candidates, or copy object-id checklists.
"""
from __future__ import annotations

import json

from ..tools import Tool, ToolError
from .common import finish_tool
from .episodes import EpisodeBuilder, audit
from .ordering import EventIndex, compare

ORGANIZATION_SYSTEM_PROMPT = """你的唯一核心任务是：依据原始病历，为发现阶段 LLM 提议的候选管理表述和程序检查证据对象建立关系。
输入是节点；你的输出是节点之间的边及边上的说明。不要重新发现候选，不改变原文范围，不组织episode或医生材料。
候选与证据都不是临床真值、专家Reference或已确认独立Checkpoint。尽量用原文解决可核对的关系，无法确定就保留不确定，不强行连线。

候选span已是定位好的原文。直接依据list_events中span比较候选；只有判断某条具体关系确有必要时才读取局部原文，不要逐份通读病历。
先加载case_organization技能。看到明确关系立即record_relation，不要等读完所有来源才开始写边。每条边写清关系类型、置信度、简短理由；需要依据的关系必须附两端逐字原文。
same_event只用于严格重复身份；contains/overlap保留组成/重叠，不是same_event；same_management_process只在原文支持同一管理目标时用supported。
evidence_used的方向只能是既往evidence_artifact→后续管理候选。compare_clocks只比较资料检索时钟，不是临床选择时刻；未知或同刻不得编造先后。
需要否定某条已记录的直接过程边时用none/no_link并附依据，不删除历史。证据和组成关系不合并过程；context_only/uncertain不合并过程。
不要执行容器预览、候选降级或逐项ID覆盖清单，这些不是本环节Agent的职责。完成关系记录后调用finish。"""


def organization_relation_tools(registry, *, events, source_index):
    """Record typed relations; a citation check is not a semantic confirmation."""
    known = {event["event_id"]: event for event in events}
    relations, demotions = [], []
    positive = {"same_event", "same_management_process", "contains", "overlap",
                "evidence_used"}
    management_relations = positive - {"evidence_used"}

    def relation(source_event_id, target_event_id, relation, basis_quotes=None,
                 confidence="uncertain", explanation=""):
        source, target = known.get(source_event_id), known.get(target_event_id)
        if source is None:
            raise ToolError("UNKNOWN_EVENT_ID:" + str(source_event_id))
        if target is None:
            raise ToolError("UNKNOWN_EVENT_ID:" + str(target_event_id))
        if source_event_id == target_event_id:
            raise ToolError("SELF_RELATION_NOT_ALLOWED")
        if relation not in positive | {"none"}:
            raise ToolError("RELATION_KIND_INVALID:" + str(relation))
        source_evidence = source.get("object_kind") == "evidence_artifact"
        target_evidence = target.get("object_kind") == "evidence_artifact"
        if relation in management_relations and (source_evidence or target_evidence):
            raise ToolError("EVIDENCE_ARTIFACT_NOT_MANAGEMENT_ENDPOINT")
        if source.get("status") not in ("ACCEPTED", "SOURCE_INDEXED") \
                or target.get("status") not in ("ACCEPTED", "SOURCE_INDEXED"):
            raise ToolError("RELATION_ENDPOINT_SPAN_NOT_VERIFIED")
        if source.get("object_kind") != "evidence_artifact" \
                and source.get("record_function") == "execution" \
                and target.get("record_function") == "new_decision":
            raise ToolError("RELATION_DIRECTION_EXECUTION_TO_PLAN_INVALID")
        if relation == "evidence_used":
            if not source_evidence or target_evidence:
                raise ToolError(
                    "EVIDENCE_USE_DIRECTION_INVALID: source_event_id必须是既往证据对象，"
                    "target_event_id必须是后续管理候选。")
            if confidence not in ("supported", "uncertain"):
                raise ToolError("EVIDENCE_USE_CONFIDENCE_INVALID")
            time_relation = compare(source, target)["relation"]
            if time_relation not in ("before", "unknown", "same_time_unresolved"):
                raise ToolError(
                    "EVIDENCE_USE_CLOCK_DIRECTION_INVALID: 程序检索时钟不支持该证据先于后续判断；"
                    "保留候选并核对来源，不能改写选择时刻。")
            entry_clock = dict(retrieval_clock_comparison=time_relation,
                               comparison_not_choice_time=True)
        else:
            entry_clock = {}
        if relation == "none":
            if confidence != "no_link":
                raise ToolError("DIRECT_PROCESS_REFUTATION_REQUIRES_NO_LINK")
            if source_evidence or target_evidence:
                raise ToolError("EVIDENCE_ARTIFACT_NOT_MANAGEMENT_ENDPOINT")
        if confidence not in ("supported", "context_only", "uncertain", "no_link"):
            raise ToolError("RELATION_CONFIDENCE_INVALID")
        needs_basis = (relation in positive and confidence in ("supported", "context_only")) \
            or (relation == "none" and confidence == "no_link")
        if needs_basis:
            if not basis_quotes or not explanation.strip():
                raise ToolError("RELATION_REQUIRES_BASIS_AND_EXPLANATION")
            for endpoint in (source, target):
                span = endpoint.get("span") or {}
                quote = span.get("quote")
                if not isinstance(quote, str) or not quote:
                    raise ToolError("RELATION_BASIS_REQUIRES_EXACT_ENDPOINT_SPAN:"
                                    + endpoint["event_id"])
                # The quote must be the candidate/evidence object's own full
                # located span. Another sentence in the same source can be
                # clinically unrelated and is not sufficient relation support.
                expected = (span.get("source_id"), quote)
                if not any((citation.get("source_id"), citation.get("quote")) == expected
                           for citation in basis_quotes):
                    raise ToolError("RELATION_BASIS_NOT_ENDPOINT_SPAN:"
                                    + endpoint["event_id"])
            for endpoint in (source, target):
                if not any(citation.get("source_id") in (endpoint.get("source_ids") or ())
                           for citation in basis_quotes):
                    raise ToolError("RELATION_BASIS_MISSING_ENDPOINT:" + endpoint["event_id"])
            for citation in basis_quotes:
                verdict = source_index.check_quote(citation, label="organization_basis")
                if not verdict.get("ok"):
                    raise ToolError("RELATION_BASIS_NOT_VERIFIED:" + str(verdict.get("reason")))
                if not any(citation.get("source_id") in (endpoint.get("source_ids") or ())
                           for endpoint in (source, target)):
                    raise ToolError("RELATION_BASIS_OUTSIDE_ENDPOINT_SOURCES")
        entry = dict(source_event_id=source_event_id, target_event_id=target_event_id,
                     relation=relation, basis_quotes=list(basis_quotes or ()),
                     confidence=confidence, explanation=explanation, **entry_clock)
        if relation == "none":
            entry["refutes"] = "direct_same_management_process_only"
        relations.append(entry)
        return dict(recorded=True, index=len(relations) - 1, relation=entry,
                    note="程序已核验端点与引文；这不等于关系语义或episode边界已获临床确认。")

    quote_schema = dict(type="array", items=dict(
        type="object", additionalProperties=False, required=["source_id", "quote"],
        properties=dict(source_id=dict(type="string"), quote=dict(type="string"))))
    registry.register(Tool(
        name="record_relation",
        description=("记录病例级对象关系。evidence_used方向为既往证据→后续判断；"
                     "contains/overlap不合并过程；context_only不合并过程。"),
        input_schema=dict(type="object", additionalProperties=False,
                          required=["source_event_id", "target_event_id", "relation", "confidence"],
                          properties=dict(
                              source_event_id=dict(type="string"), target_event_id=dict(type="string"),
                              relation=dict(type="string", enum=sorted(positive | {"none"})),
                              basis_quotes=quote_schema,
                              confidence=dict(type="string", enum=[
                                  "supported", "context_only", "uncertain", "no_link"]),
                              explanation=dict(type="string"))),
        run=relation, writes=True))
    return dict(relations=relations, demotions=demotions)


def build_prompt(events, *, case_id):
    return """## 病例 %s 的候选关系记录

管理候选与程序证据索引共 %d 个对象。你的任务只是连边，不是组织episode。
1. list_events(limit=200)读取候选span和证据索引；这些已是可直接比较的原文。
2. 发现明确关系就立即record_relation，不要先逐份read_unit/read_source通读病历。
3. 只有一条具体关系缺少必要上下文时才检索局部原文；不需要读每个对象的rationale或全部单位。
4. 在边上写清关系meta：类型、confidence、理由和必要的两端原文。时间未知就保留unknown。
5. 没有可支持连接的候选自然保留为孤立节点；记录完有依据的边后直接finish。
""" % (case_id, len(events))


def recovery_context(relations):
    """Expose durable edge IDs, not a request to repeat all prior work."""
    pairs = [[item["source_event_id"], item["target_event_id"], item["relation"]]
             for item in relations]
    return ("上一次网络传输失败。以下关系已由程序保存，不要重复记录；从其他候选继续：\n"
            + json.dumps(pairs, ensure_ascii=False))


__all__ = ["EventIndex", "ORGANIZATION_SYSTEM_PROMPT", "EpisodeBuilder", "audit",
           "finish_tool", "build_prompt", "organization_relation_tools", "recovery_context"]
