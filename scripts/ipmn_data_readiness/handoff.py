from pathlib import Path

from .candidate import SCHEMA_PATH, canonical_sha256, file_sha256, validate_candidate_manifest

REVIEW_REQUIREMENTS = {
    "source_channels": "核对原始报告、临床文书、检验和 analysis_only 字段的来源、时间、单位与可采用范围",
    "clinical_population": "由临床角色审定 BD-IPMN、entry、landmark 风险集及开发/确认患者隔离",
    "measurement": "闭合 index cyst 跨时点身份、协议可比性、测量误差和冻结 M1 组件",
    "outcomes": "由独立角色审定目标事件、cPDAC、切除、死亡、失访及相应日期",
    "analysis": "由隔离统计角色确认共同分析集合、事件信息、M1/M2 自由度和可估计性",
    "candidate_freeze": "在结局盲态下冻结唯一主表型、计算版本、候选清单和分析 manifest",
}

OBSERVED_STAGES = (
    "N4_observed_eligibility_proxy",
    "N5_repeat_same_modality",
    "N5_repeat_numeric_cyst_size",
    "N6_minimal_outcome_trace",
    "E_postlandmark_3y_target_pathology_candidates",
    "E_in_repeat_numeric_cyst_size_subset",
)

REVIEW_FIELDS = {"status", "evidence_refs", "reviewer_role", "reviewed_at", "note", "artifact_sha256"}


def _evidence_refs(review, name):
    refs = review.get("evidence_refs", [])
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise ValueError(f"{name}.evidence_refs must be a list of non-empty strings")
    return refs


def _normalize_review(name, review):
    if review is None:
        return {"status": "missing", "evidence_refs": []}
    if not isinstance(review, dict):
        raise ValueError(f"{name} must be an object")
    unknown = set(review) - REVIEW_FIELDS
    if unknown:
        raise ValueError(f"{name} has unknown fields: {sorted(unknown)}")
    status = review.get("status")
    if status not in {"pending", "passed", "failed"}:
        raise ValueError(f"{name}.status must be pending, passed, or failed")
    if "artifact_sha256" in review and name != "candidate_freeze":
        raise ValueError(f"{name} does not accept artifact_sha256")
    refs = _evidence_refs(review, name)
    if status in {"passed", "failed"}:
        missing = [field for field in ("reviewer_role", "reviewed_at") if not isinstance(review.get(field), str) or not review[field].strip()]
        if not refs:
            missing.append("evidence_refs")
        if name == "candidate_freeze" and status == "passed" and not review.get("artifact_sha256"):
            missing.append("artifact_sha256")
        if missing:
            raise ValueError(f"{name} {status} review lacks {', '.join(missing)}")
    normalized = {"status": status, "evidence_refs": refs}
    for field in ("reviewer_role", "reviewed_at", "note", "artifact_sha256"):
        if field in review:
            normalized[field] = review[field]
    return normalized


def build_readiness_handoff(summary, reviews=None, candidate_manifest=None):
    reviews = {} if reviews is None else reviews
    if not isinstance(reviews, dict):
        raise ValueError("reviews must be an object keyed by review requirement")
    unknown = set(reviews) - set(REVIEW_REQUIREMENTS)
    if unknown:
        raise ValueError(f"Unknown review requirements: {sorted(unknown)}")
    normalized = {name: _normalize_review(name, reviews.get(name)) for name in REVIEW_REQUIREMENTS}
    summary_sha256 = canonical_sha256(summary)
    candidate_artifact = None
    candidate_status = normalized["candidate_freeze"]["status"]
    if candidate_manifest is not None or candidate_status == "passed":
        if candidate_status != "passed":
            raise ValueError("A candidate manifest can only be attached to a passed candidate_freeze review")
        if candidate_manifest is None:
            raise ValueError("Passed candidate_freeze review requires the candidate manifest")
        validate_candidate_manifest(candidate_manifest, expected_summary_sha256=summary_sha256)
        manifest_sha256 = canonical_sha256(candidate_manifest)
        if normalized["candidate_freeze"]["artifact_sha256"] != manifest_sha256:
            raise ValueError("candidate_freeze artifact_sha256 does not match the candidate manifest")
        candidate_artifact = {
            "manifest_sha256": manifest_sha256,
            "readiness_summary_sha256": summary_sha256,
            "primary_hypothesis_id": candidate_manifest["primary_hypothesis_id"],
        }
    failed = any(review["status"] == "failed" for review in normalized.values())
    all_passed = all(review["status"] == "passed" for review in normalized.values())
    if failed:
        decision = "REPAIR_REQUIRED"
    elif all_passed:
        decision = "READY_FOR_CLINICAL_CONFIRMATION"
    else:
        decision = "READINESS_REVIEW_REQUIRED"
    counts = {row["stage"]: row["count"] for row in summary.get("table_A", [])}
    return {
        "interface_version": "ipmn_readiness_handoff_v0.1",
        "handoff_code_sha256": {
            "handoff.py": file_sha256(Path(__file__)),
            "candidate.py": file_sha256(Path(__file__).with_name("candidate.py")),
            "measurement.py": file_sha256(Path(__file__).with_name("measurement.py")),
            "release.py": file_sha256(Path(__file__).with_name("release.py")),
            SCHEMA_PATH.name: file_sha256(SCHEMA_PATH),
        },
        "decision": decision,
        "clinical_confirmation_permitted": all_passed and not failed,
        "source_audit": {
            "audit_version": summary.get("audit_version", ""),
            "created_at": summary.get("created_at", ""),
            "summary_sha256": summary_sha256,
            "code_sha256": summary.get("code_sha256", {}),
            "source_sha256": summary.get("source_sha256", {}),
        },
        "candidate_freeze_artifact": candidate_artifact,
        "observed_counts": {stage: counts.get(stage) for stage in OBSERVED_STAGES},
        "observed_counts_status": "rule_candidates_and_coverage_only_not_clinical_reference",
        "blinding": summary.get("blinding", ""),
        "reviews": normalized,
        "next_actions": [
            {"review": name, "status": review["status"], "action": REVIEW_REQUIREMENTS[name]}
            for name, review in normalized.items()
            if review["status"] != "passed"
        ],
    }
