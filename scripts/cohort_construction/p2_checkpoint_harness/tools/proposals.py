"""Discovery writes a bounded candidate expression, not a clinical event.

A proposal names one contiguous expression in one immutable source. The program
locates its exact character range. ``scope`` records the discovery LLM's research
relevance judgment; it is not an expert verdict, an episode, or a decision-time
claim. Evidence organization and presentation/masking are separate consumers.
"""
from __future__ import annotations

import hashlib
import json

from .registry import Tool, ToolError


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class ProposalStore:
    """Proposals for one program reading task, keyed by local revision id."""

    def __init__(self, *, source_index, clock_check=None, unit_id=None, unit_kind=None):
        self.index = source_index
        self.clock_check = clock_check  # retained constructor compatibility
        self.unit_id = unit_id
        self.unit_kind = unit_kind
        self.proposals = {}
        self.revisions = []
        self.diagnostic_reviews = {}
        self.diagnostic_revisions = []

    def locate(self, source_id, quote):
        """Return every exact occurrence of ``quote`` in the named source."""
        source = self.index.require(source_id)
        text = source.get("text") or ""
        if not isinstance(quote, str) or not quote:
            raise ToolError("QUOTE_REQUIRED")
        spans = []
        cursor = 0
        while True:
            start = text.find(quote, cursor)
            if start < 0:
                break
            spans.append([start, start + len(quote)])
            cursor = start + 1
        return source, spans

    def check_quote(self, citation, *, label):
        """Locate a citation; ambiguous text keeps every occurrence.

        This shared relation/demotion check deliberately retains all locations
        for repeated text. A discovery candidate, in contrast, must select one
        location using ``start`` in ``_span_verdict``.
        """
        if not isinstance(citation, dict):
            return dict(label=label, ok=False, reason="CITATION_NOT_OBJECT")
        source_id = citation.get("source_id")
        quote = citation.get("quote")
        try:
            source, spans = self.locate(source_id, quote)
        except ToolError as exc:
            return dict(label=label, ok=False, reason=str(exc), source_id=source_id)
        verdict = dict(label=label, source_id=source_id,
                       source_sha256=source.get("source_sha256"),
                       quote_characters=len(quote or ""))
        if not spans:
            verdict.update(ok=False, reason="QUOTE_NOT_IN_SOURCE")
            return verdict
        if len(spans) > 1:
            verdict.update(ok=True, reason="QUOTE_AMBIGUOUS_KEEP_ALL_SPANS",
                           occurrences=spans,
                           guidance=("同一文字在来源中出现多次。关系核验保留全部位置，"
                                     "不替你挑一个；候选片段必须用 start 指定一个位置。"))
            return verdict
        verdict.update(ok=True, reason="QUOTE_VERIFIED", start=spans[0][0],
                       end=spans[0][1])
        return verdict

    def _span_verdict(self, source_id, quote, start=None, end=None):
        """Resolve exactly one non-empty half-open Python character interval."""
        verdict = self.check_quote(dict(source_id=source_id, quote=quote),
                                   label="span")
        if not verdict.get("ok"):
            return verdict, None
        source, spans = self.locate(source_id, quote)
        if start is None and len(spans) > 1:
            verdict.update(ok=False, reason="SPAN_START_REQUIRED_FOR_REPEATED_QUOTE",
                           guidance="重复文字必须给 start；程序不会替你选择一次出现。")
            return verdict, None
        if start is None:
            start = spans[0][0]
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            verdict.update(ok=False, reason="SPAN_START_INVALID")
            return verdict, None
        expected_end = start + len(quote)
        if end is None:
            end = expected_end
        if isinstance(end, bool) or not isinstance(end, int) or end != expected_end:
            verdict.update(ok=False, reason="SPAN_END_MISMATCH",
                           expected_end=expected_end)
            return verdict, None
        if source.get("text", "")[start:end] != quote:
            verdict.update(ok=False, reason="SPAN_OFFSET_MISMATCH",
                           occurrences=spans, expected_end=expected_end)
            return verdict, None
        verdict.update(ok=True, reason="SPAN_VERIFIED", start=start, end=end,
                       occurrences=spans)
        return verdict, dict(source_id=source_id, quote=quote, start=start, end=end)

    def tool_propose_candidate(self):
        def run(local_id, scope, record_function, source_id, quote,
                start=None, end=None, rationale="", reason="", uncertainty="",
                action_type=None, target=None):
            if not isinstance(local_id, str) or not local_id.strip():
                raise ToolError("LOCAL_ID_REQUIRED")
            if scope not in ("yes", "no", "uncertain"):
                raise ToolError("SCOPE_INVALID")
            if scope == "no" and not str(reason).strip():
                raise ToolError("OUT_OF_SCOPE_REASON_REQUIRED")
            if scope == "uncertain" and not str(uncertainty).strip():
                raise ToolError("UNCERTAIN_SCOPE_REQUIRES_UNCERTAINTY")
            candidate_action = None
            if scope == "yes" and record_function in ("new_decision", "reassessment"):
                if not isinstance(action_type, str) or not action_type.strip():
                    raise ToolError("CANDIDATE_ACTION_TYPE_REQUIRED")
                if not isinstance(target, str) or not target.strip():
                    raise ToolError("CANDIDATE_ACTION_TARGET_REQUIRED")
                candidate_action = dict(action_type=action_type.strip(), target=target.strip())
            verdict, span = self._span_verdict(source_id, quote, start, end)
            record = dict(
                local_id=local_id, scope=scope, record_function=record_function,
                span=span or dict(source_id=source_id, quote=quote,
                                  start=start, end=end),
                rationale=rationale, reason=reason, uncertainty=uncertainty,
                candidate_action=candidate_action)
            record_id = _digest(record)[:20]
            record["attribution"] = self._attribution(record)
            problems = [] if verdict.get("ok") else [verdict]
            previous = self.proposals.get(local_id)
            if previous is not None:
                self.revisions.append(dict(
                    local_id=local_id, previous_id=previous["record_id"],
                    previous_status=previous["status"], reason="replaced"))
            self.proposals[local_id] = dict(
                record=record, record_id=record_id,
                status="ACCEPTED" if not problems else "PROPOSED_WITH_PROBLEMS",
                citation_verdicts=[verdict],
                revision=previous["record_id"] if previous else None)
            response = dict(
                local_id=local_id, record_id=record_id,
                status=self.proposals[local_id]["status"],
                accepted=not problems, span_verdict=verdict,
                note=("ACCEPTED 只表示原文片段可精确定位，不表示临床真值或专家接受。"
                      "scope=no 是发现阶段 LLM 的明确排除记录，保留原文索引但不进入后续"
                      "候选事件组织；不要求提交所有无关句子。"))
            if problems:
                response.update(problems=problems, guidance=(
                    "候选仍被保存。修正 source_id、逐字 quote 或偏移后重提同一 local_id；"
                    "重复 quote 必须指定 start，end 是开区间终点。不要为了通过校验删除候选。"))
            return response
        return Tool(
            name="propose_candidate",
            description=("提交一个候选管理判断表述或一条已识别但明确排除的管理相关表述。"
                         "quote 必须逐字来自同一 source_id 的连续原文；start/end 为字符区间。"
                         "这不是事件拆合、证据组织或 episode 工具。"),
            input_schema=dict(
                type="object", additionalProperties=False,
                required=["local_id", "scope", "record_function", "source_id", "quote"],
                properties=dict(
                    local_id=dict(type="string", minLength=1),
                    scope=dict(type="string", enum=["yes", "no", "uncertain"],
                               description="yes/no/uncertain 是发现阶段 LLM 的研究范围判断。"),
                    record_function=dict(type="string", enum=[
                        "new_decision", "reassessment", "repeat", "execution",
                        "generic", "uncertain"]),
                    source_id=dict(type="string", minLength=1),
                    quote=dict(type="string", minLength=1),
                    start=dict(type="integer", minimum=0),
                    end=dict(type="integer", minimum=0),
                    rationale=dict(type="string"),
                    reason=dict(type="string", description="scope=no 时的简短排除理由。"),
                    uncertainty=dict(type="string"),
                    action_type=dict(type="string", description=(
                        "scope=yes 且 new_decision/reassessment 时必填的动作类型。")),
                    target=dict(type="string", description=(
                        "scope=yes 且 new_decision/reassessment 时必填的动作目标。")))),
            run=run, writes=True)

    def tool_review_diagnostic_candidate(self):
        def run(candidate_kind, disposition, reason, action_type=None, target=None,
                management_question=None):
            if self.unit_kind != "performed_test_report":
                raise ToolError("DIAGNOSTIC_REVIEW_REQUIRES_PERFORMED_TEST_REPORT")
            if candidate_kind not in ("pre_exam_evaluation", "evidence_arrival_reassessment"):
                raise ToolError("DIAGNOSTIC_CANDIDATE_KIND_INVALID")
            if disposition not in ("yes", "no", "uncertain"):
                raise ToolError("DIAGNOSTIC_DISPOSITION_INVALID")
            if not isinstance(reason, str) or not reason.strip():
                raise ToolError("DIAGNOSTIC_REVIEW_REASON_REQUIRED")
            candidate_action = None
            if disposition == "yes":
                if not isinstance(action_type, str) or not action_type.strip():
                    raise ToolError("DIAGNOSTIC_ACTION_TYPE_REQUIRED")
                if not isinstance(target, str) or not target.strip():
                    raise ToolError("DIAGNOSTIC_ACTION_TARGET_REQUIRED")
                candidate_action = dict(action_type=action_type.strip(), target=target.strip())
            review_id = "DR-" + _digest([self.unit_id, candidate_kind])[:16]
            previous = self.diagnostic_reviews.get(candidate_kind)
            record = dict(
                review_id=review_id, candidate_kind=candidate_kind,
                disposition=disposition, reason=reason.strip(),
                candidate_action=candidate_action,
                management_question=(management_question.strip()
                                     if isinstance(management_question, str)
                                     and management_question.strip() else None))
            record_id = _digest(record)[:20]
            if previous is not None:
                self.diagnostic_revisions.append(dict(
                    review_id=review_id, candidate_kind=candidate_kind,
                    previous_record_id=previous["record_id"],
                    previous_disposition=previous["record"]["disposition"],
                    previous_record=dict(previous["record"]), reason="replaced"))
            self.diagnostic_reviews[candidate_kind] = dict(
                record=record, record_id=record_id, status="REVIEWED",
                revision=previous["record_id"] if previous else None)
            return dict(
                review_id=review_id, record_id=record_id,
                candidate_kind=candidate_kind, disposition=disposition,
                candidate_action=candidate_action,
                status="REVIEWED", revision=self.diagnostic_reviews[candidate_kind]["revision"],
                note=("这只保存 Discovery LLM 对程序检查候选的筛选与修订，"
                      "不是专家真值、已确认临床决策点或原文中的管理句。"))
        return Tool(
            name="review_diagnostic_candidate",
            description=("审核当前 performed_test_report 的一个程序检查候选。"
                         "两类候选各提交一次；yes 必须给动作类型和目标，no/uncertain 也必须有理由。"
                         "unit_id 由程序绑定，不能由 Agent 指定。"),
            input_schema=dict(
                type="object", additionalProperties=False,
                required=["candidate_kind", "disposition", "reason"],
                properties=dict(
                    candidate_kind=dict(type="string", enum=[
                        "pre_exam_evaluation", "evidence_arrival_reassessment"]),
                    disposition=dict(type="string", enum=["yes", "no", "uncertain"]),
                    reason=dict(type="string", minLength=1),
                    action_type=dict(type="string"),
                    target=dict(type="string"),
                    management_question=dict(type="string"))),
            run=run, writes=True)

    def _attribution(self, record):
        """A candidate belongs to its one cited source, not its reading task."""
        span = record.get("span") or {}
        source_ids = [span["source_id"]] if span.get("source_id") else []
        source = self.index.by_id.get(source_ids[0]) if source_ids else None
        units = list(self.index.units_by_id.values())
        owner = next((unit for unit in units
                      if source_ids and unit.get("source_ids") == source_ids), None)
        problems = [] if source is not None else ["SPAN_ORIGIN_REQUIRED"]
        return dict(
            origin_source_ids=source_ids, origin_role="span",
            origin_unit_id=(owner or {}).get("unit_id"),
            origin_unit_kind=(owner or {}).get("kind"),
            origin_document_profile=(owner or {}).get("document_profile"),
            clock=(source or {}).get("clock"),
            clock_basis=(source or {}).get("clock_basis"), problems=problems)

    def events(self):
        """Compatibility name for pipeline collection; entries are candidates."""
        return [dict(entry) for key, entry in sorted(self.proposals.items())]

    def rejected(self):
        return [entry for entry in self.events()
                if entry["record"].get("scope") == "no"]

    def diagnostic_review_events(self):
        return [dict(entry) for _, entry in sorted(self.diagnostic_reviews.items())]

    def summary(self):
        events = self.events()
        reviews = self.diagnostic_review_events()
        required = self.unit_kind == "performed_test_report"
        reviewed_kinds = {entry["record"]["candidate_kind"] for entry in reviews}
        expected_kinds = {"pre_exam_evaluation", "evidence_arrival_reassessment"}
        return dict(
            proposals=len(events),
            accepted=sum(1 for event in events if event["status"] == "ACCEPTED"),
            with_problems=sum(1 for event in events
                              if event["status"] != "ACCEPTED"),
            revisions=len(self.revisions),
            in_scope=sum(1 for event in events if event["record"]["scope"] == "yes"),
            out_of_scope=sum(1 for event in events if event["record"]["scope"] == "no"),
            uncertain_scope=sum(1 for event in events
                                if event["record"]["scope"] == "uncertain"),
            rejected_mentions=len(self.rejected()),
            diagnostic_review_required=required,
            diagnostic_candidate_reviews=len(reviews),
            diagnostic_yes=sum(1 for entry in reviews
                               if entry["record"]["disposition"] == "yes"),
            diagnostic_no=sum(1 for entry in reviews
                              if entry["record"]["disposition"] == "no"),
            diagnostic_uncertain=sum(1 for entry in reviews
                                     if entry["record"]["disposition"] == "uncertain"),
            diagnostic_revisions=len(self.diagnostic_revisions),
            diagnostic_review_complete=(not required or reviewed_kinds == expected_kinds),
            diagnostic_missing_candidate_kinds=(sorted(expected_kinds - reviewed_kinds)
                                                if required else []))


_CITATION = dict(
    type="object", additionalProperties=False, required=["source_id", "quote"],
    properties=dict(source_id=dict(type="string", minLength=1),
                    quote=dict(type="string", minLength=1),
                    start=dict(type="integer", minimum=0)))


def register(registry, store):
    registry.register(store.tool_propose_candidate())
    registry.register(store.tool_review_diagnostic_candidate())
    return registry
