"""Deterministic candidate Episode/Checkpoint research-material projection.

The relation graph is a candidate management-process organization, not a list of
confirmed clinical decisions. This module exposes the exact located candidate,
its masked source, its case-level relations, and its adjacent candidates without
turning display order or retrieval clocks into clinical decision times.

FORMAT 0.2 adds a researcher-only inspection layer. It does not make the packet
a doctor release: a final clinician packet must be derived under a frozen
protocol and must not contain hidden originals or unrelated later candidates.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..tools.masking import mask_source


def _replace_masked_spans(source, spans, replacement):
    """Apply already verified, non-overlapping spans without re-finding quotes."""
    pieces, cursor = [], 0
    for span in sorted(spans, key=lambda item: item["start"]):
        pieces.extend((source["text"][cursor:span["start"]],
                       "[" + span.get("_mask_label", replacement) + "已遮蔽]"))
        cursor = span["end"]
    pieces.append(source["text"][cursor:])
    return dict(text="".join(pieces), masked_spans=list(spans),
                protocol_status="program_projection_not_frozen_blinding_protocol")

FORMAT = "p2-candidate-checkpoint-material-0.2.0"
RESPONSE_FORMAT = "p2-candidate-checkpoint-review-response-0.2.0"
HTML_FORMAT = "p2-candidate-checkpoint-preview-0.2.0"
FORM_VERSION = "development-preview-0.2"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=str)
                          .encode("utf-8")).hexdigest()


def _future_source_spans(candidates, source_id, target_clock):
    """Mask only located candidates after this target's retrieval clock."""
    if not target_clock:
        return []
    return [candidate["span"] for candidate in candidates
            if candidate.get("status") == "ACCEPTED"
            and (candidate.get("span") or {}).get("source_id") == source_id
            and (candidate.get("clock") or {}).get("value")
            and candidate["clock"]["value"] > target_clock]


def _navigation(material_packets):
    """Display order is explicit and stable; it is not a clinical time order."""
    ordered = sorted(material_packets, key=lambda packet: (
        packet["target"]["clock"].get("value") or "9999",
        packet["checkpoint_id"]))
    by_episode = {}
    for index, packet in enumerate(ordered):
        packet["display_order"] = dict(index=index + 1, total=len(ordered),
                                       basis="retrieval_clock_then_checkpoint_id_unknown_last")
        siblings = by_episode.setdefault(packet["episode_id"], [])
        packet["episode_navigation"] = dict(index=len(siblings) + 1)
        siblings.append(packet)
    for siblings in by_episode.values():
        for index, packet in enumerate(siblings):
            navigation = packet["episode_navigation"]
            navigation.update(
                total=len(siblings),
                previous_checkpoint_id=(siblings[index - 1]["checkpoint_id"]
                                        if index else None),
                next_checkpoint_id=(siblings[index + 1]["checkpoint_id"]
                                    if index + 1 < len(siblings) else None),
                order_role="candidate_display_order_not_confirmed_clinical_sequence")


