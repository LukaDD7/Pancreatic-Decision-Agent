"""Synthetic leakage and cohort-boundary checks; no clinical patient fixtures."""

from copy import deepcopy
from datetime import datetime

import pytest

from pancreatic_agent.four_stage import (
    extract_case, document_time, future_dates, protected_output, sha, write_case,
)


def note(text, title="首次病程记录", admission="2030-01-03 09:00:00", visit="1"):
    return {"document_title": title, "document_text": text,
            "create_time": "2029-12-01 00:00:00", "admission_time": admission,
            "visit_id": visit, "source_record_id": "fictional-" + sha(text)[:12]}


def image(time, method="上腹部CT平扫", key="fictional-image", body="原始完整报告：发现占位，建议进一步检查。"):
    return {"exam_method": method, "exam_datetime": time, "report_deidentified": body,
            "index_report_uid": key, "source_record_key": key}


def lab(report, key="fictional-lab", sample="2030-01-01 08:00:00"):
    return {"item_name": "虚构项目", "result_raw": ">10", "unit_raw": "U/L",
            "sample_time": sample, "report_time": report, "source_record_key": key,
            "source_abnormal_flag": "H"}


@pytest.fixture
def case():
    return {"documents_all": [
        note("2030-01-03 10:00 首次病程记录\n病史：患者既往腹痛。\n查体：腹部柔软。\n诊疗计划：排除手术禁忌后限期行手术治疗。"),
        note("2030-01-04 12:06 主诊首次查房\n拟行腹腔镜探查备胰体癌根治术。", "主诊首次查房记录"),
        note("2030-01-05 13:00 术后记录\n术中发现转移，未切除。", "术后首次病程记录"),
    ], "imaging_all": [image("2030-01-01 08:00:00"),
                       image("2030-01-02 09:00:00", "胰腺MR平扫+增强", "fictional-mr", "整份MR报告\n第二行原文。")],
            "laboratory_all": [lab("2030-01-01 09:00:00")], "pathology_all": [],
            "outcome_post_T0_isolated": {"DO_NOT_READ": "future-outcome"}}


def test_raw_context_independence_and_decision_isolation(case):
    result, audit, _ = extract_case(case, "S001")
    ps = result["packets"]
    assert all(p["messages"] and len(p["messages"]) == 2 for p in ps)
    assert ps[2]["context"] == ps[3]["context"]
    assert ps[2]["messages"] != ps[3]["messages"]
    for p in ps:
        assert "根治术" not in p["context"]
        assert "术中发现" not in p["context"]
        assert "future-outcome" not in str(p)
    assert case["imaging_all"][1]["report_deidentified"] in ps[1]["context"]
    assert "病史：患者既往腹痛。" in ps[2]["context"]
    assert audit["episode"]["cutoff"] == "2030-01-04 12:06:00"


def test_report_time_not_sample_time_and_equal_minute_excluded(case):
    case["laboratory_all"] += [lab("2030-01-04 12:05:59", "before"), lab("2030-01-04 12:06:00", "equal"),
                               lab("2030-01-05 09:00:00", "future"), lab(None, "missing")]
    result, audit, _ = extract_case(case, "S001")
    assert result["packets"][2]["counts"]["laboratory"] == 2
    excluded = {d["source_id"]: d["reason"] for d in audit["source_decisions"] if d["stage"] == 3}
    assert excluded["LAB0003"] == "NOT_FULLY_BEFORE_BOUNDARY"
    assert excluded["LAB0005"] == "RESULT_TIME_MISSING_OR_INVALID"


def test_create_time_never_substitutes_for_completion(case):
    case["documents_all"][1]["document_text"] = "主诊首次查房：拟行胰体癌根治术。"
    result, audit, _ = extract_case(case, "S001")
    assert not audit["episode"]
    assert all(p["status"] == "UNRESOLVED" and p["messages"] is None for p in result["packets"])
    assert document_time("记录时间：2030-01-04 10:00\n原文")[0].precision == "minute"
    assert document_time("2030-01-04 10:00\n记录时间：2030-01-05 10:00")[1] == "DOCUMENT_TIME_AMBIGUOUS"


def test_first_enhanced_can_overlap_and_missing_enhanced_not_invented(case):
    case["imaging_all"] = [case["imaging_all"][1]]
    result, _, _ = extract_case(case, "S001")
    assert result["packets"][0]["context"] == result["packets"][1]["context"]
    assert "DISCOVERY_STAGING_ROUNDS_OVERLAP" in result["packets"][0]["issues"]
    case["imaging_all"] = [image("2030-01-01 08:00:00")]
    result, _, _ = extract_case(case, "S001")
    assert result["packets"][1]["messages"] is None
    assert len(result["packets"]) == 4


def test_plan_before_imaging_does_not_move_later_scans_back(case):
    case["imaging_all"] = [image("2030-01-06 08:00:00", "胰腺CT平扫+增强")]
    result, _, _ = extract_case(case, "S001")
    assert result["packets"][0]["status"] == "UNRESOLVED"
    assert result["packets"][1]["status"] == "UNRESOLVED"
    assert not result["packets"][2]["counts"].get("imaging")


