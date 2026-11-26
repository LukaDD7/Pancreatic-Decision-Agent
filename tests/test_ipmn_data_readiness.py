import csv
import gzip
import io
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.ipmn_data_readiness.audit import patient_audit
from scripts.ipmn_data_readiness.candidate import canonical_sha256, compute_candidate_values, engine_sha256, validate_candidate_manifest
from scripts.ipmn_data_readiness.handoff import REVIEW_REQUIREMENTS, build_readiness_handoff
from scripts.ipmn_data_readiness.measurement import validate_measurement_rows
from scripts.ipmn_data_readiness.rules import cyst_size_mm, lab_kind, nodule_status, pathology_view


def l1_row(research_id="SYNTH-01"):
    return {"research_id": research_id, "evidence_grade": "A3"}


def l2_row():
    return {"entry_date": "2020-02-01", "baseline_subtype": "BD_EXPLICIT", "L2_route": "L2监测候选索引"}


def image(day, size=20, method="上腹CT增强"):
    return {
        "exam_date": day,
        "exam_method": method,
        "report_deidentified": f"胰腺分支型IPMN，囊肿约{size}mm，主胰管约2mm，未见壁结节。",
    }


def test_landmark_uses_365_days_and_repeat_measurements():
    patient = {
        "imaging_all": [image("2020-02-01"), image("2020-12-01", 18)],
        "pathology_all": [],
        "documents_all": [{"文书名称": "门诊记录", "create_time": "2021-03-01 08:00:00", "document_text": "随访"}],
        "laboratory_all": [],
    }
    result = patient_audit(patient, l1_row(), l2_row())
    assert result["landmark_date"] == "2021-01-31"
    assert result["three_year_end"] == "2024-01-31"
    assert result["repeat_same_modality_2"] is True
    assert result["repeat_numeric_size_same_modality"] is True
    assert result["landmark_eligibility_rule_proxy"] is True


def test_postlandmark_pathology_is_candidate_not_confirmed_event():
    patient = {
        "imaging_all": [image("2020-02-01"), image("2020-12-01")],
        "pathology_all": [{
            "取材日期": "2022-01-01",
            "报告日期": "2022-01-05",
            "标本名称": "胰体尾切除标本",
            "病理诊断": "分支型IPMN伴高级别异型增生，未见浸润癌。",
        }],
        "documents_all": [{"文书名称": "门诊记录", "create_time": "2021-03-01", "document_text": "随访"}],
        "laboratory_all": [],
    }
    result = patient_audit(patient, l1_row(), l2_row())
    event = result["postlandmark_3y_target_pathology_candidates"][0]
    assert event["date"] == "2022-01-01"
    assert event["malignancy"] == "IPMN_HGD"
    assert result["target_event_counts_by_period"]["post_landmark_3y"] == 1
    assert "POSTLANDMARK_CANCER_PATHOLOGY_ATTRIBUTION" in result["manual_review_reasons"]


def test_negated_high_grade_is_not_positive_pathology():
    result = pathology_view({"标本名称": "胰腺", "病理诊断": "IPMN伴低级别异型增生，未见高级别异型增生，未见浸润癌。"})
    assert result["malignancy"] == ""


def test_unmentioned_nodule_is_not_negative():
    assert nodule_status("胰腺囊肿约20mm。") == "not_mentioned"
    assert nodule_status("胰腺囊肿，未见壁结节。") == "explicit_absent"


def test_prelandmark_resection_prevents_proxy_eligibility():
    patient = {
        "imaging_all": [image("2020-02-01"), image("2020-12-01")],
        "pathology_all": [{"取材日期": "2020-06-01", "标本名称": "胰体尾切除标本", "病理诊断": "IPMN伴低级别异型增生。"}],
        "documents_all": [{"文书名称": "门诊记录", "create_time": "2021-03-01", "document_text": "术后随访"}],
        "laboratory_all": [],
    }
    result = patient_audit(patient, l1_row(), l2_row())
    assert result["prelandmark_resection_proxy"] is True
    assert result["landmark_eligibility_rule_proxy"] is False


def test_lab_synonyms_and_unrelated_items():
    assert lab_kind("糖类抗原19-9") == "ca199"
    assert lab_kind("CA19-9") == "ca199"
    assert lab_kind("糖化血红蛋白(HbA1c)") == "hba1c"
    assert lab_kind("总胆红素") == "total_bilirubin"
    assert lab_kind("白蛋白") == ""


