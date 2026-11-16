"""Private timed materials for reprojected candidate Checkpoints."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from .materials import digest


FORMAT = "p2-candidate-checkpoint-material-0.4.0"


def _clock_value(clock):
    return (clock or {}).get("value") if isinstance(clock, dict) else clock


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
        aliases.update(("增强CT", "CT增强", "CT增强造影"))
    for source_id in event.get("source_ids", []):
        title = (source_by_id.get(source_id) or {}).get("title")
        if title:
            aliases.add(title)
    return sorted(aliases, key=len, reverse=True)


def _mask_management_action_lines(text):
    import re
    terms = ("拟施", "拟行", "已申请", "已签署", "治疗计划", "检查安排",
             "输血", "冰冻病理", "排除手术禁忌", "限期行手术", "定于",
             "手术指征", "手术切除", "手术方式", "手术时间", "根治性胰", "Whipple",
             "限期行", "择期行", "拟实施", "故拟", "需手术", "签署知情同意", "积极术前准备",
             "建议", "转肿瘤内科", "系统化疗", "化疗", "放疗", "免疫治疗", "靶向治疗",
             "保守治疗", "转诊", "随访", "复查")
    spans = []
    for match in re.finditer(r"[^。；\n]+[。；\n]?", text):
        clause = match.group(0)
        if clause and any(term in clause for term in terms):
            spans.append(dict(start=match.start(), end=match.end(),
                              mask_reason="ADJACENT_MANAGEMENT_ACTION_HEURISTIC"))
    return text, spans


def _future_datetime_spans(text, cutoff):
    import re
    if not cutoff:
        return []
    spans = []
    pattern = r"20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?"
    for match in re.finditer(pattern, text):
        normalized = match.group(0).replace("年", "-").replace("月", "-")
        normalized = normalized.replace("日", "").replace("/", "-").replace("T", " ")
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
    groups = {}
    for packet in sorted(packets, key=lambda item: (
            item["episode_id"] or "EP-UNASSIGNED",
            _clock_value(item["evaluation"]["clock"]) or "9999",
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
        _clock_value(item["evaluation"]["clock"]) or "9999", item["checkpoint_id"]))
    for index, packet in enumerate(ordered):
        packet["display_order"] = dict(index=index + 1, total=len(packets))
        packet["sequence_navigation"] = dict(
            index=index + 1, total=len(ordered),
            previous_checkpoint_id=(ordered[index - 1]["checkpoint_id"] if index else None),
            next_checkpoint_id=(ordered[index + 1]["checkpoint_id"]
                                if index + 1 < len(ordered) else None),
            order_role="whole_case_display_order_not_confirmed_clinical_sequence")


def project_candidate_materials(*, case_id, sources, candidates, projection,
                                relations=None, episodes=None,
                                candidate_relations=None, candidate_episodes=None):
    """Project candidate CPs; original text remains a separate researcher layer."""
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
        if ids.get("pre_exam_evaluation") and ids.get("evidence_arrival_reassessment"):
            diagnostic_cp_pairs[ids["pre_exam_evaluation"]] = (
                ids["evidence_arrival_reassessment"], "evidence_arrival")
            diagnostic_cp_pairs[ids["evidence_arrival_reassessment"]] = (
                ids["pre_exam_evaluation"], "pre_exam_evaluation")

    packets = []
    for checkpoint in projection["candidate_checkpoints"]:
        cutoff = _clock_value(checkpoint["clock"])
        target_ids = set(checkpoint["source_ids"])
        diagnostic = diagnostic_by_id.get(checkpoint.get("diagnostic_event_id"))
        pre_exam = checkpoint["kind"] == "pre_exam_evaluation"
        target_spans_by_source = {}
        for span in checkpoint.get("masked_spans") or []:
            target_spans_by_source.setdefault(span["source_id"], []).append(span)
        for span in (_same_event_spans(checkpoint, candidates, relations)
                     + _same_checkpoint_spans(checkpoint, projection, candidate_relations)):
            target_spans_by_source.setdefault(span["source_id"], []).append(span)
        if pre_exam or checkpoint["kind"] == "evidence_arrival_reassessment":
            for candidate in candidates:
                if candidate.get("status") != "ACCEPTED" or not candidate.get("span"):
                    continue
                span = candidate["span"]
                if pre_exam or span.get("source_id") not in target_ids:
                    target_spans_by_source.setdefault(span["source_id"], []).append(span)
        aliases = _diagnostic_aliases(diagnostic, source_by_id) if pre_exam else []
        included, excluded, mask_metadata = [], [], []
        whole_masked = []
        for source in sources:
            source_id = source["source_id"]
            visible_clock = effective_clocks[source_id]
            visible_value = _clock_value(visible_clock)
            is_masked_pre_exam_target = pre_exam and source_id in target_ids
            if not is_masked_pre_exam_target and (
                    not cutoff or not visible_value or visible_value > cutoff):
                excluded.append(dict(source_id=source_id,
                                     disposition="EXCLUDED_BY_CANDIDATE_TIMELINE"))
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
                if checkpoint["kind"] == "management_decision" or (
                        checkpoint["kind"] == "evidence_arrival_reassessment"
                        and source_id not in target_ids):
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
            included.append(card)
        originals = []
        for source_id in checkpoint["source_ids"]:
            source = source_by_id.get(source_id)
            if source is not None:
                original = _source_card(source, source.get("clock") or {})
                original["presentation_scope"] = "researcher_original_not_doctor_projection"
                originals.append(original)
        if checkpoint["kind"] == "management_decision":
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
        if checkpoint["kind"] == "management_decision":
            document_profiles = sorted({migration_by_event[event_id].get("origin_document_profile")
                                        for event_id in checkpoint["member_event_ids"]
                                        if migration_by_event.get(event_id, {}).get("origin_document_profile")})
        else:
            document_profiles = ["performed_test_report"]
        packet = dict(
            format=FORMAT, case_id=case_id,
            checkpoint_id=checkpoint["checkpoint_id"], kind=checkpoint["kind"],
            candidate_status=checkpoint["candidate_status"],
            review_status=checkpoint["review_status"],
            management_question=checkpoint["management_question"],
            management_question_source=checkpoint["management_question_source"],
            evaluation=dict(clock=deepcopy(checkpoint["clock"]),
                            time_basis=checkpoint["time_basis"],
                            role=checkpoint["evaluation_time_role"]),
            target=dict(source_ids=list(checkpoint["source_ids"]), title=target_title,
                        document_profile=" / ".join(document_profiles) or "未分类",
                        diagnostic_event_id=checkpoint.get("diagnostic_event_id")),
            episode_id=episode_id, episode_basis=episode_basis,
            candidate_organization_applied=organized,
            origin_episode_ids=origin_episode_ids,
            record_functions=list(checkpoint.get("record_functions") or []),
            supporting_event_ids=list(checkpoint.get("supporting_event_ids") or []),
            action_space=deepcopy(checkpoint["action_space"]),
            candidate_actions=deepcopy(checkpoint.get("candidate_actions") or []),
            discovery_review_status=(checkpoint.get("discovery_review") or {}).get("disposition"),
            included_sources=included, excluded_sources=excluded,
            researcher_target_originals=originals,
            masked_projection=dict(
                reason=mask_reason, masked_spans=mask_metadata,
                whole_source_ids=whole_masked,
                cross_document_reviewed=False,
                protocol_status="private_development_projection_not_doctor_release",
                heuristic_diagnostic_restatement_mask=bool(pre_exam)),
            related_checkpoints=related,
            migration_event_ids=list(checkpoint.get("member_event_ids") or []),
            clinical_reference_created=False, doctor_answer_prefilled=False,
            not_doctor_release=True)
        packet["content_sha256"] = digest(packet)
        packets.append(packet)
    _navigation(packets)
    for packet in packets:
        packet["content_sha256"] = digest({key: value for key, value in packet.items()
                                          if key != "content_sha256"})
    return dict(format=FORMAT + "-set", case_id=case_id,
                packets=packets, migrations=projection["migrations"],
                support_records=projection["support_records"],
                diagnostic_events=projection["diagnostic_events"],
                summary=projection["summary"], source_count=len(sources),
                clinical_accuracy_assessed=False, not_doctor_release=True,
                note="候选CP私有研究预览；研究原文和未来节点均嵌入HTML，不能作为医生盲评包。")


def write_candidate_material_set(material, output):
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    for packet in material["packets"]:
        (output / (packet["checkpoint_id"] + ".json")).write_text(
            json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {key: value for key, value in material.items()
                if key not in {"packets", "migrations", "support_records", "diagnostic_events"}}
    manifest["packets"] = [dict(checkpoint_id=packet["checkpoint_id"],
                                kind=packet["kind"], episode_id=packet["episode_id"],
                                target_clock=_clock_value(packet["evaluation"]["clock"]),
                                content_sha256=packet["content_sha256"],
                                included_sources=len(packet["included_sources"]),
                                excluded_sources=len(packet["excluded_sources"]))
                           for packet in material["packets"]]
    manifest["manifest_sha256"] = digest(manifest)
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