def test_future_backfill_and_postop_path_are_isolated(case):
    case["documents_all"].append(note("2030-01-03 11:00 入院记录\n病史：原文。\n辅助检查：2030-01-06 MRI显示后来结果。", "入院记录"))
    case["pathology_all"] = [{"report_date": "2030-01-07 00:00:00", "specimen_type": "小标本", "pathology_diagnosis": "未来病理"}]
    result, _, _ = extract_case(case, "S001")
    assert "后来结果" not in result["packets"][2]["context"]
    assert "未来病理" not in result["packets"][2]["context"]
    assert "NOTE_MENTIONS_LATER_DATE" in result["packets"][2]["issues"]
    assert future_dates("2030-01-04 14:00 检查", datetime(2030, 1, 4, 12, 6))


def test_prior_returned_diagnostic_pathology_date_is_conservative(case):
    case["pathology_all"] = [{"report_date": "2030-01-02", "specimen_type": "细针穿刺", "pathology_diagnosis": "诊断性病理原文"},
                             {"report_date": "2030-01-04 00:00:00", "specimen_type": "小标本", "pathology_diagnosis": "同日钟点未知"}]
    result, _, _ = extract_case(case, "S001")
    assert result["packets"][2]["counts"]["pathology"] == 1
    assert "同日钟点未知" not in result["packets"][2]["context"]


def test_cross_admission_and_long_followup_excluded(case):
    other = note("2030-01-03 11:00 首次病程\n其他住院资料不应混入此次病程。", admission="2030-01-02 08:00:00", visit="2")
    case["documents_all"].append(other)
    case["imaging_all"].append(image("2031-01-01 08:00:00", "胰腺CT平扫+增强", "followup"))
    result, audit, _ = extract_case(case, "S001")
    assert "其他住院资料" not in result["packets"][2]["context"]
    assert result["packets"][2]["counts"]["imaging"] == 2
    assert "LOOKBACK_OVERLAPS_OTHER_ADMISSION" in audit["global_issues"]


def test_exact_source_duplicates_merge_but_conflicts_survive(case):
    case["imaging_all"] += [deepcopy(case["imaging_all"][0]), {**case["imaging_all"][0], "report_deidentified": "不同内容，不能静默择一。"}]
    result, audit, _ = extract_case(case, "S001")
    assert len(audit["duplicates"]) == 1
    assert result["packets"][0]["counts"]["imaging"] == 2
    assert "不同内容，不能静默择一。" in result["packets"][0]["context"]
    assert "CONFLICTING_PAYLOADS_FOR_SAME_SOURCE" in audit["global_issues"]


def test_preop_summary_planned_operation_date_is_not_an_operative_record(case):
    case["documents_all"][1] = note("2030-01-04 12:06 术前小结\n拟施手术名称和手术方式：胰体癌根治术。手术时间：2030年01月05日", "术前小结")
    _, audit, _ = extract_case(case, "S001")
    assert audit["episode"]["selected_plan_source_id"] == "DOC0002"


def test_identical_images_with_different_ids_render_once_with_all_sources(case):
    case["imaging_all"].append({**case["imaging_all"][0], "index_report_uid": "different", "source_record_key": "different"})
    result, audit, _ = extract_case(case, "S001")
    assert result["packets"][0]["counts"]["imaging"] == 1
    assert len(audit["source_inventory"][3]["source_refs"]) == 2


def test_history_backfill_requires_bound_span_and_reason(case):
    original = "2030-01-03 10:00 首次病程\n2、患者于2029-12-28外院就诊，报告提示病变。\n3、查体：后来查体。\n诊疗计划：手术治疗。"
    case["documents_all"][0]["document_text"] = original
    result, _, template = extract_case(case, "S001")
    assert "外院就诊" not in result["packets"][0]["context"]
    review = deepcopy(template)
    review["history_spans"][0].update(accepted=True, reviewer="source-review", reason="Verified historical paragraph only")
    result, _, _ = extract_case(case, "S001", review)
    assert "外院就诊" in result["packets"][0]["context"]
    assert "后来查体" not in result["packets"][0]["context"]
    review["source_bundle_sha256"] = "wrong"
    with pytest.raises(ValueError, match="hash"):
        extract_case(case, "S001", review)


def test_missing_report_does_not_count_as_resolved_stage(case):
    case["imaging_all"][0]["report_deidentified"] = ""
    result, _, _ = extract_case(case, "S001")
    assert result["packets"][0]["status"] == "UNRESOLVED"


def test_configurable_episode_not_selected_from_outcome(case):
    case["documents_all"].append(note("2030-02-04 12:00 术前小结\n拟施手术名称：胰体尾切除术", "术前小结", "2030-02-03 09:00:00", "2"))
    _, audit, template = extract_case(case, "S001")
    assert "MULTIPLE_PLANNED_ADMISSIONS" in audit["global_issues"]
    template["plan_source_id"] = "DOC0004"
    _, audit, _ = extract_case(case, "S001", template)
    assert audit["episode"]["cutoff"] == "2030-02-04 12:00:00"


def test_output_stale_messages_removed_and_private_cli_boundary(case, tmp_path):
    result, audit, template = extract_case(case, "S001")
    write_case(tmp_path, result, audit, template)
    assert (tmp_path / "S001/02_messages.json").exists()
    case["imaging_all"] = [case["imaging_all"][0]]
    result, audit, template = extract_case(case, "S001")
    write_case(tmp_path, result, audit, template)
    assert not (tmp_path / "S001/02_messages.json").exists()
    with pytest.raises(ValueError, match="private"):
        protected_output(tmp_path)