def test_followup_cyst_measurement_does_not_require_repeated_ipmn_wording():
    patient = {
        "imaging_all": [
            image("2020-02-01"),
            {"exam_date": "2020-12-01", "exam_method": "上腹CT增强", "report_deidentified": "胰体囊肿约18mm，主胰管约2mm。"},
        ],
        "pathology_all": [],
        "documents_all": [{"文书名称": "门诊记录", "create_time": "2021-03-01", "document_text": "随访"}],
        "laboratory_all": [],
    }
    result = patient_audit(patient, l1_row(), l2_row())
    assert result["repeat_numeric_size_same_modality"] is True
    assert result["repeat_numeric_mpd_same_modality"] is True


def test_lgd_is_separate_from_target_and_non_target_pathology():
    result = pathology_view({"标本名称": "胰体尾切除标本", "病理诊断": "IPMN伴低级别异型增生。"})
    assert result["malignancy"] == ""
    assert result["ipmn_lgd_candidate"] is True
    assert result["non_target_pancreas_pathology_candidate"] is True


def test_ipmn_audit_parse_001_solid_mass_is_not_cyst_size():
    text = "胰腺钩突见不规则软组织肿块影，范围约31*46mm；胰腺见多发囊状低密度灶。"
    assert cyst_size_mm(text) is None


def test_ipmn_audit_parse_002_units_on_each_dimension():
    assert cyst_size_mm("胰头颈部小囊状异常信号灶，大小约2.8mm*3.5mm。") == 3.5
    assert cyst_size_mm("胰头囊性灶，大小约20.2×14.7mm。") == 20.2


def test_repeat_mpd_explicit_normal_status_is_longitudinal_coverage():
    patient = {
        "imaging_all": [image("2020-02-01"), image("2020-12-01")],
        "pathology_all": [],
        "documents_all": [{"文书名称": "门诊记录", "create_time": "2021-03-01", "document_text": "随访"}],
        "laboratory_all": [],
    }
    result = patient_audit(patient, l1_row(), l2_row())
    assert result["repeat_mpd_status_same_modality"] is True


def handoff_summary():
    return {
        "audit_version": "ipmn_data_readiness_v0",
        "created_at": "2026-11-13T17:05:34+08:00",
        "code_sha256": {"audit.py": "abc"},
        "source_sha256": {"L1主队列.zip": "def"},
        "blinding": "aggregate readiness only",
        "table_A": [
            {"stage": "N4_observed_eligibility_proxy", "count": 926},
            {"stage": "N5_repeat_same_modality", "count": 419},
            {"stage": "N5_repeat_numeric_cyst_size", "count": 109},
            {"stage": "N6_minimal_outcome_trace", "count": 419},
            {"stage": "E_postlandmark_3y_target_pathology_candidates", "count": 2},
            {"stage": "E_in_repeat_numeric_cyst_size_subset", "count": 0},
        ],
    }


def frozen_candidate_manifest(summary):
    return {
        "manifest_version": "ipmn-candidate-manifest-v0.1",
        "protocol_version": "ipmn-main-v1.1",
        "status": "frozen",
        "readiness_summary_sha256": canonical_sha256(summary),
        "primary_hypothesis_id": "SYNTH-H1",
        "candidates": [{
            "hypothesis_id": "SYNTH-H1",
            "scientific_rationale": "合成候选，仅验证冻结接口",
            "object": "index cyst",
            "spatial_region": "胰腺囊肿",
            "time_window": {"start_relative_to_entry_days": 0, "end_relative_to_entry_days": 365},
            "formula": "signed_size_change / observation_days",
            "unit": "mm/day",
            "minimum_observations": 2,
            "measurement_version": "synthetic-measurement-v1",
            "code_sha256": engine_sha256(),
            "source_fields": ["cyst_size_mm", "exam_date"],
            "missing_policy": "不可计算保留原因，不填补为正常",
            "track": "primary",
            "known_status": "extension",
            "selection_status": "selected"
        }],
        "selection": {
            "confirmation_outcomes_accessed": False,
            "development_patient_set_sha256": "b" * 64,
            "selection_patient_set_sha256": "c" * 64,
            "candidate_budget": 1,
            "search_call_budget": 1,
            "selection_rule": "合成确定性选择",
            "tie_break_rule": "hypothesis_id",
            "frozen_at": "2026-11-24T00:00:00+08:00",
            "frozen_by_role": "synthetic_blinded_reviewer"
        },
        "analysis_plan": {
            "m1_version": "synthetic-m1-v1",
            "m1_code_sha256": "d" * 64,
            "primary_test": "M2_VS_M1_CAUSE_SPECIFIC_COX_LRT",
            "alpha": 0.05,
            "landmark_days": 365,
            "confirmation_window_days": 1095
        }
    }


