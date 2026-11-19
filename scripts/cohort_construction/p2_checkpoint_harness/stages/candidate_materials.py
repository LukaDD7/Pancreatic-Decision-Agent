"""Private timed materials for reprojected candidate Checkpoints."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .materials import digest
from .privacy import deidentify_value


FORMAT = "p2-candidate-checkpoint-material-0.6.1"
BEFORE_KINDS = {"pre_exam_evaluation", "BEFORE_ACQUISITION"}
ARRIVAL_KINDS = {"evidence_arrival_reassessment", "AFTER_EVIDENCE_ARRIVAL"}
MANAGEMENT_KINDS = {"management_decision", "MANAGEMENT_REASSESSMENT"}
DIAGNOSTIC_KINDS = BEFORE_KINDS | ARRIVAL_KINDS


def _clock_value(clock):
    return (clock or {}).get("value") if isinstance(clock, dict) else clock


def _created_after_cutoff(created_clock, cutoff_clock):
    created_value = _clock_value(created_clock)
    cutoff_value = _clock_value(cutoff_clock)
    if not created_value or not cutoff_value:
        return False
    created_value = created_value.replace("T", " ")
    cutoff_value = cutoff_value.replace("T", " ")
    created_date, created_separator, created_time = created_value.partition(" ")
    cutoff_date, cutoff_separator, cutoff_time = cutoff_value.partition(" ")
    if len(created_date) != 10 or len(cutoff_date) != 10:
        return False
    if created_date != cutoff_date:
        return created_date > cutoff_date
    coarse_precisions = {"year", "month", "day", "date", "unknown"}
    created_precision = (created_clock or {}).get("precision") \
        if isinstance(created_clock, dict) else None
    cutoff_precision = (cutoff_clock or {}).get("precision") \
        if isinstance(cutoff_clock, dict) else None
    if not created_separator or not cutoff_separator \
            or created_precision in coarse_precisions or cutoff_precision in coarse_precisions:
        return False
    precision_lengths = {"hour": 2, "minute": 5, "second": 8}
    known_precisions = [precision_lengths[value] for value in
                        (created_precision, cutoff_precision) if value in precision_lengths]
    if known_precisions:
        length = min(known_precisions)
        created_time, cutoff_time = created_time[:length], cutoff_time[:length]
    return bool(created_time and cutoff_time and created_time > cutoff_time)


def _source_card(source, visibility_clock):
    return dict(source_id=source["source_id"], title=source.get("title"),
                kind=source.get("kind"), clock=deepcopy(visibility_clock),
                original_source_clock=deepcopy(source.get("clock") or {}),
                clock_basis=source.get("clock_basis"),
                clocks=deepcopy(source.get("clocks") or {}),
                source_sha256=source.get("source_sha256"),
                text=source.get("text") or "")


def _apply_spans(text, spans):
    pieces, cursor = [], 0
    for span in sorted(spans, key=lambda item: (item["start"], item["end"])):
        if span["start"] < cursor:
            raise ValueError("OVERLAPPING_MASK_SPANS_REQUIRE_MERGE")
        replacement = ("[晚于评价时点的时间已遮蔽]"
                       if span.get("mask_reason") == "FUTURE_EXPLICIT_DATETIME_REDACTION"
                       else "[目标管理判断已遮蔽]")
        pieces.extend((text[cursor:span["start"]], replacement))
        cursor = span["end"]
    pieces.append(text[cursor:])
    return "".join(pieces)


def _merge_spans(spans):
    merged = []
    for span in sorted(spans, key=lambda item: (item["start"], item["end"])):
        if merged and span["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], span["end"])
            merged[-1]["member_event_ids"] = sorted(set(
                merged[-1].get("member_event_ids", [])
                + span.get("member_event_ids", [])))
        else:
            merged.append(deepcopy(span))
    return merged


def _diagnostic_aliases(event, source_by_id):
    titles = " ".join((source_by_id[source_id].get("title") or "")
                      for source_id in event.get("source_ids", [])
                      if source_id in source_by_id)
    aliases = set()
    if any(term in titles.upper() for term in ("MR", "MRI")) or "磁共振" in titles:
        aliases.update(("MR", "MRI", "磁共振"))
    if "CT" in titles.upper():
        aliases.update(("CT", "增强CT", "CT增强", "CT增强造影"))
    for source_id in event.get("source_ids", []):
        title = (source_by_id.get(source_id) or {}).get("title")
        if title:
            aliases.add(title)
    return sorted(aliases, key=len, reverse=True)


def _mask_management_action_lines(text):
    import re
    terms = ("拟施", "拟行", "已申请", "已签署", "诊疗计划", "治疗计划", "处置计划",
             "检查安排", "可考虑予", "可考虑行", "抑酸", "抑酶", "术前准备",
             "取消手术", "穿刺活检", "下一步治疗", "转入", "择期手术", "完善检查",
             "出院", "随诊", "输血", "冰冻病理", "排除手术禁忌", "限期行手术", "定于",
             "手术指征", "手术切除", "手术方式", "手术时间", "根治性胰", "Whipple",
             "限期行", "择期行", "拟实施", "故拟", "需手术", "签署知情同意", "积极术前准备",
             "建议", "转肿瘤内科", "系统化疗", "化疗", "放疗", "免疫治疗", "靶向治疗",
             "保守治疗", "转诊", "随访", "复查")
    spans = []
    for match in re.finditer(r"[^。；，,\n]+[。；，,\n]?", text):
        clause = match.group(0)
        positions = [clause.find(term) for term in terms if term in clause]
        if positions:
            spans.append(dict(start=match.start() + min(positions), end=match.end(),
                              mask_reason="ADJACENT_MANAGEMENT_ACTION_HEURISTIC"))
    return text, spans


def _future_datetime_spans(text, cutoff):
    import re
    if not cutoff:
        return []
    spans = []
    pattern = r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?"
    for match in re.finditer(pattern, text):
        normalized = match.group(0).replace("年", "-").replace("月", "-")
        normalized = normalized.replace("日", "").replace("/", "-").replace(".", "-")
        normalized = normalized.replace("T", " ")
        parts = normalized.split(" ", 1)
        date = "-".join(part.zfill(2) for part in parts[0].split("-"))
        value = date + ((" " + parts[1]) if len(parts) == 2 else "")
        if value > cutoff:
            spans.append(dict(start=match.start(), end=match.end(),
                              mask_reason="FUTURE_EXPLICIT_DATETIME_REDACTION"))
    return spans


def _redact_future_clocks(card, cutoff):
    if not cutoff:
        return
    def projected(clock):
        if not isinstance(clock, dict) or not _clock_value(clock):
            return deepcopy(clock)
        if _clock_value(clock) > cutoff:
            return dict(value=None, precision="unknown", redacted=True,
                        reason="AFTER_CANDIDATE_EVALUATION_CUTOFF")
        return deepcopy(clock)
    card["clock"] = projected(card.get("clock") or {})
    card["original_source_clock"] = projected(card.get("original_source_clock") or {})
    card["clocks"] = {key: projected(value) for key, value in (card.get("clocks") or {}).items()}


def _mask_diagnostic_restatements(text, aliases):
    spans, cursor = [], 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if content and any(alias.upper() in content.upper() for alias in aliases):
            spans.append(dict(start=cursor, end=cursor + len(content),
                              mask_reason="TARGET_DIAGNOSTIC_RESTATEMENT_HEURISTIC"))
        cursor += len(line)
    return text, spans


def _same_event_spans(checkpoint, candidates, relations):
    members = set(checkpoint.get("member_event_ids") or [])
    if not members:
        return []
    related_ids = set()
    for relation in relations:
        if relation.get("relation") != "same_event":
            continue
        endpoints = {relation.get("source_event_id"), relation.get("target_event_id")}
        if endpoints & members:
            related_ids.update(endpoints)
    return [deepcopy(candidate["span"]) for candidate in candidates
            if candidate.get("event_id") in related_ids - members
            and candidate.get("status") == "ACCEPTED" and candidate.get("span")]


def _candidate_relation_summary(relation):
    kind = relation.get("relation")
    if kind == "same_management_process":
        return "Agent 记录为同一管理过程；共同目标、具体理由和原文依据在独立关系文件核对。"
    if kind == "evidence_used":
        return "Agent 记录后续管理使用该证据；具体理由和原文依据在独立关系文件核对。"
    return "Agent 记录的候选 CP 关系；具体理由和原文依据在独立关系文件核对。"


def _same_checkpoint_spans(checkpoint, projection, relations):
    related_ids = set()
    for relation in relations:
        if relation.get("relation") != "same_event" \
                or relation.get("confidence") != "supported":
            continue
        endpoints = {relation.get("source_checkpoint_id"),
                     relation.get("target_checkpoint_id")}
        if checkpoint["checkpoint_id"] in endpoints:
            related_ids.update(endpoints)
    related_ids.discard(checkpoint["checkpoint_id"])
    return [deepcopy(span) for candidate in projection["candidate_checkpoints"]
            if candidate["checkpoint_id"] in related_ids
            for span in candidate.get("masked_spans") or []]


def _navigation(packets):
    kind_order = {"BEFORE_ACQUISITION": 0, "pre_exam_evaluation": 0,
                  "AFTER_EVIDENCE_ARRIVAL": 2, "evidence_arrival_reassessment": 2}
    groups = {}
    for packet in sorted(packets, key=lambda item: (
            item["episode_id"] or "EP-UNASSIGNED",
            _clock_value(item["evaluation"]["clock"]) or "9999",
            kind_order.get(item.get("kind"), 1),
            item["checkpoint_id"])):
        groups.setdefault(packet["episode_id"], []).append(packet)
    for siblings in groups.values():
        for index, packet in enumerate(siblings):
            packet["episode_navigation"] = dict(
                index=index + 1, total=len(siblings),
                previous_checkpoint_id=(siblings[index - 1]["checkpoint_id"]
                                        if index else None),
                next_checkpoint_id=(siblings[index + 1]["checkpoint_id"]
                                      if index + 1 < len(siblings) else None),
                order_role="candidate_display_order_not_confirmed_clinical_sequence")
    ordered = sorted(packets, key=lambda item: (
        _clock_value(item["evaluation"]["clock"]) or "9999",
        kind_order.get(item.get("kind"), 1), item["checkpoint_id"]))
    for index, packet in enumerate(ordered):
        packet["display_order"] = dict(index=index + 1, total=len(packets))
        packet["sequence_navigation"] = dict(
            index=index + 1, total=len(ordered),
            previous_checkpoint_id=(ordered[index - 1]["checkpoint_id"] if index else None),
            next_checkpoint_id=(ordered[index + 1]["checkpoint_id"]
                                if index + 1 < len(ordered) else None),
            order_role="whole_case_display_order_not_confirmed_clinical_sequence")


def _task_header(checkpoint, cycle, included_sources):
    upstream = {}
    for container in (cycle, checkpoint):
        upstream.update(container.get("task_header") or {})
        for key in ("next_key_surgical_step", "allowed_time_precision",
                    "allowed_time_precisions", "knowledge_version"):
            if container.get(key) is not None:
                upstream[key] = deepcopy(container[key])
    return dict(
        next_key_surgical_step=upstream.get("next_key_surgical_step"),
        allowed_time_precision=upstream.get(
            "allowed_time_precision", upstream.get("allowed_time_precisions")),
        knowledge_version=upstream.get("knowledge_version"),
        included_source_ids=[source["source_id"] for source in included_sources],
    )


def project_candidate_materials(*, case_id, sources, candidates, projection,
                                relations=None, episodes=None,
                                candidate_relations=None, candidate_episodes=None):
    """Project candidate CPs from sources de-identified before reconstruction."""
    for source in sources:
        if not source.get("deidentified_text_sha256"):
            raise ValueError("DIRECT_IDENTIFIER_INGRESS_RECEIPT_MISSING:" + source["source_id"])
        actual_hash = hashlib.sha256((source.get("text") or "").encode()).hexdigest()
        if source["deidentified_text_sha256"] != actual_hash \
                or source.get("source_sha256") != actual_hash:
            raise ValueError("DEIDENTIFIED_SOURCE_HASH_MISMATCH:" + source["source_id"])
    relations = relations or []
    episodes = episodes or []
    candidate_relations = candidate_relations or []
    organized = candidate_episodes is not None
    candidate_episodes = candidate_episodes or []
    checkpoint_ids = {checkpoint["checkpoint_id"]
                      for checkpoint in projection["candidate_checkpoints"]}
    candidate_membership = {}
    for episode in candidate_episodes:
        for checkpoint_id in episode.get("checkpoint_ids") or []:
            if checkpoint_id not in checkpoint_ids:
                raise ValueError("UNKNOWN_CANDIDATE_EPISODE_MEMBER:" + checkpoint_id)
            if checkpoint_id in candidate_membership:
                raise ValueError("DUPLICATE_CANDIDATE_EPISODE_MEMBERSHIP:" + checkpoint_id)
            candidate_membership[checkpoint_id] = episode["episode_id"]
    for relation in candidate_relations:
        for field in ("source_checkpoint_id", "target_checkpoint_id"):
            if relation.get(field) not in checkpoint_ids:
                raise ValueError("UNKNOWN_CANDIDATE_RELATION_ENDPOINT:" + str(relation.get(field)))
    if organized and set(candidate_membership) != checkpoint_ids:
        missing = sorted(checkpoint_ids - set(candidate_membership))
        raise ValueError("INCOMPLETE_CANDIDATE_EPISODE_COVERAGE:" + ",".join(missing))
    source_by_id = {source["source_id"]: source for source in sources}
    diagnostic_by_id = {event["event_id"]: event
                        for event in projection["diagnostic_events"]}
    diagnostic_source_map = {source_id: event for event in projection["diagnostic_events"]
                             for source_id in event["source_ids"]}
    cycle_by_id = {item.get("action_cycle_id"): item
                   for item in projection.get("action_cycles") or []}
    state_by_id = {item.get("evidence_state_id"): item
                   for item in projection.get("evidence_states") or []}
    effective_clocks = {}
    for source in sources:
        event = diagnostic_source_map.get(source["source_id"])
        effective_clocks[source["source_id"]] = deepcopy(
            event["visibility_clock"] if event else source.get("clock") or {})
    membership = {event_id: episode["episode_id"] for episode in episodes
                  for event_id in episode["event_ids"]}
    migration_by_event = {item["event_id"]: item for item in projection["migrations"]
                          if item["migration_type"] == "candidate_span"}
    checkpoint_by_event = {event_id: item["to_checkpoint_id"]
                           for event_id, item in migration_by_event.items()
                           if item.get("to_checkpoint_id")}
    diagnostic_cp_pairs = {}
    for event in projection["diagnostic_events"]:
        ids = event["candidate_checkpoint_ids"]
        before_id = ids.get("BEFORE_ACQUISITION") or ids.get("pre_exam_evaluation")
        arrival_id = ids.get("AFTER_EVIDENCE_ARRIVAL") or ids.get("evidence_arrival_reassessment")
        if before_id and arrival_id:
            diagnostic_cp_pairs[before_id] = (arrival_id, "evidence_arrival")
            diagnostic_cp_pairs[arrival_id] = (before_id, "BEFORE_ACQUISITION")

    material_migrations = deepcopy(projection["migrations"])
    packets = []
    for checkpoint in projection["candidate_checkpoints"]:
        cutoff = _clock_value(checkpoint["clock"])
        target_ids = set(checkpoint["source_ids"])
        diagnostic = diagnostic_by_id.get(checkpoint.get("diagnostic_event_id"))
        diagnostic_targets = [diagnostic_by_id[event_id]
                              for event_id in checkpoint.get("diagnostic_event_ids")
                              or ([checkpoint.get("diagnostic_event_id")]
                                  if checkpoint.get("diagnostic_event_id") else [])
                              if event_id in diagnostic_by_id]
        pre_exam = checkpoint["kind"] in BEFORE_KINDS
        if checkpoint["kind"] in ARRIVAL_KINDS and diagnostic_targets:
            target_ids = {source_id for event in diagnostic_targets
                          for source_id in event.get("source_ids") or []}
        target_spans_by_source = {}
        for span in checkpoint.get("masked_spans") or []:
            target_spans_by_source.setdefault(span["source_id"], []).append(span)
        for span in (_same_event_spans(checkpoint, candidates, relations)
                     + _same_checkpoint_spans(checkpoint, projection, candidate_relations)):
            target_spans_by_source.setdefault(span["source_id"], []).append(span)
        if pre_exam or checkpoint["kind"] in ARRIVAL_KINDS:
            for candidate in candidates:
                if candidate.get("status") != "ACCEPTED" or not candidate.get("span"):
                    continue
                span = candidate["span"]
                if pre_exam or span.get("source_id") not in target_ids:
                    target_spans_by_source.setdefault(span["source_id"], []).append(span)
        aliases = sorted({alias for event in diagnostic_targets
                          for alias in _diagnostic_aliases(event, source_by_id)},
                         key=len, reverse=True) if pre_exam else []
        included, excluded, mask_metadata = [], [], []
        whole_masked = []
        excluded_support_masked = []
        body_creation_time_conflicts = []

        def retain_excluded_support_masks(source_id):
            if source_id in target_ids or source_id not in checkpoint.get("support_source_ids", []):
                return
            spans = target_spans_by_source.get(source_id) or []
            if not spans:
                return
            excluded_support_masked.append(source_id)
            mask_metadata.extend(dict(
                source_id=source_id, start=span["start"], end=span["end"],
                mask_reason="SUPPORT_TARGET_EXCLUDED_FROM_VISIBLE_CONTEXT",
                member_event_ids=span.get("member_event_ids")) for span in spans)

        for source in sources:
            source_id = source["source_id"]
            visible_clock = effective_clocks[source_id]
            visible_value = _clock_value(visible_clock)
            is_masked_pre_exam_target = pre_exam and source_id in target_ids
            creation_clocks = source.get("clocks") or {}
            created_after_cutoff = source.get("kind") == "document" and any(
                _created_after_cutoff(creation_clocks.get(key), checkpoint["clock"])
                for key in ("source_create_time", "create_time", "record_create_time",
                            "created_time", "creation_time", "document_create_time"))
            retain_body_time_target = (
                created_after_cutoff and checkpoint["kind"] in MANAGEMENT_KINDS
                and source_id in target_ids and checkpoint.get("time_basis") == "body_record_time"
                and visible_value == cutoff)
            if retain_body_time_target:
                body_creation_time_conflicts.append(dict(
                    source_id=source_id, body_record_time=cutoff,
                    source_create_time=next(
                        _clock_value(creation_clocks.get(key))
                        for key in ("source_create_time", "create_time", "record_create_time",
                                    "created_time", "creation_time", "document_create_time")
                        if _created_after_cutoff(creation_clocks.get(key), checkpoint["clock"])),
                    disposition="RETAIN_MASKED_TARGET_AT_BODY_RECORD_TIME"))
            elif created_after_cutoff:
                excluded.append(dict(
                    source_id=source_id, disposition="EXCLUDED_BY_CANDIDATE_TIMELINE",
                    reason="SOURCE_CREATED_AFTER_CANDIDATE_CUTOFF"))
                retain_excluded_support_masks(source_id)
                continue
            if not is_masked_pre_exam_target and (
                    not cutoff or not visible_value or visible_value > cutoff):
                excluded.append(dict(source_id=source_id,
                                     disposition="EXCLUDED_BY_CANDIDATE_TIMELINE"))
                retain_excluded_support_masks(source_id)
                continue
            card = _source_card(source, visible_clock)
            _redact_future_clocks(card, cutoff)
            card["presentation_scope"] = "within_candidate_information_cutoff"
            if pre_exam and source_id in target_ids:
                card.update(title="[目标检查已遮蔽]", text="[目标检查组全部内容已遮蔽]",
                            presentation_scope="target_diagnostic_event_fully_masked")
                whole_masked.append(source_id)
            else:
                spans = list(target_spans_by_source.get(source_id) or [])
                if checkpoint["kind"] in MANAGEMENT_KINDS or (
                        checkpoint["kind"] in ARRIVAL_KINDS and source_id not in target_ids):
                    card["text"], action_spans = _mask_management_action_lines(card["text"])
                    for span in action_spans:
                        span.update(source_id=source_id)
                    spans.extend(action_spans)
                if pre_exam and aliases:
                    card["text"], heuristic_spans = _mask_diagnostic_restatements(
                        card["text"], aliases)
                    for span in heuristic_spans:
                        span.update(source_id=source_id)
                    spans.extend(heuristic_spans)
                spans.extend(_future_datetime_spans(card["text"], cutoff))
                if spans:
                    merged = _merge_spans(spans)
                    card["text"] = _apply_spans(card["text"], merged)
                    card["presentation_scope"] = "jointly_masked_candidate_projection"
                    mask_metadata.extend(dict(source_id=source_id, start=span["start"],
                                              end=span["end"],
                                              mask_reason=span.get("mask_reason"),
                                              member_event_ids=span.get("member_event_ids"))
                                         for span in merged)
            if not source.get("deidentified_text_sha256"):
                raise ValueError("DIRECT_IDENTIFIER_INGRESS_RECEIPT_MISSING:" + source_id)
            included.append(card)
        originals = []
        for source_id in checkpoint["source_ids"]:
            source = source_by_id.get(source_id)
            if source is not None:
                original = _source_card(source, source.get("clock") or {})
                original["presentation_scope"] = "researcher_original_not_doctor_projection"
                if original.get("text") != source.get("text"):
                    raise ValueError("RESEARCH_ORIGINAL_DIFFERS_FROM_INGRESS_SOURCE:" + source_id)
                originals.append(original)
        if checkpoint["kind"] in MANAGEMENT_KINDS:
            origin_episode_ids = sorted({membership[event_id] for event_id in
                                         checkpoint["member_event_ids"]
                                         if event_id in membership})
            episode_id = candidate_membership.get(checkpoint["checkpoint_id"])
            episode_basis = "candidate_cp_supported_relation_program_assembly"
            if episode_id is None:
                episode_id = "EP-CP-" + checkpoint["checkpoint_id"].removeprefix("CP-M-")
                episode_basis = "one_merged_candidate_checkpoint_pending_new_episode_organization"
            target_title = next((source_by_id[source_id].get("title")
                                 for source_id in checkpoint["source_ids"]
                                 if source_id in source_by_id), "未标来源")
            mask_reason = ("同一来源全部组成动作及same_event跨文书回述联合遮蔽；"
                           "其他邻近解释、执行和结局仍需复核。")
        else:
            origin_episode_ids = []
            episode_id = candidate_membership.get(checkpoint["checkpoint_id"])
            episode_basis = "candidate_cp_supported_relation_program_assembly"
            if episode_id is None:
                episode_id = "EP-DIAG-" + diagnostic["event_id"].removeprefix("EA-")
                episode_basis = "program_diagnostic_event_pair_not_original_episode"
            target_title = ("本次检查（类型及结果已遮蔽）" if pre_exam
                            else "本次检查报告（新证据）")
            mask_reason = ("检查前遮蔽整个目标检查组及可见文书中的检查相关表述，"
                           "仅保留检查前时间引导。" if pre_exam else
                           "当前报告作为新增证据完整可见；晚于报告可见时点的来源不纳入。")
        related = []
        pair = diagnostic_cp_pairs.get(checkpoint["checkpoint_id"])
        if pair:
            related.append(dict(
                checkpoint_id=pair[0], relation=pair[1], direction="outgoing",
                relation_source="program_diagnostic_event_pair",
                projection_explanation="同一检查事件的检查前评价与报告后再评价，由程序时间对象连接。"))
        for relation in candidate_relations:
            endpoints = (relation.get("source_checkpoint_id"),
                         relation.get("target_checkpoint_id"))
            if checkpoint["checkpoint_id"] not in endpoints:
                continue
            other = endpoints[1] if endpoints[0] == checkpoint["checkpoint_id"] else endpoints[0]
            if any(item["checkpoint_id"] == other and item["relation"] == relation.get("relation")
                   for item in related):
                continue
            related.append(dict(
                checkpoint_id=other, relation=relation.get("relation"),
                direction=("outgoing" if endpoints[0] == checkpoint["checkpoint_id"]
                           else "incoming"),
                confidence=relation.get("confidence"),
                projection_explanation=_candidate_relation_summary(relation),
                explanation_location="candidate_cp_relations.json_research_audit_not_current_projection",
                shared_management_goal_location=(
                    "candidate_cp_relations.json_research_audit_not_current_projection"
                    if relation.get("shared_management_goal") else None),
                basis_quote_count=len(relation.get("basis_quotes") or []),
                basis_quote_location="candidate_cp_relations.json_research_audit_not_current_projection",
                relation_source=relation.get("relation_source") or "candidate_cp_agent_recording",
                retrieval_clock_comparison=relation.get("retrieval_clock_comparison")))
        for relation in ([] if organized else relations):
            endpoints = (relation.get("source_event_id"), relation.get("target_event_id"))
            target_cps = {checkpoint_by_event.get(event_id) for event_id in endpoints} - {None}
            if checkpoint["checkpoint_id"] not in target_cps:
                continue
            for other in target_cps - {checkpoint["checkpoint_id"]}:
                related.append(dict(checkpoint_id=other,
                                    relation=relation.get("relation"),
                                    confidence=relation.get("confidence"),
                                    explanation=relation.get("explanation")))
        if checkpoint["kind"] in MANAGEMENT_KINDS:
            document_profiles = sorted({migration_by_event[event_id].get("origin_document_profile")
                                        for event_id in checkpoint["member_event_ids"]
                                        if migration_by_event.get(event_id, {}).get("origin_document_profile")})
        else:
            document_profiles = ["performed_test_report"]
        cycle = cycle_by_id.get(checkpoint.get("action_cycle_id")) or {}
        evidence_state = state_by_id.get(checkpoint.get("evidence_state_id")) or {}
        packet = dict(
            format=FORMAT, case_id=case_id,
            checkpoint_id=checkpoint["checkpoint_id"], kind=checkpoint["kind"],
            management_goal=deepcopy(checkpoint.get("management_goal")
                                     or cycle.get("goal")),
            phase=checkpoint.get("phase") or cycle.get("phase"),
            action_cycle_id=checkpoint.get("action_cycle_id"),
            action_cycle=deepcopy(cycle) or None,
            evidence_state_id=checkpoint.get("evidence_state_id"),
            evidence_state=deepcopy(evidence_state) or None,
            input_evidence_ids=list(checkpoint.get("input_evidence_ids")
                                    or cycle.get("input_evidence_ids") or []),
            support_source_ids=list(checkpoint.get("support_source_ids")
                                    or cycle.get("support_source_ids") or []),
            candidate_status=checkpoint["candidate_status"],
            review_status=checkpoint["review_status"],
            management_question=checkpoint["management_question"],
            management_question_source=checkpoint["management_question_source"],
            evaluation=dict(clock=deepcopy(checkpoint["clock"]),
                            time_basis=checkpoint["time_basis"],
                            role=checkpoint["evaluation_time_role"],
                            clock_degradation=deepcopy(checkpoint.get("clock_degradation")),
                            primary_evaluation_eligible=checkpoint.get(
                                "primary_evaluation_eligible", True),
                            scope=checkpoint.get(
                                "evaluation_scope",
                                "PRIMARY_TASK_PREOPERATIVE_OR_NAT_REASSESSMENT"),
                            scope_reason=checkpoint.get("evaluation_scope_reason")),
            target=dict(source_ids=list(checkpoint["source_ids"]), title=target_title,
                        document_profile=" / ".join(document_profiles) or "未分类",
                        diagnostic_event_id=checkpoint.get("diagnostic_event_id"),
                        created_after_cutoff_source_ids=[
                            item["source_id"] for item in excluded
                            if item["source_id"] in target_ids
                            and item.get("reason") == "SOURCE_CREATED_AFTER_CANDIDATE_CUTOFF"],
                        body_creation_time_conflicts=body_creation_time_conflicts),
            episode_id=episode_id, episode_basis=episode_basis,
            candidate_organization_applied=organized,
            origin_episode_ids=origin_episode_ids,
            record_functions=list(checkpoint.get("record_functions") or []),
            supporting_event_ids=list(checkpoint.get("supporting_event_ids") or []),
            global_adjudication=deepcopy(checkpoint.get("global_adjudication")),
            supporting_adjudications=deepcopy(checkpoint.get("supporting_adjudications") or []),
            action_space=deepcopy(checkpoint["action_space"]),
            task_header=_task_header(checkpoint, cycle, included),
            included_source_ids=[source["source_id"] for source in included],
            candidate_actions=deepcopy(checkpoint.get("candidate_actions") or []),
            action_task_review_flags=list(checkpoint.get("action_task_review_flags") or []),
            action_task_review_reason=checkpoint.get("action_task_review_reason"),
            discovery_review_status=(checkpoint.get("discovery_review") or {}).get("disposition"),
            included_sources=included, excluded_sources=excluded,
            researcher_target_originals=originals,
            masked_projection=dict(
                reason=mask_reason, masked_spans=mask_metadata,
                whole_source_ids=whole_masked,
                excluded_support_source_ids=sorted(set(excluded_support_masked)),
                cross_document_reviewed=False,
                protocol_status="private_development_projection_not_doctor_release",
                heuristic_diagnostic_restatement_mask=bool(pre_exam),
                span_offset_basis="deidentified_source_text_used_for_episode_reconstruction"),
            direct_identifier_deidentification=dict(
                method="FHIR_INGRESS_STRUCTURED_VALUES_EXPLICIT_LABELS_AND_CROSS_SOURCE_LITERALS",
                ages_and_sex_retained=True, ages_over_89_grouped=True,
                exact_dates_retained_for_sequential_evaluation=True,
                temporal_masking_is_separate=True, hipaa_safe_harbor_claimed=False),
            related_checkpoints=related,
            migration_event_ids=list(checkpoint.get("member_event_ids") or []),
            migration_records=[dict(
                event_id=item.get("event_id"), migration_type=item.get("migration_type"),
                migration_status=item.get("migration_status"), reason=item.get("reason"),
                source_id=item.get("source_id"))
                for item in material_migrations
                if item.get("to_checkpoint_id") == checkpoint["checkpoint_id"]],
            clinical_reference_created=False, doctor_answer_prefilled=False,
            not_doctor_release=True)
        packet = deidentify_value(packet)
        packet["content_sha256"] = digest(packet)
        packets.append(packet)
    no_context_ids = {
        packet["checkpoint_id"] for packet in packets
        if not any(source["source_id"] not in set(
            packet["masked_projection"]["whole_source_ids"]) and source["text"].strip()
                   for source in packet["included_sources"])}
    if no_context_ids:
        packets = [packet for packet in packets
                   if packet["checkpoint_id"] not in no_context_ids]
        for migration in material_migrations:
            if migration.get("to_checkpoint_id") in no_context_ids:
                migration.update(migration_status="REVIEW_REQUIRED", status="REVIEW_REQUIRED",
                                 reason="NO_VISIBLE_SOURCE_AT_CANDIDATE_CUTOFF",
                                 to_checkpoint_id=None)
    material_diagnostic_events = deepcopy(projection["diagnostic_events"])
    for event in material_diagnostic_events:
        event["candidate_checkpoint_ids"] = {
            kind: checkpoint_id for kind, checkpoint_id
            in (event.get("candidate_checkpoint_ids") or {}).items()
            if checkpoint_id not in no_context_ids}
    no_context_cycle_ids = {checkpoint.get("action_cycle_id")
                            for checkpoint in projection["candidate_checkpoints"]
                            if checkpoint["checkpoint_id"] in no_context_ids}
    material_episodes = []
    projection_by_id = {checkpoint["checkpoint_id"]: checkpoint
                        for checkpoint in projection["candidate_checkpoints"]}
    for episode in deepcopy(projection.get("management_episodes") or []):
        episode["checkpoint_ids"] = [checkpoint_id for checkpoint_id
                                     in episode.get("checkpoint_ids") or []
                                     if checkpoint_id not in no_context_ids]
        episode["action_cycle_ids"] = [cycle_id for cycle_id
                                       in episode.get("action_cycle_ids") or []
                                       if cycle_id not in no_context_cycle_ids]
        episode["phase_transitions"] = []
        previous_phase = None
        for checkpoint_id in episode["checkpoint_ids"]:
            checkpoint = projection_by_id[checkpoint_id]
            phase = checkpoint.get("phase")
            if phase != previous_phase:
                episode["phase_transitions"].append(dict(
                    from_phase=previous_phase, to_phase=phase,
                    checkpoint_id=checkpoint_id,
                    clock=deepcopy(checkpoint.get("clock"))))
                previous_phase = phase
        if episode["checkpoint_ids"]:
            material_episodes.append(episode)
    material_cycles = [deepcopy(cycle) for cycle in projection.get("action_cycles") or []
                       if not (set(cycle.get("checkpoint_ids") or []) & no_context_ids)]
    material_states = [deepcopy(state) for state in projection.get("evidence_states") or []
                       if not (set(state.get("checkpoint_ids") or []) & no_context_ids)]
    material_support_records = deepcopy(projection["support_records"])
    for support in material_support_records:
        if support.get("to_checkpoint_id") in no_context_ids:
            support["to_checkpoint_id"] = None
            support["review_reason"] = "NO_VISIBLE_SOURCE_AT_CANDIDATE_CUTOFF"
    summary = deepcopy(projection["summary"])
    summary.update(
        material_checkpoint_count=len(packets),
        projection_primary_evaluation_checkpoint_count=summary.get(
            "primary_evaluation_checkpoint_count"),
        projection_non_primary_evaluation_checkpoint_count=summary.get(
            "non_primary_evaluation_checkpoint_count"),
        primary_evaluation_checkpoint_count=sum(
            packet["evaluation"]["primary_evaluation_eligible"] for packet in packets),
        non_primary_evaluation_checkpoint_count=sum(
            not packet["evaluation"]["primary_evaluation_eligible"] for packet in packets),
        material_action_cycle_count=len(material_cycles),
        material_evidence_state_count=len(material_states),
        material_no_visible_context_count=len(no_context_ids),
        material_no_visible_context_checkpoint_ids=sorted(no_context_ids),
        migration_status_counts=dict(sorted(Counter(
            item.get("migration_status") for item in material_migrations).items())),
        migration_type_counts=dict(sorted(Counter(
            item.get("migration_type") for item in material_migrations).items())),
        migration_reason_counts=dict(sorted(Counter(
            item.get("reason") for item in material_migrations).items())))
    _navigation(packets)
    packet_by_id = {packet["checkpoint_id"]: packet for packet in packets}
    for packet in packets:
        navigation = packet["episode_navigation"]
        for direction, key, relation in (
                ("incoming", "previous_checkpoint_id", "previous_state"),
                ("outgoing", "next_checkpoint_id", "next_state")):
            other_id = navigation.get(key)
            if not other_id or any(item.get("checkpoint_id") == other_id
                                   for item in packet["related_checkpoints"]):
                continue
            other = packet_by_id[other_id]
            packet["related_checkpoints"].append(dict(
                checkpoint_id=other_id, relation=relation, direction=direction,
                relation_source="program_management_trajectory_state_order",
                projection_explanation=(
                    "同一持续管理目标内的相邻证据/动作状态；显示顺序不等于已确认临床时序。"
                    + (" action cycle 发生变化。" if other.get("action_cycle_id")
                       != packet.get("action_cycle_id") else " 属于同一 action cycle 的时间投影。")),
                evidence_use_claimed=False))
        packet["content_sha256"] = digest({key: value for key, value in packet.items()
                                          if key != "content_sha256"})
    material = dict(format=FORMAT + "-set", case_id=case_id,
                    packets=packets, migrations=material_migrations,
                    support_records=material_support_records,
                    diagnostic_events=material_diagnostic_events,
                    management_episodes=material_episodes,
                    action_cycles=material_cycles,
                    evidence_states=material_states,
                    summary=summary, source_count=len(sources),
                    clinical_accuracy_assessed=False, not_doctor_release=True,
                    direct_identifier_deidentification=dict(
                        method="FHIR_INGRESS_STRUCTURED_VALUES_EXPLICIT_LABELS_AND_CROSS_SOURCE_LITERALS",
                        ages_and_sex_retained=True, ages_over_89_grouped=True,
                        exact_dates_retained_for_sequential_evaluation=True,
                        temporal_masking_is_separate=True,
                        hipaa_safe_harbor_claimed=False),
                    note="候选CP私有研究预览；已做保守直接标识符去标识化，保留精确日期；研究原文和未来节点均嵌入HTML，不能作为医生盲评包。")
    return deidentify_value(material)


def candidate_material_manifest(material):
    manifest = {key: value for key, value in material.items()
                if key not in {"packets", "migrations", "support_records", "diagnostic_events"}}
    manifest["packets"] = [dict(checkpoint_id=packet["checkpoint_id"],
                                kind=packet["kind"], episode_id=packet["episode_id"],
                                target_clock=_clock_value(packet["evaluation"]["clock"]),
                                content_sha256=packet["content_sha256"],
                                included_sources=len(packet["included_sources"]),
                                excluded_sources=len(packet["excluded_sources"]))
                           for packet in material["packets"]]
    return manifest


def candidate_material_manifest_sha256(material):
    return digest(candidate_material_manifest(material))


def write_candidate_material_set(material, output):
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    for packet in material["packets"]:
        (output / (packet["checkpoint_id"] + ".json")).write_text(
            json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = candidate_material_manifest(material)
    manifest["manifest_sha256"] = candidate_material_manifest_sha256(material)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "migrations.json").write_text(
        json.dumps(material["migrations"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (output / "diagnostic_events.json").write_text(
        json.dumps(material["diagnostic_events"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (output / "support_records.json").write_text(
        json.dumps(material["support_records"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return manifest
