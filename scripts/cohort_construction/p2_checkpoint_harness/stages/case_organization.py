"""One case-level organization stage for located management spans and evidence.

The former ``ordering`` module remains the legacy comparison baseline. This stage
owns the full object graph before assembly: evidence is visible, evidence-use is
directed from a previous artifact to a later management expression, and only a
supported same-process relation merges a candidate container.
"""
from __future__ import annotations

from ..tools import Tool, ToolError
from .ordering import EventIndex, compare
from .episodes import EpisodeBuilder, audit, episode_finish_tool

ORGANIZATION_SYSTEM_PROMPT = """你在一次病例级组织中整理发现阶段 LLM 提议的候选管理表述，以及程序建立的检查证据索引。
这些都不是临床真值、专家Reference或已确认独立episode。你要尽量用原文完成可核对的组织，
而不是把能在原文中解决的问题留给人工。不得重新执行发现阶段的范围分类；只有原文明确推翻候选
解释时，才可降级并附引文。

先用list_events查看全部对象；evidence_artifact与管理候选同样在本阶段可见。按需read_event、
read_unit、read_source/search_sources核对完整来源。compare_clocks只比较资料检索时钟，不是临床选择时刻。
source_event_id必须是既往evidence_artifact，target_event_id是后续管理候选，关系用evidence_used；
时间可比时程序会拒绝反向，时间未知必须保留不确定，不能编造先后。

same_event只用于严格重复身份。组合候选与原子候选分别用contains或overlap记录，不得冒充same_event；
它们只保留组成/重叠信息，不自动合并过程。只有confidence=supported的same_management_process会
合并候选过程容器；context_only保留为未解释的上下文关系，uncertain不合并。
same_management_process须说明同一管理目标的原文依据，同日、同次查房、同标题或同住院本身不够。
既有关系不可删除；否定某条直接过程边用none/no_link并附两端原文，其他证据路径和候选仍保留。

先记录有依据的关系，再preview_episodes检查所有传递合并后的容器，必要时修正并再次预览。
对全部管理候选和证据对象逐项声明覆盖；每个证据对象均须evidence_checks。最后调用finish。
先加载case_organization技能。"""


def organization_relation_tools(registry, *, events, source_index, allow_demote=True):
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

    def demote(event_id, reason, basis_quotes=None, to_status="not_a_checkpoint"):
        if not allow_demote:
            raise ToolError("DEMOTION_NOT_PERMITTED_IN_THIS_TURN")
        event = known.get(event_id)
        if event is None or event.get("object_kind") == "evidence_artifact":
            raise ToolError("MANAGEMENT_CANDIDATE_REQUIRED")
        if not reason.strip() or not basis_quotes:
            raise ToolError("DEMOTION_REQUIRES_BASIS")
        for citation in basis_quotes:
            verdict = source_index.check_quote(citation, label="demotion_basis")
            if not verdict.get("ok"):
                raise ToolError("DEMOTION_BASIS_NOT_VERIFIED:" + str(verdict.get("reason")))
        entry = dict(event_id=event_id, reason=reason,
                     basis_quotes=list(basis_quotes), to_status=to_status)
        demotions.append(entry)
        return dict(recorded=True, demotion=entry)

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
    if allow_demote:
        registry.register(Tool(
            name="demote_event", description="用可定位原文给候选管理表述添加降级标注，不删除候选。",
            input_schema=dict(type="object", additionalProperties=False,
                              required=["event_id", "reason"],
                              properties=dict(event_id=dict(type="string"), reason=dict(type="string"),
                                              basis_quotes=quote_schema,
                                              to_status=dict(type="string", enum=[
                                                  "not_a_checkpoint", "repeat", "out_of_scope", "uncertain"]))),
            run=demote, writes=True))
    return dict(relations=relations, demotions=demotions)


def build_prompt(events, *, case_id):
    return """## 病例 %s 的一次病例级组织

管理候选与程序证据索引共 %d 个对象。
1. list_events(limit=200)查看全部对象，read_event/read_unit核对候选完整表述及上下文。
2. 记录严格身份、包含/重叠、证据使用和支持型过程关系；不要把所有对象强行配对或强行连成一条路径。
3. 只有原文支持同一管理目标时才用supported same_management_process；仅有上下文时用context_only。
4. preview_episodes检查传递合并的实际结果；不合适的直接边用none/no_link修正后再次预览。
5. 孤立候选须区分“没有记录到直接连接”和“与研究无关”；证据对象须逐项evidence_checks。
6. 调用finish，列出全部reviewed_event_ids。finish是已执行审阅的程序回执，不是临床正确性证明。
""" % (case_id, len(events))


__all__ = ["EventIndex", "ORGANIZATION_SYSTEM_PROMPT", "EpisodeBuilder", "audit",
           "episode_finish_tool", "build_prompt", "organization_relation_tools"]