def completed_review(status="passed"):
    return {
        "status": status,
        "reviewer_role": "independent_test_role",
        "reviewed_at": "2026-11-23",
        "evidence_refs": ["synthetic-review:1"],
    }


def test_handoff_counts_cannot_release_clinical_confirmation():
    result = build_readiness_handoff(handoff_summary())
    assert result["decision"] == "READINESS_REVIEW_REQUIRED"
    assert result["clinical_confirmation_permitted"] is False
    assert len(result["next_actions"]) == len(REVIEW_REQUIREMENTS)
    assert result["observed_counts"]["N5_repeat_numeric_cyst_size"] == 109


def test_handoff_rejects_unknown_review_requirement():
    with pytest.raises(ValueError, match="Unknown review requirements"):
        build_readiness_handoff(handoff_summary(), {"source_channel": completed_review()})


def test_handoff_rejects_bare_pass_without_traceable_evidence():
    with pytest.raises(ValueError, match="evidence_refs"):
        build_readiness_handoff(handoff_summary(), {"source_channels": {"status": "passed"}})


def test_handoff_failed_review_requires_repair():
    result = build_readiness_handoff(handoff_summary(), {"source_channels": completed_review("failed")})
    assert result["decision"] == "REPAIR_REQUIRED"
    assert result["clinical_confirmation_permitted"] is False
    assert result["next_actions"][0]["review"] == "source_channels"


def test_handoff_complete_traceable_reviews_release_confirmation():
    summary = handoff_summary()
    manifest = frozen_candidate_manifest(summary)
    reviews = {name: completed_review() for name in REVIEW_REQUIREMENTS}
    reviews["candidate_freeze"]["artifact_sha256"] = canonical_sha256(manifest)
    result = build_readiness_handoff(summary, reviews, manifest)
    assert result["decision"] == "READY_FOR_CLINICAL_CONFIRMATION"
    assert result["clinical_confirmation_permitted"] is True
    assert result["candidate_freeze_artifact"]["primary_hypothesis_id"] == "SYNTH-H1"
    assert result["next_actions"] == []


def test_candidate_manifest_rejects_outcome_source_fields():
    manifest = frozen_candidate_manifest(handoff_summary())
    manifest["candidates"][0]["source_fields"] = ["postlandmark_3y_target_pathology_candidates"]
    with pytest.raises(ValueError, match="prohibited"):
        validate_candidate_manifest(manifest)