def project_materials(*, case_id, sources, candidates, evidence_inventory,
                      relations, episodes):
    """Build one private research material record per located candidate.

    Program candidates are retained as possible Checkpoint anchors. Original
    candidate spans are exposed only for research audit, never as a clinician
    answer. Every source visible in a timed packet remains masked as required;
    the separate researcher span is labelled and is not part of a doctor packet.
    """
    source_by_id = {source["source_id"]: source for source in sources}
    candidate_by_id = {candidate["event_id"]: candidate for candidate in candidates}
    evidence_by_id = {item["event_id"]: item for item in evidence_inventory}
    memberships = {event_id: episode["episode_id"]
                   for episode in episodes for event_id in episode["event_ids"]}
    packets = []
    for candidate in candidates:
        event_id = candidate["event_id"]
        episode_id = memberships.get(event_id)
        clock = candidate.get("clock") or {}
        visible, excluded = [], []
        target_source = source_by_id.get((candidate.get("span") or {}).get("source_id"))
        if target_source is None:
            raise ValueError("CANDIDATE_SOURCE_MISSING:" + event_id)
        target_record = dict(
            source_id=target_source["source_id"], title=target_source.get("title"),
            kind=target_source.get("kind"), clock=target_source.get("clock"),
            clock_basis=target_source.get("clock_basis"),
            clocks=target_source.get("clocks") or {},
            source_sha256=target_source.get("source_sha256"),
            text=target_source.get("text") or "",
            text_role="target_original_text_for_time_review_not_blinded_clinician_packet")
        if not clock.get("value"):
            visible.append(target_record)
        for source in sources:
            source_clock = (source.get("clock") or {}).get("value")
            if not clock.get("value"):
                disposition = "REVIEW_CHOICE_TIME_UNKNOWN"
            elif not source_clock:
                disposition = "REVIEW_SOURCE_CLOCK_UNKNOWN"
            elif source_clock <= clock["value"]:
                disposition = "INCLUDE_BY_RETRIEVAL_CLOCK"
            else:
                disposition = "EXCLUDE_AFTER_CANDIDATE_RETRIEVAL_CLOCK"
            if disposition != "INCLUDE_BY_RETRIEVAL_CLOCK":
                excluded.append(dict(source_id=source["source_id"], disposition=disposition))
                continue
            is_target = candidate["span"]["source_id"] == source["source_id"]
            # Validate the exact source and every selected span in one pass,
            # then project by offsets so each span has a distinct audit label.
            mask_source(source, _future_source_spans(
                candidates, source["source_id"], clock.get("value"))
                + ([candidate["span"]] if is_target else []))
            current_spans = ([dict(candidate["span"], _mask_label="当前候选定位原文")]
                             if is_target else [])
            future_spans = [dict(span, _mask_label="目标时点之后的候选定位原文")
                            for span in _future_source_spans(
                                candidates, source["source_id"], clock.get("value"))]
            masked = _replace_masked_spans(source, current_spans + future_spans,
                                           "候选定位原文")
            visible.append(dict(
                source_id=source["source_id"], title=source.get("title"),
                kind=source.get("kind"), clock=source.get("clock"),
                clock_basis=source.get("clock_basis"), clocks=source.get("clocks") or {},
                source_sha256=source.get("source_sha256"),
                text=masked["text"], masked_text_sha256=digest(masked["text"]),
                masked_candidate_count=len(masked["masked_spans"]),
                protocol_status=masked["protocol_status"],
                presentation_scope=("current_candidate_source_masked_projection" if is_target
                                    else "within_clock_source_with_future_candidates_masked")))
        evidence = []
        for relation in relations:
            if relation.get("target_event_id") != event_id \
                    or relation.get("relation") != "evidence_used":
                continue
            item = evidence_by_id.get(relation["source_event_id"])
            if item is not None:
                evidence.append(dict(relation=relation, evidence=item))
        related_candidates = []
        for relation in relations:
            if relation.get("relation") == "evidence_used":
                continue
            endpoints = (relation.get("source_event_id"), relation.get("target_event_id"))
            if event_id not in endpoints:
                continue
            other_id = next((item for item in endpoints if item != event_id), None)
            other = candidate_by_id.get(other_id)
            if other is None:
                continue
            related_candidates.append(dict(
                relation=relation, checkpoint_id="CP-" + other_id.removeprefix("EV-"),
                candidate_event_id=other_id, episode_id=memberships.get(other_id),
                record_function=other.get("record_function"), clock=other.get("clock"),
                span=dict(other.get("span") or {}),
                source_title=(source_by_id.get((other.get("span") or {}).get("source_id"))
                              or {}).get("title"),
                within_current_episode=memberships.get(other_id) == episode_id))
        packet = dict(
            format=FORMAT, case_id=case_id,
            checkpoint_id="CP-" + event_id.removeprefix("EV-"),
            candidate_event_id=event_id,
            episode_id=episode_id,
            status="candidate_checkpoint_not_expert_selected",
            target=dict(clock=clock, clock_basis=candidate.get("clock_basis"),
                        clock_role="document_retrieval_clock_not_confirmed_choice_time",
                        record_function=candidate.get("record_function"),
                        source_id=(candidate.get("span") or {}).get("source_id"),
                        source_title=target_source.get("title"),
                        source_kind=target_source.get("kind"),
                        source_clocks=target_source.get("clocks") or {},
                        retrieval_scope="complete_source_projection_not_whole_patient_record"),
            researcher_candidate_inspection=dict(
                visibility="private_research_audit_not_doctor_release",
                actor="discovery_llm_proposal_with_program_span_verification",
                record_function=candidate.get("record_function"),
                status=candidate.get("status"), scope=candidate.get("scope"),
                span=dict(candidate.get("span") or {}),
                rationale=candidate.get("rationale"), uncertainty=candidate.get("uncertainty"),
                citation_verdicts=candidate.get("citation_verdicts") or [],
                discovery_unit_id=candidate.get("discovery_unit_id"),
                note="这是发现阶段LLM提议的连续原文区间，不是专家确认的独立临床决策点。"),
            candidate_episode=dict(
                event_ids=next((item["event_ids"] for item in episodes
                                if item["episode_id"] == episode_id), [event_id]),
                open_questions=next((item["open_questions"] for item in episodes
                                     if item["episode_id"] == episode_id), [])),
            related_candidates=related_candidates,
            evidence_used=evidence,
            evidence_use_note=("仅呈现关系阶段Agent记录且程序核验的引用；不因时间或内容相似"
                               "自动补造引用。时点内历史资料不等于该判断实际使用的证据。"),
            included_sources=visible, excluded_sources=excluded,
            blinding=dict(
                protocol_status="development_projection_not_frozen_blinding_protocol",
                target_candidate_masked_in_its_source=bool(clock.get("value")),
                prior_source_content_preserved=True,
                only_target_and_candidates_after_target_clock_masked=bool(clock.get("value")),
                other_located_candidates_in_same_source_masked="only_target_or_future",
                hidden_original_text_in_packet=False,
                later_candidate_events_included=False,
                researcher_original_span_separate_from_masked_source=True,
                adjacent_explanations_results_and_leakage_reviewed=False,
                note=("这是一份本地研究材料，不是已发布医生包。目标时钟未知的原文明确标记为待核，"
                      "不是已遮蔽空包。历史决策与手术保留；目标之后的已定位候选遮蔽；"
                      "未标出的解释、结果、未来信息仍需核对。")),
            clinical_reference_created=False,
            doctor_answer_prefilled=False,
            reviewer_actions=dict(
                acceptable_modes="由研究方案/医生填写，不由程序预设",
                acceptable_actions="由研究方案/医生填写，不由程序预设",
                evidence_evolution_review="比较A/B/C记录及证据随时间的变化，不以后果反推此前选择"),
        )
        packet["content_sha256"] = digest({key: value for key, value in packet.items()
                                          if key != "content_sha256"})
        packets.append(packet)
    _navigation(packets)
    return dict(format=FORMAT + "-set", case_id=case_id,
                source_count=len(sources), candidate_count=len(candidates),
                episode_count=len(episodes), evidence_object_count=len(evidence_inventory),
                relation_count=len(relations), packets=packets,
                not_clinical_reference=True, not_doctor_release=True,
                note=("每个已定位候选均单独呈现为候选Checkpoint，供研究/医生判断其独立性；"
                      "候选Episode管理过程较宽不等于全部成员是一个Checkpoint。"
                      "候选切换顺序不是已确认的临床决策顺序。"))


def write_material_set(material, output):
    """Owner-only files, never an Artifact or clinician distribution."""
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    for packet in material["packets"]:
        (output / (packet["checkpoint_id"] + ".json")).write_text(
            json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {key: value for key, value in material.items() if key != "packets"}
    manifest["packets"] = [dict(checkpoint_id=packet["checkpoint_id"],
                               episode_id=packet["episode_id"],
                               target_clock=packet["target"]["clock"].get("value"),
                               content_sha256=packet["content_sha256"],
                               included_sources=len(packet["included_sources"]),
                               excluded_sources=len(packet["excluded_sources"]))
                           for packet in material["packets"]]
    manifest["manifest_sha256"] = digest(manifest)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
