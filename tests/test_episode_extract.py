"""HEWV semantic regressions, using fictional records only."""

from copy import deepcopy
import pytest

from pancreatic_agent.episode_extract import VERSION, extract_episode_case
from pancreatic_agent.four_stage import sha
from test_four_stage import image, lab, note


@pytest.fixture
def records():
    return {"documents_all": [
        note("2030-01-03 12:00 首次病程\n病史：2029-11-10外院CT提示占位。\n"
             "查体：腹部柔软。\n初步诊断：疑似肿瘤。\n诊疗计划：限期行手术治疗。"),
        note("2030-01-04 12:00 主诊查房\n拟行胰体癌根治术。", "主诊查房"),
    ], "imaging_all": [image("2030-01-01 08:00:00"),
                        image("2030-01-02 09:00:00", "胰腺MR增强", "mr", "整份报告\n另一行原文")],
            "laboratory_all": [lab("2030-01-01 08:00:01", "early"),
                               lab("2030-01-03 10:00:00", "marker"),
                               lab("2030-01-03 11:00:00", "later")], "pathology_all": []}


def review_for(b):
    return {"contract_version": VERSION, "source_bundle_sha256": sha(b), "reviewer": "Fictional source review",
            "source_scope": {"default_origin": "hospital", "reason": "Fictional hospital export"},
            "episode": {"entry_source_id": "IMG0001", "entry_basis": "FIRST_OBSERVED_HOSPITAL_RECORD",
                        "reason": "Earliest observable hospital disease record; first visit unknown"},
            "imaging_exam_proxy": {"accepted": True, "reason": "Fictional declared report-return assumption"},
            "checkpoints": [
                {"stage": 1, "source_ids": ["IMG0001"], "position": "after_records", "reason": "Observed lesion"},
                {"stage": 2, "source_ids": ["IMG0002"], "position": "after_records", "reason": "Staging update"},
                {"stage": 3, "source_ids": ["LAB0002"], "position": "after_records", "reason": "Evidence update, not an automatic stage transition"},
                {"stage": 4, "source_ids": ["DOC0001"], "position": "before_record", "task_mode": "independent_decision",
                 "reason": "Before first identifiable surgical recommendation"}]}


def run(b, r=None):
    return extract_episode_case(b, "S001", r or review_for(b))


def test_hospital_entry_does_not_backdate_external_history(records):
    r, a, timeline = run(records)
    assert r["episode"]["entry_time"] == "2030-01-01 08:00:00"
    assert r["episode"]["first_visit_confirmed"] is False
    assert r["packets"][0]["counts"] == {"imaging": 1}
    assert all("-H" not in x["source_id"] for x in timeline)
    assert a["source_count"] == 7 and not a["retrospective_backfill"]


def test_fourth_boundary_precedes_first_general_plan_and_evidence_can_differ(records):
    r, _, _ = run(records)
    assert r["packets"][2]["counts"]["laboratory"] == 2
    assert r["packets"][3]["counts"]["laboratory"] == 3
    assert r["packets"][2]["context_sha256"] != r["packets"][3]["context_sha256"]
    assert "限期行手术治疗" not in r["packets"][3]["context"]
    assert all(len(p["messages"]) == 2 for p in r["packets"])


def test_plan_review_preserves_all_earlier_diagnoses_and_plans(records):
    review = review_for(records)
    review["checkpoints"][3].update(source_ids=["DOC0002"], task_mode="plan_review")
    result, audit, _ = run(records, review)
    original = records["documents_all"][0]["document_text"]
    p = result["packets"][3]
    assert original in p["context"]
    assert p["records"][2]["body"] == original
    assert "疑似肿瘤" in p["context"] and "限期行手术治疗" in p["context"]
    assert all("span" not in x for x in audit["source_decisions"])


def test_later_specific_plan_is_not_silently_called_independent_decision(records):
    review = review_for(records)
    review["checkpoints"][3]["source_ids"] = ["DOC0002"]
    result, _, _ = run(records, review)
    p = result["packets"][3]
    assert p["status"] == "UNRESOLVED" and p["messages"] is None
    assert any(x.startswith("EARLIER_SURGICAL_PLAN_CANDIDATE") for x in p["issues"])
    assert records["documents_all"][0]["document_text"] in p["context"]


def test_mixed_version_document_is_held_whole_until_review(records):
    records["documents_all"].append(note("2030-01-02 10:00 入院记录\n查体正常。\n最后诊断：后来结果。\n2030-01-05 10:00"))
    result, audit, timeline = run(records)
    assert "查体正常" not in result["packets"][3]["context"]
    assert any(x["reason"] == "DOCUMENT_VERSION_REQUIRES_REVIEW" for x in audit["source_decisions"])
    source = next(x for x in timeline if x["source_id"] == "DOC0003")
    assert source["body"] == records["documents_all"][-1]["document_text"]
    review = review_for(records)
    review["document_version_reviews"] = {"DOC0003": {"body_sha256": sha(source["body"]),
        "whole_record_available": True, "reason": "Fictional independent version audit: later date was a documented future appointment"}}
    result, _, _ = run(records, review)
    assert source["body"] in result["packets"][3]["context"]


def test_whole_version_review_cannot_repair_missing_completion(records):
    records["documents_all"].append(note("无正文完成时间的完整资料"))
    review = review_for(records)
    review["document_version_reviews"] = {"DOC0003": {"body_sha256": sha(records["documents_all"][-1]["document_text"]),
        "whole_record_available": True, "reason": "Cannot manufacture a completion timestamp"}}
    result, _, _ = run(records, review)
    assert "无正文完成时间" not in result["packets"][3]["context"]


