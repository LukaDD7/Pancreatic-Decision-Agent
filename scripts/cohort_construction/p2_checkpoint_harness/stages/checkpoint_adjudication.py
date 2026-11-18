"""Adjudicate management candidate Checkpoints against prior evidence states.

The case-level agent reviews the complete provisional CP projection. Its output
remains a candidate audit: it neither confirms clinical decisions nor creates a
clinical Reference. Exact citations, clock direction and coverage are checked by
program code rather than delegated to the model's rationale.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import re

from ..tools import Tool, ToolError
from ..tools.proposals import ProposalStore
from ..tools.sources import SourceIndex
from .ordering import compare


ADJUDICATION_FORMAT = "p2-checkpoint-adjudication-0.1.0"
MANAGEMENT_KIND = "MANAGEMENT_REASSESSMENT"
RETAIN = "RETAIN_CP"
SUPPORT = "SUPPORT_ONLY"
REVIEW = "REVIEW_REQUIRED"
DISPOSITIONS = {RETAIN, SUPPORT, REVIEW}
EVOLUTIONS = {
    "FIRST_VISIBLE", "NEW_EVIDENCE_ROUND", "SAME_ROUND_SUPPORT",
    "REPEATED_PLAN", "EXECUTION", "DIFFERENT_TASK", "UNCERTAIN",
}
SUPPORT_EVOLUTIONS = {"SAME_ROUND_SUPPORT", "REPEATED_PLAN", "EXECUTION"}
INTERVAL_STATUSES = {
    "READ_AND_USED", "READ_NOT_NEW", "SKIPPED_FROM_INDEX", "UNCERTAIN",
}
INTERVAL_READ_STATUSES = {"READ_AND_USED", "READ_NOT_NEW"}
ACTOR = "case_level_adjudication_llm"

ADJUDICATION_SYSTEM_PROMPT = """你在完整Discovery和离线候选投影之后，审核一个病例的全部候选Checkpoint（CP）是否体现患者证据演化。
这些CP、动作和裁决都只是研究候选，不是临床真值、专家Reference或已确认的独立临床决策点。不要重新发现候选，不要修改CP或原文。

先用list_candidate_checkpoints逐页查看全部candidate_checkpoints。诊断CP不需要裁决，每个MANAGEMENT_REASSESSMENT必须恰好调用一次adjudicate_checkpoint。每个管理CP先调用read_checkpoint_adjudication_context：程序一次返回当前CP详情、唯一最近保留comparator及其详情，不需要也不允许Agent自行选择comparator。提交任何裁决前，必须read_source读取至少一份当前来源原文；若程序返回的comparator是诊断CP，还必须read_source读取diagnostic_target_source_ids中至少一个目标报告原文。程序会拒绝未读取当前CP、比较对象或目标报告的提交，不能用其他管理文书代替。

list_checkpoint_interval_sources只作为可选诊断工具：仅当两个CP之间是否出现新检查、病理或其他患者证据仍不清楚时，才查看区间索引并按需read_source；不要求逐页查看或逐项处置全部区间资料。不要因为索引中有一个管理动作或检查名称就认定证据演化，仍需核对相关原文。

裁决规则：
1. RETAIN_CP+FIRST_VISIBLE仅用于全病例没有任何其他可见候选状态的首见锚点；同刻候选也不能被忽略。
2. 按检索clock从早到晚裁决。除首个可见锚点外，程序自动将唯一最近一个已保留CP状态作为comparator；不能跳过中间CP选择更早的比较对象。诊断CP是已保留状态，后续管理CP不能无视它而自称FIRST_VISIBLE。同刻、未知或并列最近状态不可定序时用REVIEW_REQUIRED+UNCERTAIN。
3. 相对程序给定的最近保留状态出现了新的相关患者证据，才用RETAIN_CP+NEW_EVIDENCE_ROUND，并至少给一条evidence_support。证据必须严格晚于comparator、且在该CP可见clock之前或当时已经可见，不能只引用当前CP的动作、计划或管理句。诊断CP已经包含的目标报告不得再次作为新增证据；后续文书中带检查日期的旧报告复述仍属于原报告，必须回到原始报告判断时间，不能因复述文书较晚而变成新证据。BEFORE_ACQUISITION comparator的目标报告可能晚于当前CP，强制读取只为识别比较对象，绝不代表报告在当前时点可见。
4. 动作更具体、新计划、转科、复述、执行本身都不是证据演化。CT后CP已可输出EUS+FNA时，后续转入记录再次写EUS+FNA或择期手术，应SUPPORT_ONLY+SAME_ROUND_SUPPORT挂到CT后CP，而不是另建证据演化轮次。复述用REPEATED_PLAN，执行用EXECUTION。
5. SUPPORT_ONLY由程序自动挂到最近保留CP，evolution只能是SAME_ROUND_SUPPORT/REPEATED_PLAN/EXECUTION。
6. 时间缺失、先后无法确定或证据关系无法确定时用REVIEW_REQUIRED+UNCERTAIN，不要为了通过校验猜测时间或编造引文。不同任务但没有新增证据也不能冒充NEW_EVIDENCE_ROUND。