def csv_bytes(rows):
    output = io.StringIO()
    fields = list(dict.fromkeys(key for row in rows for key in row))
    writer = csv.DictWriter(output, fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def synthetic_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    l1 = l1_row()
    l2 = l2_row() | {"research_id": "SYNTH-01"}
    extra_l2 = {"research_id": "SYNTH-L2-ONLY", "entry_date": "2020-01-01", "baseline_subtype": "MD_EXPLICIT", "L2_route": "其他"}
    with ZipFile(source / "索引与说明.zip", "w") as archive:
        archive.writestr("nested/L2全部索引.csv", csv_bytes([l2, extra_l2]))
    patient = {
        "imaging_all": [image("2020-02-01"), image("2020-12-01", 18)],
        "pathology_all": [],
        "documents_all": [{"文书名称": "门诊记录", "create_time": "2021-03-01", "document_text": "随访"}],
        "laboratory_all": [],
    }
    with ZipFile(source / "L1主队列.zip", "w") as archive:
        archive.writestr("nested/队列索引.csv", csv_bytes([l1]))
        archive.writestr("patients/SYNTH-01.json.gz", gzip.compress(json.dumps(patient).encode("utf-8")))
    (source / "纳入规则与使用说明.md").write_text("合成来源，仅用于接口回归。\n", encoding="utf-8")
    return source


def run_synthetic_audit(source, output, review_file=None):
    command = [
        sys.executable,
        "-m",
        "scripts.ipmn_data_readiness.audit",
        "--source-root",
        str(source),
        "--output-dir",
        str(output),
        "--as-of",
        "2026-11-24",
    ]
    if review_file:
        command.extend(["--review-file", str(review_file)])
    return subprocess.run(command, cwd=Path(__file__).parents[1], capture_output=True, text=True, check=True)


def run_synthetic_release(summary, reviews, manifest, output):
    return subprocess.run([
        sys.executable,
        "-m",
        "scripts.ipmn_data_readiness.release",
        "--summary",
        str(summary),
        "--review-file",
        str(reviews),
        "--candidate-manifest",
        str(manifest),
        "--output",
        str(output),
    ], cwd=Path(__file__).parents[1], capture_output=True, text=True, check=True)


def test_ipmn_audit_cli_synthetic_source_and_review_handoff(tmp_path):
    source = synthetic_source(tmp_path)
    first_output = tmp_path / "audit-without-reviews"
    run_synthetic_audit(source, first_output)
    summary = json.loads((first_output / "summary.json").read_text(encoding="utf-8"))
    counts = {row["stage"]: row["count"] for row in summary["table_A"]}
    assert summary["source_patients"] == 1
    assert counts["N0_source_recall"] == 2
    assert counts["N1_L1_prepath_diagnosis"] == 1
    assert counts["N4_observed_eligibility_proxy"] == 1
    assert counts["N5_repeat_same_modality"] == 1
    handoff = json.loads((first_output / "readiness_handoff.json").read_text(encoding="utf-8"))
    assert handoff["decision"] == "READINESS_REVIEW_REQUIRED"
    assert handoff["clinical_confirmation_permitted"] is False
    assert "handoff.py" in handoff["source_audit"]["code_sha256"]

    reviews = {name: completed_review() for name in REVIEW_REQUIREMENTS}
    reviews["candidate_freeze"] = {"status": "pending", "note": "等待冻结manifest"}
    review_file = tmp_path / "reviews.json"
    review_file.write_text(json.dumps(reviews), encoding="utf-8")
    second_output = tmp_path / "audit-with-reviews"
    run_synthetic_audit(source, second_output, review_file)
    pending = json.loads((second_output / "readiness_handoff.json").read_text(encoding="utf-8"))
    assert pending["decision"] == "READINESS_REVIEW_REQUIRED"
    assert len(pending["next_actions"]) == 1

    second_summary = json.loads((second_output / "summary.json").read_text(encoding="utf-8"))
    manifest = frozen_candidate_manifest(second_summary)
    reviews["candidate_freeze"] = completed_review() | {"artifact_sha256": canonical_sha256(manifest)}
    review_file.write_text(json.dumps(reviews), encoding="utf-8")
    manifest_file = tmp_path / "candidate-manifest.json"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    handoff_file = tmp_path / "released-handoff.json"
    run_synthetic_release(second_output / "summary.json", review_file, manifest_file, handoff_file)
    released = json.loads(handoff_file.read_text(encoding="utf-8"))
    assert released["decision"] == "READY_FOR_CLINICAL_CONFIRMATION"
    assert released["clinical_confirmation_permitted"] is True
    assert released["candidate_freeze_artifact"]["manifest_sha256"] == canonical_sha256(manifest)
    assert released["next_actions"] == []

    manifest["candidates"][0]["formula"] = "tampered_formula"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero exit status 1"):
        run_synthetic_release(second_output / "summary.json", review_file, manifest_file, tmp_path / "tampered-handoff.json")
    assert not (tmp_path / "tampered-handoff.json").exists()


def measurement_row(measurement_id, exam_date, value, **overrides):
    row = {
        "research_id": "SYNTH-01",
        "measurement_id": measurement_id,
        "entry_date": "2020-02-01",
        "object_id": "INDEX-CYST-1",
        "exam_date": exam_date,
        "available_date": exam_date,
        "availability_basis": "reported_available_time",
        "modality": "MRI",
        "protocol_id": "MR-PROTOCOL-1",
        "variable": "cyst_size_mm",
        "value": value,
        "unit": "mm",
        "source_ref": f"synthetic:{measurement_id}",
        "measurement_version": "synthetic-measurement-v1",
        "review": {
            "identity_status": "passed",
            "protocol_status": "passed",
            "reviewer_role": "synthetic_measurement_reviewer",
            "reviewed_at": "2026-11-24",
            "evidence_refs": ["synthetic-measurement-review:1"]
        }
    }
    row.update(overrides)
    return row


def computation_inputs():
    manifest = frozen_candidate_manifest(handoff_summary())
    rows = [measurement_row("M1", "2020-02-01", 20), measurement_row("M2", "2020-12-01", 18)]
    return manifest, rows


def test_candidate_computation_preserves_signed_shrinkage_and_missing_patients():
    manifest, rows = computation_inputs()
    results = compute_candidate_values(manifest, rows, ["SYNTH-01", "SYNTH-NO-DATA"])
    assert results[0]["status"] == "computable"
    assert results[0]["value"] == -2 / 304
    assert results[0]["signed_change_mm"] == -2
    assert results[0]["observation_days"] == 304
    assert results[1]["status"] == "not_computable"
    assert results[1]["reason"] == "NO_VISIBLE_MEASUREMENT"


def test_candidate_computation_excludes_future_available_measurement():
    manifest, rows = computation_inputs()
    rows[1]["available_date"] = "2021-02-01"
    result = compute_candidate_values(manifest, rows)[0]
    assert result["status"] == "not_computable"
    assert result["reason"] == "INSUFFICIENT_NUMERIC_OBSERVATIONS"


def test_candidate_computation_rejects_cross_object_series():
    manifest, rows = computation_inputs()
    rows[1]["object_id"] = "INDEX-CYST-2"
    result = compute_candidate_values(manifest, rows)[0]
    assert result["reason"] == "AMBIGUOUS_MEASUREMENT_OBJECT"


def test_candidate_computation_rejects_cross_modality_series():
    manifest, rows = computation_inputs()
    rows[1]["modality"] = "CT"
    result = compute_candidate_values(manifest, rows)[0]
    assert result["reason"] == "CROSS_MODALITY_MEASUREMENT"


def test_candidate_computation_requires_reviewed_measurement_links():
    manifest, rows = computation_inputs()
    rows[1]["review"]["identity_status"] = "pending"
    result = compute_candidate_values(manifest, rows)[0]
    assert result["reason"] == "MEASUREMENT_REVIEW_REQUIRED"


def test_candidate_computation_does_not_impute_missing_values():
    manifest, rows = computation_inputs()
    rows[1]["value"] = None
    result = compute_candidate_values(manifest, rows)[0]
    assert result["reason"] == "INSUFFICIENT_NUMERIC_OBSERVATIONS"
    assert result["value"] is None


def test_candidate_computation_binds_builtin_engine_hash():
    manifest, rows = computation_inputs()
    manifest["candidates"][0]["code_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="engine hash"):
        compute_candidate_values(manifest, rows)


def test_candidate_computation_supports_signed_mpd_change():
    manifest, rows = computation_inputs()
    manifest["candidates"][0]["formula"] = "signed_mpd_change / observation_days"
    manifest["candidates"][0]["source_fields"] = ["mpd_mm", "exam_date"]
    for row, value in zip(rows, (2, 4)):
        row["variable"] = "mpd_mm"
        row["object_id"] = "MAIN-PANCREATIC-DUCT"
        row["value"] = value
    result = compute_candidate_values(manifest, rows)[0]
    assert result["status"] == "computable"
    assert result["value"] == 2 / 304


def test_measurement_rows_reject_outcome_injection():
    _, rows = computation_inputs()
    rows[0]["postlandmark_target_event"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        validate_measurement_rows(rows)


@pytest.mark.parametrize("overrides,reason", [
    ({"protocol_id": "MR-PROTOCOL-2"}, "INCOMPARABLE_PROTOCOL"),
    ({"measurement_version": "synthetic-measurement-v2"}, "AMBIGUOUS_MEASUREMENT_VERSION"),
])
def test_candidate_computation_rejects_inconsistent_measurement_versions(overrides, reason):
    manifest, rows = computation_inputs()
    rows[1].update(overrides)
    result = compute_candidate_values(manifest, rows)[0]
    assert result["reason"] == reason


def test_candidate_computation_rejects_conflicting_same_day_values():
    manifest, rows = computation_inputs()
    rows.append(measurement_row("M3", "2020-12-01", 19))
    result = compute_candidate_values(manifest, rows)[0]
    assert result["reason"] == "CONFLICTING_SAME_DAY_MEASUREMENT"


def test_candidate_computation_rejects_rows_outside_patient_set():
    manifest, rows = computation_inputs()
    with pytest.raises(ValueError, match="outside the requested patient set"):
        compute_candidate_values(manifest, rows, ["SYNTH-NO-DATA"])


def test_candidate_computation_rejects_unsupported_formula():
    manifest, rows = computation_inputs()
    manifest["candidates"][0]["formula"] = "unreviewed_dynamic_expression"
    with pytest.raises(ValueError, match="Unsupported frozen formula"):
        compute_candidate_values(manifest, rows)