def test_no_title_filter_no_pathology_type_filter_and_no_display_dedup(records):
    records["documents_all"].append(note("2029-12-30 09:00\n既往手术完成的全部原始记录。", "术后首次病程记录"))
    records["pathology_all"] = [{"report_date": "2029-12-30", "specimen_type": "手术标本", "pathology_diagnosis": "既往病理全文"}]
    records["imaging_all"].append(deepcopy(records["imaging_all"][0]))
    result, _, timeline = run(records)
    p = result["packets"][0]
    assert p["counts"] == {"document": 1, "pathology": 1, "imaging": 2}
    assert "既往手术完成" in p["context"] and "既往病理全文" in p["context"]
    assert len(timeline) == 10


def test_equal_return_included_later_same_day_excluded(records):
    records["laboratory_all"].append(lab("2030-01-01 08:00:00", "equal"))
    result, _, _ = run(records)
    assert result["packets"][0]["counts"]["laboratory"] == 1
    records["laboratory_all"][-1]["report_time"] = "2030-01-01"
    result, _, _ = run(records)
    assert result["packets"][0]["counts"].get("laboratory", 0) == 0


def test_record_timestamp_precision_and_date_only_pathology(records):
    records["pathology_all"] = [{"report_date": "2030-01-03 00:00:00", "pathology_diagnosis": "病理全文"}]
    result, _, _ = run(records)
    assert not result["packets"][3]["counts"].get("pathology")
    records["pathology_all"][0]["report_date"] = "2030-01-02 00:00:00"
    result, _, _ = run(records)
    assert result["packets"][3]["counts"]["pathology"] == 1


def test_explicit_report_overrides_exam_and_invalid_report_has_no_fallback(records):
    records["imaging_all"][0]["report_time"] = "2030-01-01 08:30:00"
    result, _, _ = run(records)
    assert result["packets"][0]["counts"]["laboratory"] == 1
    assert result["packets"][0]["checkpoint"]["time"] == "2030-01-01 08:30:00"
    records["imaging_all"][0]["report_time"] = "invalid"
    result, _, _ = run(records)
    assert result["packets"][0]["messages"] is None


def test_no_default_proxy_and_unresolved_stage_not_filled_from_another(records):
    review = review_for(records)
    review.pop("imaging_exam_proxy")
    result, _, _ = run(records, review)
    assert result["packets"][0]["status"] == "UNRESOLVED"
    assert result["packets"][2]["messages"]
    review = review_for(records)
    review["checkpoints"][1]["source_ids"] = []
    result, _, _ = run(records, review)
    assert result["packets"][1]["messages"] is None
    assert result["packets"][2]["messages"]


def test_external_source_requires_hospital_receipt_and_cannot_anchor_entry(records):
    records["imaging_all"].append(image("2029-11-10 08:00:00", key="external"))
    review = review_for(records)
    review["source_scope"]["origins"] = {"IMG0003": "external"}
    result, _, _ = run(records, review)
    assert "IMG0003" not in {x["source_id"] for x in result["packets"][3]["records"]}
    records["imaging_all"][-1]["hospital_received_time"] = "2030-01-02 12:00:00"
    review["source_bundle_sha256"] = sha(records)
    result, _, _ = run(records, review)
    assert "IMG0003" in {x["source_id"] for x in result["packets"][3]["records"]}
    review["episode"]["entry_source_id"] = "IMG0003"
    with pytest.raises(ValueError, match="hospital"):
        run(records, review)


def test_confirmed_encounter_requires_native_clinical_time(records):
    review = review_for(records)
    review["episode"].update(entry_basis="REVIEWED_FIRST_HOSPITAL_ENCOUNTER", encounter_time_field="create_time")
    with pytest.raises(ValueError, match="schema|native"):
        run(records, review)
    records["imaging_all"][0]["visit_time"] = "2030-01-01 07:30:00"
    review["source_bundle_sha256"] = sha(records)
    review["episode"]["encounter_time_field"] = "visit_time"
    result, _, _ = run(records, review)
    assert result["episode"]["entry_time"] == "2030-01-01 07:30:00"
    assert result["episode"]["first_visit_confirmed"]


def test_archival_and_outcome_changes_cannot_affect_packets(records):
    first, _, _ = run(records)
    for row in records["documents_all"]:
        row["create_time"] = "2099-12-31 23:59:59"
    records["outcome_post_T0_isolated"] = {"outcome": "NO_INPUT"}
    records["state_pre_T0"] = {"resectable": "NO_INPUT"}
    second, _, _ = run(records)
    assert first == second


def test_alternative_lab_result_fields_are_not_silently_lost(records):
    records["laboratory_all"][0].update(result_raw=None, result_qualitative_value="虚构定性值", result_text_value="虚构备注")
    result, _, _ = run(records)
    assert "虚构定性值" in result["packets"][1]["context"]
    assert "虚构备注" in result["packets"][1]["context"]


@pytest.mark.parametrize("mutation", ["legacy", "hash", "order"])
def test_invalid_configuration_is_rejected(records, mutation):
    review = review_for(records)
    if mutation == "legacy": review["evidence_spans"] = []
    if mutation == "hash": review["source_bundle_sha256"] = "invalid"
    if mutation == "order": review["checkpoints"][2]["source_ids"] = ["IMG0001"]
    with pytest.raises(ValueError):
        run(records, review)