evidence_supports只提供source_id和逐字、连续来自该来源的quote，不提供start或end；若quote重复，请扩大引用上下文使其唯一。程序会定位quote、核验原文并生成字符半开区间。程序会拒绝非最近比较状态、比较状态以前的旧证据、未来证据、无法定位的引文，以及和当前CP member candidate span重叠的引文。逐项提交后立即继续，不要反复重新列出全部CP；完成全部管理CP后调用finish，未知、重复或漏裁决会在finish报错。"""


def _event_id(candidate, index):
    return candidate.get("event_id") or candidate.get("record_id") or (
        "candidate-%d" % index)


def _clock_value(item):
    return (item.get("clock") or {}).get("value")


class AdjudicationCheckpointIndex:
    """Read-only CP and member-candidate view for one provisional projection."""

    def __init__(self, checkpoints, candidates):
        self.checkpoints = list(checkpoints)
        self.by_id = {}
        self.returned_checkpoint_ids = set()
        self.read_checkpoint_ids = set()
        self.limitations = []
        self.member_spans = {}
        self.management_member_spans = {}
        self.member_span_problems = {}
        candidates_by_id = {}
        for position, candidate in enumerate(candidates):
            event_id = _event_id(candidate, position)
            if event_id in candidates_by_id:
                raise ValueError("DUPLICATE_CANDIDATE_EVENT_ID:" + event_id)
            candidates_by_id[event_id] = candidate
        self.candidates_by_id = candidates_by_id
        for checkpoint in self.checkpoints:
            checkpoint_id = checkpoint.get("checkpoint_id")
            if not isinstance(checkpoint_id, str) or not checkpoint_id:
                raise ValueError("CANDIDATE_CHECKPOINT_ID_REQUIRED")
            if checkpoint_id in self.by_id:
                raise ValueError("DUPLICATE_CANDIDATE_CHECKPOINT_ID:" + checkpoint_id)
            self.by_id[checkpoint_id] = checkpoint
            self._index_member_spans(checkpoint)

    @property
    def management_ids(self):
        return [checkpoint["checkpoint_id"] for checkpoint in self.checkpoints
                if checkpoint.get("kind") == MANAGEMENT_KIND]

    def require(self, checkpoint_id):
        checkpoint = self.by_id.get(checkpoint_id)
        if checkpoint is None:
            raise ToolError("UNKNOWN_CHECKPOINT_ID:" + str(checkpoint_id))
        return checkpoint

    def _limitation(self, checkpoint_id, event_id, code):
        problem = dict(checkpoint_id=checkpoint_id, member_event_id=event_id,
                       code=code)
        self.limitations.append(problem)
        self.member_span_problems.setdefault(checkpoint_id, []).append(problem)

    def _index_member_spans(self, checkpoint):
        checkpoint_id = checkpoint["checkpoint_id"]
        spans = []
        for event_id in checkpoint.get("member_event_ids") or ():
            candidate = self.candidates_by_id.get(event_id)
            if candidate is None:
                self._limitation(checkpoint_id, event_id,
                                 "MEMBER_CANDIDATE_NOT_SUPPLIED")
                continue
            span = deepcopy(candidate.get("span") or {})
            source_id = span.get("source_id")
            start, end, quote = span.get("start"), span.get("end"), span.get("quote")
            valid = (isinstance(source_id, str) and source_id
                     and isinstance(quote, str) and quote
                     and type(start) is int and type(end) is int
                     and 0 <= start < end and end == start + len(quote))
            if not valid:
                self._limitation(checkpoint_id, event_id,
                                 "MEMBER_CANDIDATE_SPAN_INVALID")
                continue
            spans.append(span)
        self.member_spans[checkpoint_id] = spans
        if checkpoint.get("kind") == MANAGEMENT_KIND:
            self.management_member_spans[checkpoint_id] = spans

    def diagnostic_target_source_ids(self, checkpoint):
        return list(checkpoint.get("source_ids") or ())

    def _member_candidate(self, event_id):
        candidate = self.candidates_by_id[event_id]
        return dict(
            event_id=event_id, record_function=candidate.get("record_function"),
            clock=deepcopy(candidate.get("clock") or {}),
            candidate_action=deepcopy(candidate.get("candidate_action") or {}),
            span=deepcopy(candidate.get("span") or {}))

    def _summary(self, checkpoint):
        result = dict(
            checkpoint_id=checkpoint["checkpoint_id"], kind=checkpoint.get("kind"),
            clock=deepcopy(checkpoint.get("clock") or {}),
            clock_basis=checkpoint.get("clock_basis"),
            time_basis=checkpoint.get("time_basis"),
            source_ids=list(checkpoint.get("source_ids") or ()),
            member_event_ids=list(checkpoint.get("member_event_ids") or ()),
            diagnostic_event_ids=list(checkpoint.get("diagnostic_event_ids") or ()))
        if checkpoint.get("kind") != MANAGEMENT_KIND:
            result.update(
                candidate_actions=deepcopy(checkpoint.get("candidate_actions") or []),
                diagnostic_target_source_ids=self.diagnostic_target_source_ids(checkpoint))
        return result

    def detail(self, checkpoint_id):
        checkpoint = self.require(checkpoint_id)
        self.read_checkpoint_ids.add(checkpoint_id)
        result = self._summary(checkpoint)
        result.update(
            support_source_ids=list(checkpoint.get("support_source_ids") or ()),
            supporting_event_ids=list(checkpoint.get("supporting_event_ids") or ()),
            candidate_actions=deepcopy(checkpoint.get("candidate_actions") or []),
            masked_spans=deepcopy(checkpoint.get("masked_spans") or []),
            review_status=checkpoint.get("review_status"),
            candidate_status=checkpoint.get("candidate_status"),
            management_question=checkpoint.get("management_question"),
            member_candidates=[
                self._member_candidate(event_id)
                for event_id in checkpoint.get("member_event_ids") or ()
                if event_id in self.candidates_by_id],
            member_span_limitations=deepcopy(
                self.member_span_problems.get(checkpoint_id, [])))
        return result

    def tools(self):
        def list_candidate_checkpoints(kind=None, source_id=None, offset=0, limit=100):
            rows = [checkpoint for checkpoint in self.checkpoints
                    if (kind is None or checkpoint.get("kind") == kind)
                    and (source_id is None
                         or source_id in (checkpoint.get("source_ids") or ()))]
            rows.sort(key=lambda item: (_clock_value(item) or "9999",
                                        item["checkpoint_id"]))
            offset = max(0, int(offset))
            limit = max(1, min(200, int(limit)))
            page = rows[offset:offset + limit]
            self.returned_checkpoint_ids.update(item["checkpoint_id"] for item in page)
            return dict(total=len(rows), offset=offset, returned=len(page),
                        truncated=offset + len(page) < len(rows),
                        checkpoints=[self._summary(item) for item in page],
                        note=("候选CP及其Discovery动作均待审核；诊断CP不需要本阶段裁决，"
                              "管理CP必须逐项裁决。"))

        def read_candidate_checkpoint(checkpoint_id):
            return self.detail(checkpoint_id)

        def compare_checkpoint_clocks(left_checkpoint_id, right_checkpoint_id):
            return compare(self.require(left_checkpoint_id),
                           self.require(right_checkpoint_id))

        return [
            Tool(name="list_candidate_checkpoints",
                 description=("逐页列出全部候选CP的ID、kind、clock、member_event_ids和source_ids；"
                              "动作及span只在read_candidate_checkpoint按需读取。"),
                 input_schema=dict(type="object", additionalProperties=False,
                                   properties=dict(
                                       kind=dict(type="string"),
                                       source_id=dict(type="string"),
                                       offset=dict(type="integer", minimum=0),
                                       limit=dict(type="integer", minimum=1, maximum=200))),
                 run=list_candidate_checkpoints),
            Tool(name="read_candidate_checkpoint",
                 description="读取一个CP及其member candidate的原始span、动作和时钟。",
                 input_schema=dict(type="object", additionalProperties=False,
                                   required=["checkpoint_id"],
                                   properties=dict(checkpoint_id=dict(type="string"))),
                 run=read_candidate_checkpoint),
            Tool(name="compare_checkpoint_clocks",
                 description="比较两个CP的检索时钟；未知或同刻未决不能冒充严格更早。",
                 input_schema=dict(type="object", additionalProperties=False,
                                   required=["left_checkpoint_id", "right_checkpoint_id"],
                                   properties=dict(left_checkpoint_id=dict(type="string"),
                                                   right_checkpoint_id=dict(type="string"))),
                 run=compare_checkpoint_clocks),
        ]


class CheckpointAdjudicationStore:
    """Validated adjudications, keyed once per management candidate CP."""

    def __init__(self, *, checkpoint_index, source_index):
        self.checkpoints = checkpoint_index
        self.source_index = source_index
        self.proposals = ProposalStore(source_index=source_index)
        self.items = {}
        self.retained_checkpoint_ids = {
            checkpoint_id for checkpoint_id, checkpoint in self.checkpoints.by_id.items()
            if checkpoint.get("kind") != MANAGEMENT_KIND}
        self.attempt_problems = []
        self.interval_listed = {}
        self.interval_reviews = {}
        self.interval_review_revisions = 0
        self.finished = False

    def _remember_problem(self, code, checkpoint_id):
        self.attempt_problems.append(dict(code=code, checkpoint_id=checkpoint_id))

    def _latest_retained(self, checkpoint):
        earlier = []
        for checkpoint_id in self.retained_checkpoint_ids:
            candidate = self.checkpoints.by_id[checkpoint_id]
            if compare(candidate, checkpoint)["relation"] == "before":
                earlier.append(candidate)
        latest = []
        for candidate in earlier:
            if not any(other["checkpoint_id"] != candidate["checkpoint_id"]
                       and compare(candidate, other)["relation"] == "before"
                       for other in earlier):
                latest.append(candidate)
        return {candidate["checkpoint_id"] for candidate in latest}

    def _unordered_states(self, checkpoint):
        problems = []
        for candidate in self.checkpoints.checkpoints:
            if candidate["checkpoint_id"] == checkpoint["checkpoint_id"]:
                continue
            relation = compare(candidate, checkpoint)["relation"]
            if relation in {"same_time_unresolved", "unknown"}:
                problems.append(dict(checkpoint_id=candidate["checkpoint_id"],
                                     relation=relation,
                                     retained=candidate["checkpoint_id"]
                                     in self.retained_checkpoint_ids))
        return problems

    def _require_prior_adjudications(self, checkpoint):
        pending = [candidate["checkpoint_id"] for candidate in self.checkpoints.checkpoints
                   if candidate.get("kind") == MANAGEMENT_KIND
                   and candidate["checkpoint_id"] not in self.items
                   and candidate["checkpoint_id"] != checkpoint["checkpoint_id"]
                   and compare(candidate, checkpoint)["relation"] == "before"]
        if pending:
            raise ToolError("PRIOR_MANAGEMENT_ADJUDICATION_REQUIRED:" + ",".join(pending))

    @staticmethod
    def _dated_recitation_payload(quote):
        match = re.match(
            r"^\s*\d{4}[-年]\d{1,2}[-月]\d{1,2}日?[^,，:：]*[,，:：]\s*(.+)$",
            quote or "", flags=re.DOTALL)
        if not match:
            return None
        payload = re.sub(r"\s+", "", match.group(1))
        return payload if len(payload) >= 12 else None

    def _require_original_for_dated_recitation(self, source, quote):
        if source.get("kind") != "document":
            return
        payload = self._dated_recitation_payload(quote)
        if not payload:
            return
        for original in self.source_index.sources:
            if original["source_id"] == source["source_id"] \
                    or original.get("kind") == "document":
                continue
            original_text = re.sub(r"\s+", "", original.get("text") or "")
            if payload in original_text:
                raise ToolError(
                    "SECONDARY_RESTATEMENT_REQUIRES_ORIGINAL_SOURCE: 引文是带检查日期的"
                    "旧报告复述，不能因出现在后一份文书中就成为新证据；请改为核对并引用"
                    "原始报告，旧报告仍需满足严格晚于comparator：" + original["source_id"])

    def _evidence(self, checkpoint, citations, *, disposition, evolution, comparator=None):
        if citations is None:
            citations = []
        if not isinstance(citations, list):
            raise ToolError("EVIDENCE_SUPPORTS_MUST_BE_LIST")
        if citations and self.checkpoints.member_span_problems.get(
                checkpoint["checkpoint_id"]):
            raise ToolError(
                "MEMBER_SPANS_NOT_VERIFIABLE: 当前CP有成员候选缺失或span无效，"
                "不能确认引文不重叠；请改用REVIEW_REQUIRED+UNCERTAIN且不提供该引文。")
        normalized = []
        for citation in citations:
            if not isinstance(citation, dict):
                raise ToolError("EVIDENCE_CITATION_NOT_OBJECT")
            verdict, span = self.proposals._span_verdict(
                citation.get("source_id"), citation.get("quote"),
                citation.get("start"), citation.get("end"))
            if not verdict.get("ok"):
                raise ToolError("EVIDENCE_SPAN_NOT_VERIFIED:"
                                + str(verdict.get("reason")))
            for member in self.checkpoints.member_spans.get(
                    checkpoint["checkpoint_id"], ()):
                if span["source_id"] == member["source_id"] and max(
                        span["start"], member["start"]) < min(span["end"], member["end"]):
                    raise ToolError(
                        "EVIDENCE_OVERLAPS_CURRENT_MEMBER: 当前动作或管理句不能作为新增患者证据。")
            if evolution == "NEW_EVIDENCE_ROUND":
                for member_checkpoint_id, members in self.checkpoints.management_member_spans.items():
                    if member_checkpoint_id == checkpoint["checkpoint_id"]:
                        continue
                    for member in members:
                        if span["source_id"] == member["source_id"] and max(
                                span["start"], member["start"]) < min(span["end"], member["end"]):
                            raise ToolError(
                                "EVIDENCE_OVERLAPS_MANAGEMENT_CANDIDATE: 其他CP或其他文书中的"
                                "管理动作句也不能作为新增患者证据。")
            source = self.source_index.require(span["source_id"])
            if evolution == "NEW_EVIDENCE_ROUND" and comparator is not None \
                    and comparator.get("kind") != MANAGEMENT_KIND \
                    and span["source_id"] in self.checkpoints.diagnostic_target_source_ids(comparator):
                raise ToolError(
                    "COMPARATOR_TARGET_CANNOT_BE_NEW_EVIDENCE: comparator已包含该目标报告，"
                    "同一报告不能再次形成证据演化轮次：" + span["source_id"])
            if evolution == "NEW_EVIDENCE_ROUND":
                self._require_original_for_dated_recitation(source, span["quote"])
            relation = compare(source, checkpoint)["relation"]
            if relation == "after":
                raise ToolError(
                    "EVIDENCE_CLOCK_AFTER_CHECKPOINT: 证据来源可见clock晚于当前CP。")
            if relation == "unknown" and not (
                    disposition == REVIEW and evolution == "UNCERTAIN"):
                raise ToolError(
                    "EVIDENCE_CLOCK_UNKNOWN_REQUIRES_REVIEW: 来源或CP可见clock未知，"
                    "请改用REVIEW_REQUIRED+UNCERTAIN。")
            if evolution == "NEW_EVIDENCE_ROUND" and comparator is not None:
                comparator_relation = compare(comparator, source)["relation"]
                if comparator_relation != "before":
                    raise ToolError(
                        "EVIDENCE_NOT_NEW_AFTER_COMPARATOR: 新增证据必须严格晚于最近保留的比较CP；"
                        "比较CP已有、同刻或时间未知的证据不能构成证据演化。")
            normalized.append(span)
        return normalized

    def read_adjudication_context(self, checkpoint_id):
        checkpoint = self.checkpoints.require(checkpoint_id)
        if checkpoint.get("kind") != MANAGEMENT_KIND:
            raise ToolError("ADJUDICATION_CONTEXT_REQUIRES_MANAGEMENT_CHECKPOINT")
        latest = sorted(self._latest_retained(checkpoint))
        comparator_id = latest[0] if len(latest) == 1 else None
        review_comparator_id = comparator_id
        if review_comparator_id is None and not latest:
            unresolved = [
                candidate for candidate in self.checkpoints.checkpoints
                if candidate["checkpoint_id"] != checkpoint_id
                and candidate.get("kind") != MANAGEMENT_KIND
                and compare(candidate, checkpoint)["relation"] in {
                    "same_time_unresolved", "unknown"}]
            if len(unresolved) == 1:
                review_comparator_id = unresolved[0]["checkpoint_id"]
        active_comparator = self.checkpoints.by_id.get(
            comparator_id or review_comparator_id)
        self.source_index.set_adjudication_context(checkpoint, active_comparator)
        return dict(
            checkpoint_id=checkpoint_id,
            current=self.checkpoints.detail(checkpoint_id),
            comparator_checkpoint_id=comparator_id,
            comparator=(self.checkpoints.detail(comparator_id)
                        if comparator_id else None),
            parallel_latest_checkpoint_ids=latest if len(latest) > 1 else [],
            review_comparator_checkpoint_id=review_comparator_id,
            review_comparator=(self.checkpoints.detail(review_comparator_id)
                               if review_comparator_id and review_comparator_id != comparator_id
                               else None),
            interval_review_required=False,
            evidence_policy=dict(
                comparator_targets_reusable_as_new_evidence=False,
                dated_secondary_restatement_requires_original_source=True,
                future_comparator_report_read_role="COMPARATOR_IDENTITY_ONLY"),
            note=("comparator由程序按唯一最近保留状态自动计算，不由Agent选择。"
                  "comparator已有的目标报告不得再次作为新增证据；其报告时间可能晚于当前CP，"
                  "读取仅用于识别comparator。后一份文书中的带日期旧报告复述不是新证据。"
                  "区间资料索引仅在有疑问时可选查看。"))

    def adjudication_context_tool(self):
        return Tool(
            name="read_checkpoint_adjudication_context",
            description=("一次读取当前CP和程序自动确定的最近保留comparator；"
                         "返回诊断目标source_ids。区间资料默认不强制核查。"),
            input_schema=dict(type="object", additionalProperties=False,
                              required=["checkpoint_id"],
                              properties=dict(checkpoint_id=dict(type="string", minLength=1))),
            run=self.read_adjudication_context)

    @staticmethod
    def _pair_key(checkpoint_id, comparator_checkpoint_id):
        return checkpoint_id, comparator_checkpoint_id

    def _interval_pair(self, checkpoint_id, comparator_checkpoint_id):
        checkpoint = self.checkpoints.require(checkpoint_id)
        comparator = self.checkpoints.require(comparator_checkpoint_id)
        if checkpoint.get("kind") != MANAGEMENT_KIND:
            raise ToolError("INTERVAL_REQUIRES_MANAGEMENT_CHECKPOINT")
        if comparator_checkpoint_id == checkpoint_id:
            raise ToolError("COMPARATOR_MUST_DIFFER_FROM_CHECKPOINT")
        if compare(comparator, checkpoint)["relation"] != "before":
            raise ToolError("INTERVAL_REQUIRES_STRICTLY_EARLIER_COMPARATOR")
        return checkpoint, comparator

    @staticmethod
    def _interval_original_read_required(source):
        title = source.get("title") or ""
        high_risk_title = any(term in title for term in (
            "EUS", "超声内镜", "腹腔镜", "穿刺", "活检"))
        return source.get("kind") in {"imaging", "pathology"} or high_risk_title

    def _interval_rows(self, checkpoint, comparator):
        current_source_ids = set(checkpoint.get("source_ids") or ())
        rows = []
        for source in self.source_index.sources:
            source_id = source["source_id"]
            if source_id in current_source_ids:
                continue
            clock = source.get("clock") or {}
            if not clock.get("value"):
                interval_relation = "UNRESOLVED_CLOCK"
            elif compare(comparator, source)["relation"] == "before" \
                    and compare(source, checkpoint)["relation"] != "after":
                interval_relation = "IN_INTERVAL"
            else:
                continue
            rows.append(dict(
                source_id=source_id, kind=source.get("kind"),
                title=source.get("title"), clock=deepcopy(clock),
                clock_basis=source.get("clock_basis"),
                characters=len(source.get("text") or ""),
                interval_relation=interval_relation,
                original_read_required=self._interval_original_read_required(source)))
        rows.sort(key=lambda row: (
            row["interval_relation"] == "UNRESOLVED_CLOCK",
            (row["clock"] or {}).get("value") or "9999", row["source_id"]))
        return rows

    def list_interval_sources(self, checkpoint_id, comparator_checkpoint_id,
                              offset=0, limit=200):
        checkpoint, comparator = self._interval_pair(
            checkpoint_id, comparator_checkpoint_id)
        rows = self._interval_rows(checkpoint, comparator)
        offset = max(0, int(offset))
        limit = max(1, min(200, int(limit)))
        page = rows[offset:offset + limit]
        key = self._pair_key(checkpoint_id, comparator_checkpoint_id)
        self.interval_listed.setdefault(key, set()).update(
            row["source_id"] for row in page)
        return dict(
            checkpoint_id=checkpoint_id,
            comparator_checkpoint_id=comparator_checkpoint_id,
            total=len(rows), offset=offset, returned=len(page),
            truncated=offset + len(page) < len(rows),
            in_interval_count=sum(
                row["interval_relation"] == "IN_INTERVAL" for row in rows),
            unresolved_clock_count=sum(
                row["interval_relation"] == "UNRESOLVED_CLOCK" for row in rows),
            original_read_required_count=sum(
                row["original_read_required"] for row in rows),
            sources=page,
            note=("可选诊断索引，不返回正文，不要求逐项处置；"
                  "当前CP自身来源不列为区间新增资料，时间未知资料单独列出。"))

    def review_interval_sources(self, checkpoint_id, comparator_checkpoint_id, reviews):
        checkpoint, comparator = self._interval_pair(
            checkpoint_id, comparator_checkpoint_id)
        if not isinstance(reviews, list) or not reviews:
            raise ToolError("INTERVAL_REVIEWS_REQUIRED")
        expected = {row["source_id"]: row
                    for row in self._interval_rows(checkpoint, comparator)}
        key = self._pair_key(checkpoint_id, comparator_checkpoint_id)
        listed = self.interval_listed.get(key, set())
        stored = self.interval_reviews.setdefault(key, {})
        expanded = []
        for review in reviews:
            if not isinstance(review, dict):
                raise ToolError("INTERVAL_REVIEW_NOT_OBJECT")
            source_id = review.get("source_id")
            source_ids = review.get("source_ids")
            if (source_id is None) == (source_ids is None):
                raise ToolError("INTERVAL_REVIEW_REQUIRES_SOURCE_ID_OR_SOURCE_IDS")
            if source_ids is not None:
                if not isinstance(source_ids, list) or not source_ids \
                        or not all(isinstance(item, str) and item for item in source_ids):
                    raise ToolError("INTERVAL_REVIEW_SOURCE_IDS_INVALID")
                if len(set(source_ids)) != len(source_ids):
                    raise ToolError("INTERVAL_REVIEW_SOURCE_IDS_DUPLICATE")
            else:
                source_ids = [source_id]
            for item in source_ids:
                expanded.append(dict(source_id=item, status=review.get("status"),
                                     reason=review.get("reason")))
        batch_source_ids = [review["source_id"] for review in expanded]
        if len(set(batch_source_ids)) != len(batch_source_ids):
            raise ToolError("INTERVAL_REVIEW_BATCH_SOURCE_DUPLICATE")
        for review in expanded:
            source_id = review["source_id"]
            status = review.get("status")
            reason = review.get("reason")
            if source_id not in expected:
                raise ToolError("INTERVAL_SOURCE_NOT_IN_PAIR:" + str(source_id))
            if source_id not in listed:
                raise ToolError("INTERVAL_SOURCE_NOT_LISTED:" + source_id)
            if status not in INTERVAL_STATUSES:
                raise ToolError("INTERVAL_REVIEW_STATUS_INVALID:" + str(status))
            if not isinstance(reason, str) or not reason.strip():
                raise ToolError("INTERVAL_REVIEW_REASON_REQUIRED:" + source_id)
            if status in INTERVAL_READ_STATUSES \
                    and source_id not in self.source_index.read_source_ids:
                raise ToolError("INTERVAL_SOURCE_NOT_READ:" + source_id)
            if status == "SKIPPED_FROM_INDEX" \
                    and expected[source_id]["original_read_required"]:
                raise ToolError("INTERVAL_HIGH_RISK_SOURCE_REQUIRES_READ:" + source_id)
            previous = stored.get(source_id)
            revision = (previous or {}).get("revision", 0) + 1
            if previous and revision > 1:
                self.interval_review_revisions += 1
            stored[source_id] = dict(
                source_id=source_id, status=status, reason=reason.strip(),
                interval_relation=expected[source_id]["interval_relation"],
                revision=revision)
        missing = sorted(set(expected) - set(stored))
        return dict(recorded=len(reviews), total=len(expected),
                    reviewed=len(stored), complete=not missing,
                    missing_source_count=len(missing),
                    missing_source_ids=missing[:20],
                    missing_source_ids_truncated=len(missing) > 20,
                    revision_count=self.interval_review_revisions)

    def interval_tools(self):
        review_schema = dict(
            type="object", additionalProperties=False,
            required=["status", "reason"],
            properties=dict(
                source_id=dict(type="string", minLength=1),
                source_ids=dict(type="array", minItems=1,
                                items=dict(type="string", minLength=1)),
                status=dict(type="string", enum=sorted(INTERVAL_STATUSES)),
                reason=dict(type="string", minLength=1)))
        pair_properties = dict(
            checkpoint_id=dict(type="string", minLength=1),
            comparator_checkpoint_id=dict(type="string", minLength=1))
        return [
            Tool(name="list_checkpoint_interval_sources",
                 description=("可选：列出当前CP与comparator之间及时间未知的资料索引；不返回正文。"
                              "仅证据关系不清时使用，按需read_source，不要求逐项处置。"),
                 input_schema=dict(
                     type="object", additionalProperties=False,
                     required=["checkpoint_id", "comparator_checkpoint_id"],
                     properties=dict(**pair_properties, offset=dict(type="integer", minimum=0),
                                     limit=dict(type="integer", minimum=1, maximum=200))),
                 run=self.list_interval_sources),
            Tool(name="review_checkpoint_interval_sources",
                 description=("批量记录或修订区间资料处置；同一来源重复提交会更新最终状态并记录revision。"
                              "同一状态和理由的多条资料用source_ids合组，不要重复理由。READ状态须先读原文；"
                              "高风险检查不得仅凭索引跳过；SUPPORT前不得保留USED或UNCERTAIN项。"),
                 input_schema=dict(
                     type="object", additionalProperties=False,
                     required=["checkpoint_id", "comparator_checkpoint_id", "reviews"],
                     properties=dict(**pair_properties, reviews=dict(
                         type="array", minItems=1, items=review_schema))),
                 run=self.review_interval_sources, writes=True),
        ]

    def _require_current_source_read(self, checkpoint):
        checkpoint_id = checkpoint["checkpoint_id"]
        if checkpoint_id not in self.checkpoints.read_checkpoint_ids:
            raise ToolError(
                "CURRENT_CHECKPOINT_NOT_READ: 提交前必须读取当前CP详情：" + checkpoint_id)
        source_ids = set(checkpoint.get("source_ids") or ())
        if not source_ids or not self.source_index.read_source_ids.intersection(source_ids):
            raise ToolError(
                "CURRENT_CHECKPOINT_SOURCE_NOT_READ: 提交前必须read_source读取当前CP的"
                "至少一份来源原文：" + ",".join(sorted(source_ids)))

    def _require_interval_review(self, checkpoint, comparator, disposition,
                                 evolution, evidence_supports):
        if disposition not in {RETAIN, SUPPORT}:
            return
        key = self._pair_key(checkpoint["checkpoint_id"],
                             comparator["checkpoint_id"])
        expected = {row["source_id"] for row in self._interval_rows(checkpoint, comparator)}
        listed = self.interval_listed.get(key, set())
        if not expected <= listed:
            raise ToolError(
                "INTERVAL_INDEX_NOT_LISTED: 必须逐页查看两个CP之间的全部资料索引，"
                "缺失%d项（最多显示20项）：" % len(expected - listed)
                + ",".join(sorted(expected - listed)[:20]))
        reviews = self.interval_reviews.get(key, {})
        missing = sorted(expected - set(reviews))
        if missing:
            raise ToolError(
                "INTERVAL_REVIEW_INCOMPLETE: 区间资料尚未逐项处置，缺失%d项"
                "（最多显示20项）：" % len(missing) + ",".join(missing[:20]))
        uncertain = sorted(source_id for source_id, review in reviews.items()
                           if review["status"] == "UNCERTAIN")
        if uncertain:
            raise ToolError(
                "INTERVAL_UNCERTAIN_REQUIRES_REVIEW: 区间资料关系不确定时不能提交"
                "RETAIN或SUPPORT：" + ",".join(uncertain))
        used = sorted(source_id for source_id, review in reviews.items()
                      if review["status"] == "READ_AND_USED")
        if disposition == SUPPORT and used:
            raise ToolError(
                "INTERVAL_NEW_EVIDENCE_REQUIRES_RETAIN_OR_REVIEW: 已标记新增证据，"
                "不能再声称无新增证据并SUPPORT_ONLY：" + ",".join(used))
        cited = {citation.get("source_id") for citation in evidence_supports or ()}
        if disposition == RETAIN and evolution == "NEW_EVIDENCE_ROUND":
            not_cited = sorted(set(used) - cited)
            if not_cited:
                raise ToolError(
                    "INTERVAL_USED_EVIDENCE_NOT_CITED: 标记为使用的证据必须进入"
                    "evidence_supports：" + ",".join(not_cited))
            not_used = sorted(source_id for source_id in cited
                              if source_id in expected and source_id not in used)
            if not_used:
                raise ToolError(
                    "INTERVAL_EVIDENCE_NOT_MARKED_USED: 作为新增证据引用的区间资料"
                    "必须标记READ_AND_USED：" + ",".join(not_used))

    def _require_comparator_read(self, comparator):
        checkpoint_id = comparator["checkpoint_id"]
        if checkpoint_id not in self.checkpoints.read_checkpoint_ids:
            raise ToolError(
                "COMPARATOR_NOT_READ: 提交前必须read_candidate_checkpoint读取比较对象：" + checkpoint_id)
        if comparator.get("kind") == MANAGEMENT_KIND:
            return
        target_ids = self.checkpoints.diagnostic_target_source_ids(comparator)
        if not target_ids:
            raise ToolError(
                "DIAGNOSTIC_COMPARATOR_TARGET_UNAVAILABLE: 诊断比较对象没有可核验的目标报告来源。")
        if not self.source_index.read_source_ids.intersection(target_ids):
            raise ToolError(
                "COMPARATOR_DIAGNOSTIC_SOURCE_NOT_READ: 必须read_source读取比较诊断CP的"
                "目标报告，不能用当前管理文书代替：" + ",".join(target_ids))

    def adjudicate(self, checkpoint_id, disposition, evolution,
                   comparator_checkpoint_id=None, evidence_supports=None,
                   rationale=""):
        checkpoint = self.checkpoints.by_id.get(checkpoint_id)
        if checkpoint is None:
            self._remember_problem("UNKNOWN_CHECKPOINT_ID", checkpoint_id)
            raise ToolError("UNKNOWN_CHECKPOINT_ID:" + str(checkpoint_id))
        if checkpoint.get("kind") != MANAGEMENT_KIND:
            self._remember_problem("DIAGNOSTIC_CHECKPOINT_NOT_ADJUDICATED", checkpoint_id)
            raise ToolError("DIAGNOSTIC_CHECKPOINT_NOT_ADJUDICATED:" + checkpoint_id)
        if checkpoint_id in self.items:
            self._remember_problem("DUPLICATE_CHECKPOINT_ADJUDICATION", checkpoint_id)
            raise ToolError("DUPLICATE_CHECKPOINT_ADJUDICATION:" + checkpoint_id)
        if disposition not in DISPOSITIONS:
            raise ToolError("ADJUDICATION_DISPOSITION_INVALID:" + str(disposition))
        if evolution not in EVOLUTIONS:
            raise ToolError("ADJUDICATION_EVOLUTION_INVALID:" + str(evolution))
        if not isinstance(rationale, str) or not rationale.strip():
            raise ToolError("ADJUDICATION_RATIONALE_REQUIRED")
        if evolution == "UNCERTAIN" and disposition != REVIEW:
            raise ToolError("UNCERTAIN_EVOLUTION_REQUIRES_REVIEW_REQUIRED")
        if not _clock_value(checkpoint) and not (
                disposition == REVIEW and evolution == "UNCERTAIN"):
            raise ToolError("CHECKPOINT_CLOCK_UNKNOWN_REQUIRES_REVIEW")
        unordered_states = self._unordered_states(checkpoint)
        if unordered_states and not (disposition == REVIEW and evolution == "UNCERTAIN"):
            raise ToolError(
                "UNORDERED_CANDIDATE_STATE_REQUIRES_REVIEW: 同刻或不可定序候选状态不能忽略；"
                "请用REVIEW_REQUIRED+UNCERTAIN。")
        if disposition != REVIEW:
            self._require_prior_adjudications(checkpoint)
        latest_retained = self._latest_retained(checkpoint)
        if len(latest_retained) > 1 and not (disposition == REVIEW and evolution == "UNCERTAIN"):
            raise ToolError(
                "PARALLEL_LATEST_STATES_REQUIRE_REVIEW: 并列最近保留状态不可任选一个比较；"
                "请用REVIEW_REQUIRED+UNCERTAIN。")

        comparator = None
        if disposition == RETAIN and evolution == "FIRST_VISIBLE":
            comparator_checkpoint_id = None
        else:
            comparator_checkpoint_id = (next(iter(latest_retained))
                                        if len(latest_retained) == 1 else None)
            if comparator_checkpoint_id is None and disposition == REVIEW \
                    and evolution == "UNCERTAIN":
                comparator_checkpoint_id = self.read_adjudication_context(
                    checkpoint_id)["review_comparator_checkpoint_id"]
        if comparator_checkpoint_id is not None:
            comparator = self.checkpoints.require(comparator_checkpoint_id)
        if disposition == SUPPORT:
            if comparator is None:
                raise ToolError("SUPPORT_ONLY_REQUIRES_COMPARATOR")
            if evolution not in SUPPORT_EVOLUTIONS:
                raise ToolError("SUPPORT_ONLY_EVOLUTION_INVALID:" + evolution)
            if comparator_checkpoint_id not in latest_retained:
                raise ToolError(
                    "COMPARATOR_NOT_LATEST_RETAINED_STATE: SUPPORT_ONLY必须挂到最近保留状态，"
                    "不能跳过中间CP或挂到未保留CP。当前唯一可选comparator："
                    + ",".join(sorted(latest_retained)))
        elif disposition == RETAIN:
            if comparator is not None:
                if evolution != "NEW_EVIDENCE_ROUND":
                    raise ToolError(
                        "RETAIN_WITH_COMPARATOR_REQUIRES_NEW_EVIDENCE_ROUND")
                if comparator_checkpoint_id not in latest_retained:
                    raise ToolError(
                        "COMPARATOR_NOT_LATEST_RETAINED_STATE: 只能相对最近保留状态判定新增证据，"
                        "不能跳过中间CP、选择未保留CP或无视已有诊断CP。当前唯一可选comparator："
                        + ",".join(sorted(latest_retained)))
                if not evidence_supports:
                    raise ToolError(
                        "NEW_EVIDENCE_ROUND_REQUIRES_EVIDENCE_SUPPORT: "
                        "不同动作、复述或更具体计划本身不能替代新增患者证据。")
            elif evolution != "FIRST_VISIBLE":
                raise ToolError("RETAIN_WITHOUT_COMPARATOR_REQUIRES_FIRST_VISIBLE")
            elif any(compare(candidate, checkpoint)["relation"] == "before"
                     for candidate in self.checkpoints.checkpoints
                     if candidate["checkpoint_id"] != checkpoint_id):
                raise ToolError(
                    "FIRST_VISIBLE_REQUIRES_NO_PRIOR_RETAINED_STATE: 已有更早可见候选状态时，"
                    "无论其保留或待核，都不能重新声明首见锚点。")

        self._require_current_source_read(checkpoint)
        if comparator is not None:
            self._require_comparator_read(comparator)
        evidence = self._evidence(
            checkpoint, evidence_supports, disposition=disposition, evolution=evolution,
            comparator=comparator)
        item = dict(
            checkpoint_id=checkpoint_id, disposition=disposition, evolution=evolution,
            comparator_checkpoint_id=comparator_checkpoint_id,
            evidence_supports=evidence,
            rationale=rationale.strip(), actor=ACTOR,
            review_status=("REVIEW_REQUIRED" if disposition == REVIEW
                           else "CANDIDATE_ADJUDICATED"))
        self.items[checkpoint_id] = item
        if disposition == RETAIN:
            self.retained_checkpoint_ids.add(checkpoint_id)
        return dict(recorded=True, item=deepcopy(item),
                    note=("程序仅核验结构、原文定位、span不重叠、最近保留状态和检索时钟；"
                          "这不构成临床关系或独立CP的专家确认。"))

    def adjudication_tool(self):
        citation_schema = dict(
            type="object", additionalProperties=False,
            required=["source_id", "quote"],
            properties=dict(source_id=dict(type="string", minLength=1),
                            quote=dict(type="string", minLength=1,
                                       description="逐字且在同一来源中唯一；重复时扩大引用上下文。")))
        return Tool(
            name="adjudicate_checkpoint",
            description=("裁决一个MANAGEMENT_REASSESSMENT候选CP。RETAIN比较时必须给新增患者证据；"
                         "SUPPORT归并到严格更早CP；时间不确定用REVIEW_REQUIRED+UNCERTAIN。"),
            input_schema=dict(
                type="object", additionalProperties=False,
                required=["checkpoint_id", "disposition", "evolution", "rationale"],
                properties=dict(
                    checkpoint_id=dict(type="string", minLength=1),
                    disposition=dict(type="string", enum=sorted(DISPOSITIONS)),
                    evolution=dict(type="string", enum=sorted(EVOLUTIONS)),
                    evidence_supports=dict(type="array", items=citation_schema),
                    rationale=dict(type="string", minLength=1))),
            run=self.adjudicate, writes=True)

    def finish_tool(self):
        def run(note=""):
            missing = [item for item in self.checkpoints.management_ids
                       if item not in self.items]
            if missing or self.attempt_problems:
                problem = dict(
                    missing_checkpoint_ids=missing,
                    attempt_problems=deepcopy(self.attempt_problems),
                    guidance=("每个管理CP必须恰好裁决一次；诊断CP不裁决。"
                              "修正后完成全部必需裁决，再调用finish。"))
                raise ToolError("CHECKPOINT_ADJUDICATION_INCOMPLETE:"
                                + json.dumps(problem, ensure_ascii=False))
            self.finished = True
            return dict(finished=True, note=note,
                        adjudicated_count=len(self.items), missing_checkpoint_ids=[])
        return Tool(
            name="finish",
            description="完成全病例CP裁决；程序在此检查未知、重复和漏裁决。",
            input_schema=dict(type="object", additionalProperties=False,
                              properties=dict(note=dict(type="string"))),
            run=run, terminal=True)

    def result(self, *, case_id, stop, final=None):
        items = [deepcopy(self.items[checkpoint_id])
                 for checkpoint_id in self.checkpoints.management_ids
                 if checkpoint_id in self.items]
        missing = [item for item in self.checkpoints.management_ids
                   if item not in self.items]
        listed = set(self.checkpoints.returned_checkpoint_ids)
        expected = set(self.checkpoints.by_id)
        complete = (stop == "answered" and self.finished and not missing
                    and not self.attempt_problems
                    and (not self.checkpoints.management_ids or expected <= listed))
        summary = dict(
            stop=stop, final=deepcopy(final or {}), complete=complete,
            management_checkpoint_count=len(self.checkpoints.management_ids),
            diagnostic_checkpoint_count=len(expected) - len(self.checkpoints.management_ids),
            adjudicated_count=len(items), missing_checkpoint_ids=missing,
            disposition_counts=dict(sorted(Counter(
                item["disposition"] for item in items).items())),
            evolution_counts=dict(sorted(Counter(
                item["evolution"] for item in items).items())),
            interval_evidence_review=dict(required=False, optional_index=True),
            attempt_problems=deepcopy(self.attempt_problems),
            candidate_checkpoint_list_coverage=dict(
                required_checkpoint_count=len(expected),
                listed_checkpoint_count=len(expected & listed),
                unlisted_checkpoint_ids=sorted(expected - listed),
                complete=expected <= listed),
            limitations=deepcopy(self.checkpoints.limitations),
            clinical_reference_created=False,
            candidate_review_only=True,
            note=("裁决结果仍供候选审核；引文和时钟核验不等于临床确认，也不证明引文"
                  "在语义上必然是患者证据。成员候选缺失或span无效会列入limitations，"
                  "且不能据此接受证据引文。"))
        return dict(format=ADJUDICATION_FORMAT, case_id=case_id,
                    items=items, summary=summary)


def checkpoint_adjudication_tools(registry, *, checkpoint_index, source_index):
    store = CheckpointAdjudicationStore(
        checkpoint_index=checkpoint_index, source_index=source_index)
    registry.register(store.interval_tools()[0])
    registry.register(store.adjudication_context_tool())
    registry.register(store.adjudication_tool())
    registry.register(store.finish_tool())
    return store


def build_prompt(checkpoints, *, case_id):
    management_count = sum(item.get("kind") == MANAGEMENT_KIND for item in checkpoints)
    return """## 病例 %s 的全病例候选CP证据演化裁决

