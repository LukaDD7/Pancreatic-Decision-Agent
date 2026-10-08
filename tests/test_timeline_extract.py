"""Synthetic checks of inclusive checkpoints, history and timeline semantics."""

import pytest

from pancreatic_agent.four_stage import sha, parse_time
from pancreatic_agent.timeline_extract import (
    extract_timeline_case, semantic_diff, visible_at, write_result,
)
from test_four_stage import note, image, lab


@pytest.fixture
def bundle():
    return {"documents_all": [
        note("2030-01-03 10:00 首次病程记录\n2、患者于2029-11-10外院就诊，CT提示胰腺占位。\n3、查体：本次住院腹部柔软。\n诊疗计划：拟行手术治疗。"),
        note("2030-01-04 12:06 主诊查房\n拟行胰体癌根治术。", "主诊首次查房记录"),
        note("2030-01-05 13:00 术后首次记录\n术中发现远处病变。", "术后首次病程记录"),
    ], "imaging_all": [image("2030-01-01 08:00:00"),
                       image("2030-01-02 09:00:00", "胰腺MR平扫+增强", "mr", "完整MR原文")],
            "laboratory_all": [lab("2030-01-01 08:00:00", "equal"),
                               lab("2030-01-01 08:00:01", "later")], "pathology_all": []}


def review_for(b, **kwargs):
    return {"source_bundle_sha256": sha(b), "reviewer": "synthetic source review",
            "allow_imaging_exam_proxy": True, "imaging_proxy_reason": "Explicit test assumption", **kwargs}


def external_review(b):
    text = b["documents_all"][0]["document_text"]
    start, end = text.index("患者于"), text.index("\n3、")
    return review_for(b, discovery_source_id="DOC0001-H1", discovery_reason="Earliest dated external report reference",
                      evidence_spans=[{"source_id": "DOC0001", "start": start, "end": end,
                                       "source_text_sha256": sha(text), "event_time": "2029-11-10",
                                       "accepted": True, "stages": [1, 2, 3, 4], "reason": "Historical paragraph only"}])


def test_current_report_and_equal_report_time_included_but_later_not(bundle):
    result, _, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    p = result["packets"][0]
    assert p["checkpoint"]["relation"] == "<="
    assert {r["source_id"] for r in p["records"]} == {"IMG0001", "LAB0001"}
    assert p["counts"] == {"imaging": 1, "laboratory": 1}


def test_preplan_remains_exclusive_and_unknown_result_not_opened(bundle):
    bundle["laboratory_all"] += [lab("2030-01-04 12:05:59", "before"),
                                 lab("2030-01-04 12:06:00", "at-plan"), lab(None, "no-report")]
    result, _, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    p = result["packets"][2]
    assert p["checkpoint"]["relation"] == "<"
    assert p["counts"]["laboratory"] == 3
    assert "根治术" not in p["context"]
    assert result["packets"][2]["context"] == result["packets"][3]["context"]
    assert all(len(p["messages"]) == 2 for p in result["packets"])


def test_equal_second_note_completion_is_inclusive(bundle):
    bundle["documents_all"].append(note("2030-01-01 08:00:00 首次病程记录\n既往病史明确，当前已完成记录的原始临床资料应可见。"))
    result, _, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    assert "当前已完成记录的原始临床资料应可见" in result["packets"][0]["context"]


def test_minute_anchor_does_not_invent_end_of_minute(bundle):
    bundle["imaging_all"][1]["exam_datetime"] = "2030-01-02 09:00"
    bundle["laboratory_all"].append(lab("2030-01-02 09:00:30", "unknown-same-minute"))
    result, _, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    p = result["packets"][1]
    assert p["checkpoint"]["time"] == "2030-01-02 09:00"
    assert p["counts"]["laboratory"] == 2


def test_external_date_anchor_does_not_open_entire_day_or_later_note(bundle):
    bundle["laboratory_all"] += [lab("2029-11-09 23:59:59", "prior-day", sample="2029-11-09 08:00:00"),
                                 lab("2029-11-10 09:00:00", "unknown-order", sample="2029-11-10 08:00:00")]
    result, audit, timeline = extract_timeline_case(bundle, "S001", external_review(bundle))
    p = result["packets"][0]
    assert p["checkpoint"]["time"] == "2029-11-10"
    assert p["counts"] == {"document": 1, "laboratory": 1}
    assert "CT提示胰腺占位" in p["context"]
    assert "本次住院腹部柔软" not in p["context"]
    assert "REVIEWED_RETROSPECTIVE_HISTORY_RECONSTRUCTION" in p["issues"]
    assert timeline[1]["source_id"] == "DOC0001-H1"
    assert audit["lookback_window"] is None


def test_full_timeline_does_not_use_create_time(bundle):
    first, _, timeline1 = extract_timeline_case(bundle, "S001", review_for(bundle))
    for row in bundle["documents_all"]:
        row["create_time"] = "2099-12-31 23:59:59"
    second, audit, timeline2 = extract_timeline_case(bundle, "S001", review_for(bundle))
    assert first == second
    assert timeline1 == timeline2
    assert audit["create_time_used"] is False
    assert "2099-12-31" not in str(timeline2)


def test_reviewed_discovery_can_include_noncontiguous_same_event_citation(bundle):
    text = bundle["documents_all"][0]["document_text"]
    text += "\n辅助检查：外院CT（2029-11-10）：胰腺占位，少量腹水。"
    bundle["documents_all"][0]["document_text"] = text
    review = external_review(bundle)
    start = text.index("外院CT（")
    review["evidence_spans"].append({"source_id": "DOC0001", "start": start, "end": len(text),
        "source_text_sha256": sha(text), "event_time": "2029-11-10", "accepted": True,
        "stages": [1, 2], "reason": "Same external CT explicitly cited in auxiliary examinations"})
    review["discovery_companion_source_ids"] = ["DOC0001-H2"]
    bundle["laboratory_all"].append(lab("2029-11-10 08:00:00", "same-day-unrelated"))
    review["source_bundle_sha256"] = sha(bundle)
    result, _, _ = extract_timeline_case(bundle, "S001", review)
    p = result["packets"][0]
    assert p["counts"] == {"document": 2}
    assert "少量腹水" in p["context"]
    assert "本次住院腹部柔软" not in p["context"]
    review["evidence_spans"][1]["event_time"] = "2030-01-01"
    with pytest.raises(ValueError):
        extract_timeline_case(bundle, "S001", review)


def test_missing_body_completion_has_no_archival_fallback(bundle):
    bundle["documents_all"][1]["document_text"] = "拟行胰体癌根治术。"
    result, audit, timeline = extract_timeline_case(bundle, "S001", review_for(bundle))
    assert all(p["status"] == "UNRESOLVED" for p in result["packets"])
    assert audit["selected_plan_source_id"] is None
    assert next(x for x in timeline if x["source_id"] == "DOC0002")["sort_time"] is None


def test_old_scan_and_prior_admission_note_not_discarded_by_window(bundle):
    bundle["imaging_all"].append(image("2029-10-01 08:00:00", key="older"))
    bundle["documents_all"].append(note("2029-09-30 10:00 首次病程记录\n病史：既往相关疾病诊治过程，有明确原始记载。",
                                          admission="2029-09-30 09:00:00", visit="previous"))
    result, _, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    assert result["packets"][0]["checkpoint"]["anchor_source_ids"] == ["IMG0003"]
    assert "既往相关疾病诊治过程" in result["packets"][0]["context"]


def test_historical_treatment_retained_current_plan_removed(bundle):
    bundle["documents_all"][0]["document_text"] = (
        "2030-01-03 10:00 首次病程记录\n2、患者于2029-11-10确诊，当时暂无手术指征，决定行转化治疗。"
        "已完成化疗，疗效评估缩小。\n3、查体：一般情况可。\n诊疗计划：拟行手术治疗。")
    result, _, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    text = result["packets"][2]["context"]
    assert "暂无手术指征，决定行转化治疗" in text
    assert "已完成化疗，疗效评估缩小" in text
    assert "诊疗计划：拟行" not in text


def test_admission_recognized_from_body_and_blank_template_excluded(bundle):
    bundle["documents_all"].append(note("入 院 记 录\n记录时间：2030-01-03 09:30\n现病史：既往诊治经过明确。\n既往史：有高血压，正在用药。\n最后诊断：后来添加的混排尾部。", "非标准疾病模板"))
    bundle["documents_all"].append(note("2030-01-03 09:40 首次病程\n主诉：[主诉]\n病史：[现病史]\n诊疗计划：完善检查。"))
    result, audit, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    text = result["packets"][2]["context"]
    assert "有高血压，正在用药" in text
    assert "后来添加" not in text
    assert "[现病史]" not in text
    assert any(x["reason"] == "UNFILLED_CLINICAL_TEMPLATE" for x in audit["source_decisions"])


def test_actual_imaging_report_time_overrides_exam_proxy(bundle):
    bundle["imaging_all"][0]["report_time"] = "2030-01-02 10:00:00"
    result, _, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    assert result["packets"][0]["checkpoint"]["anchor_source_ids"] == ["IMG0002"]
    assert "IMG0001" not in {r["source_id"] for r in result["packets"][0]["records"]}


def test_invalid_report_field_never_falls_back_to_exam_time(bundle):
    bundle["imaging_all"][0]["report_time"] = "invalid"
    result, _, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    assert "IMG0001" not in {r["source_id"] for r in result["packets"][2]["records"]}