候选CP共 %d 个，其中MANAGEMENT_REASSESSMENT共 %d 个。先逐页list_candidate_checkpoints查看全部CP；诊断CP不裁决，每个管理CP恰好adjudicate_checkpoint一次。
按clock从早到晚，每个管理CP先read_checkpoint_adjudication_context，由程序返回当前CP及唯一最近保留comparator，不得自行选择比较对象。诊断CP也属于已保留状态。动作更具体、新计划、转科、复述或执行不等于证据演化；CT后既有EUS+FNA动作时，后续转入记录中的EUS+FNA/择期手术应归并为SUPPORT_ONLY。
每次裁决前读取当前来源原文；诊断comparator还须读取目标报告原文。区间索引仅在证据关系不清时可选查看，不要求逐项处置全部区间资料。
RETAIN_CP+NEW_EVIDENCE_ROUND必须附不重叠于当前member candidate span的逐字患者证据，且原始证据严格晚于comparator、不晚于当前CP。comparator目标报告及其在后续文书中的旧复述均不可用；BEFORE_ACQUISITION comparator的未来目标报告只用于识别比较对象。时间不确定用REVIEW_REQUIRED+UNCERTAIN。
逐项提交后立即继续，全部管理CP完成后finish。
""" % (case_id, len(checkpoints), management_count)


def run_checkpoint_adjudication(harness, *, case_id, sources, candidates, projection):
    """Run one case-level adjudication turn through the existing harness."""
    checkpoints = list(projection.get("candidate_checkpoints") or ())
    source_index = SourceIndex(sources)
    checkpoint_index = AdjudicationCheckpointIndex(checkpoints, candidates)
    if not checkpoint_index.management_ids:
        store = CheckpointAdjudicationStore(
            checkpoint_index=checkpoint_index, source_index=source_index)
        store.finished = True
        return store.result(case_id=case_id, stop="answered",
                            final=dict(note="无管理CP需要本阶段裁决。"))

    session = harness.session_for("checkpoint_adjudication")
    registry = harness.new_registry(session=session)
    source_index.register(registry)
    for tool in checkpoint_index.tools():
        registry.register(tool)
    store = checkpoint_adjudication_tools(
        registry, checkpoint_index=checkpoint_index, source_index=source_index)
    agent = harness.agent(registry=registry, session=session,
                          system_prompt=ADJUDICATION_SYSTEM_PROMPT)
    outcome = agent.act(build_prompt(checkpoints, case_id=case_id),
                        final_tool=registry.get("finish"))
    stop = outcome.stop
    if stop == "answered" and not store.finished:
        stop = "not_finished"
    return store.result(case_id=case_id, stop=stop,
                        final=getattr(outcome, "final", None) or {})


__all__ = [
    "ADJUDICATION_FORMAT", "ADJUDICATION_SYSTEM_PROMPT",
    "AdjudicationCheckpointIndex", "CheckpointAdjudicationStore",
    "build_prompt", "checkpoint_adjudication_tools", "run_checkpoint_adjudication",
]