def test_imaging_proxy_must_be_explicitly_accepted(bundle):
    result, _, _ = extract_timeline_case(bundle, "S001")
    assert result["packets"][0]["status"] == "UNRESOLVED"
    r = review_for(bundle)
    del r["imaging_proxy_reason"]
    with pytest.raises(ValueError, match="reason"):
        extract_timeline_case(bundle, "S001", r)


def test_selected_staging_round_includes_each_anchor_and_no_later_scan(bundle):
    bundle["imaging_all"] += [image("2030-01-02 10:00:00", "胰腺CT增强", "enh-ct"),
                              image("2030-01-02 11:00:00", "胰腺CT增强", "later-ct")]
    r = review_for(bundle, staging_source_ids=["IMG0002", "IMG0003"], staging_reason="Declared two-report round")
    result, _, _ = extract_timeline_case(bundle, "S001", r)
    p = result["packets"][1]
    assert p["checkpoint"]["time"] == "2030-01-02 10:00:00"
    assert {r["source_id"] for r in p["records"] if r["kind"] == "imaging"} == {"IMG0001", "IMG0002", "IMG0003"}


def test_date_only_pathology_not_assumed_available_at_midnight(bundle):
    bundle["pathology_all"] = [{"report_date": "2030-01-02 00:00:00", "specimen_type": "细针穿刺", "pathology_diagnosis": "虚构病理"}]
    result, _, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    assert not result["packets"][1]["counts"].get("pathology")
    assert result["packets"][2]["counts"]["pathology"] == 1


def test_precision_aware_inclusive_and_exclusive():
    cp = {"time": "2030-01-01 10:00:00", "relation": "<="}
    assert visible_at(parse_time(cp["time"]), cp)
    assert not visible_at(parse_time("2030-01-01 10:00"), cp)
    assert not visible_at(parse_time("2030-01-01"), cp)
    assert visible_at(parse_time("2030-01-01"), cp, anchor=True)
    cp["relation"] = "<"
    assert not visible_at(parse_time(cp["time"]), cp)


def test_future_note_rejected_without_losing_other_sources(bundle):
    bundle["documents_all"].append(note("2030-01-03 11:00 首次病程\n病史：旧病史。\n检查：2030-01-06 MRI提示后来结果。"))
    result, audit, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    assert "后来结果" not in result["packets"][2]["context"]
    assert any(x["reason"] == "NOTE_CONTAINS_LATER_EVENT_REQUIRES_SPAN_REVIEW" for x in audit["source_decisions"])


def test_review_hash_and_original_date_are_bound(bundle):
    r = external_review(bundle)
    r["source_bundle_sha256"] = "wrong"
    with pytest.raises(ValueError, match="hash"):
        extract_timeline_case(bundle, "S001", r)
    r = external_review(bundle)
    r["evidence_spans"][0]["event_time"] = "2029-11-09"
    with pytest.raises(ValueError, match="event date"):
        extract_timeline_case(bundle, "S001", r)


def test_outcome_labels_do_not_choose_checkpoints_or_context(bundle):
    before, _, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    bundle["outcome_post_T0_isolated"] = {"outcome": "DO_NOT_INJECT"}
    after, _, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    assert before == after


def test_missing_anchor_text_and_future_path_never_fill_slots(bundle):
    bundle["imaging_all"][0]["report_deidentified"] = ""
    bundle["pathology_all"] = [{"report_date": "2030-01-10", "specimen_type": "小标本", "pathology_diagnosis": "最终病理"}]
    result, _, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    assert result["packets"][0]["status"] == "UNRESOLVED"
    assert all("最终病理" not in p["context"] for p in result["packets"])


def test_source_conflicts_retained_and_marked(bundle):
    bundle["imaging_all"].append({**bundle["imaging_all"][0], "report_deidentified": "不同原文，应保留冲突"})
    result, audit, _ = extract_timeline_case(bundle, "S001", review_for(bundle))
    assert result["packets"][0]["counts"]["imaging"] == 2
    assert audit["source_payload_conflicts"]


def test_semantic_diff_checks_payloads_and_stale_messages_removed(bundle, tmp_path):
    result, audit, timeline = extract_timeline_case(bundle, "S001", review_for(bundle))
    old = {"version": "old", "packets": [{**p, "boundary_exclusive": p["checkpoint"]["time"]} for p in result["packets"]]}
    diff = semantic_diff(old, result)
    assert diff["stages"][0]["old_checkpoint"] == old["packets"][0]["checkpoint"]
    assert all(x["distinct_payloads_added"] == x["distinct_payloads_removed"] == 0 for x in diff["stages"])
    write_result(tmp_path, result, audit, timeline, {"old": diff})
    assert (tmp_path / "S001/01_messages.json").exists()
    result["packets"][0]["messages"] = None
    write_result(tmp_path, result, audit, timeline, {})
    assert not (tmp_path / "S001/01_messages.json").exists()
