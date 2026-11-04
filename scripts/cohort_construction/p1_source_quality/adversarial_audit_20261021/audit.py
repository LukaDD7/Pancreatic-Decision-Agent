"""Audit frozen P1 v0.2 without rerunning screening or changing membership.

Synthetic witnesses reproduce rule defects; they do not estimate clinical error
rates. Real-data output contains aggregate counts only, never identifiers/text.
Each report is counted once per signal; signals may overlap. Patient counts mean
at least one such report, not patients who would change eligibility.
"""
from collections import Counter, defaultdict
from pathlib import Path
import argparse
import hashlib
import json
import sys


def read_jsonl(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    release = repo / "scripts/cohort_construction/p1_source_quality/p1_quality_v0.2"
    results = repo / "local_artifacts/p1_quality_v0.2/results"
    # Frozen module names are required by run_quality's internal import.
    sys.path.insert(0, str(release))
    import report_parser as rp
    from run_quality import classify_patient

    rules = json.loads((release / "quality_rules.json").read_text())
    base = (
        "肿块评估：胰头可见肿块，大小约15×18mm。\n"
        "动脉评估：肠系膜上动脉无接触。腹腔干无接触。肝总动脉无接触。\n"
        "静脉评估：门静脉无接触。肠系膜上静脉无接触。\n"
        "其它评估：肝脏病变：无。腹膜或网膜结节：无。"
    )

    def assess(text=base, impression="远处转移：未见。", verified=True):
        record = {"patient_uid": "SYNTHETIC", "exam_datetime": "2024-01-01",
                  "exam_method": "胰腺CT增强", "original_findings": text,
                  "original_impression": impression}
        parsed = rp.parse_report(record, rules)
        state, reasons = rp.evaluate_report(record, parsed,
                                           {"verified": verified, "reason": "SYNTHETIC_UNVERIFIED"}, rules)
        return {"parsed": parsed, "state": state, "reasons": reasons}

    witnesses = []

    def witness(name, text, observed, concern, reproduced):
        witnesses.append({"id": name, "synthetic_input": text, "observed": observed,
                          "concern": concern, "reproduced": bool(reproduced)})

    p = assess()
    assert p["state"] == "PASS", "Positive control must pass frozen code."
    partial = base.replace("腹腔干无接触。肝总动脉无接触。", "")
    p = assess(partial)
    witness("W01_PARTIAL_A_BLOCKED", partial, p["reasons"],
            "有实质解剖信息，仍因两条血管未述而阻断报告自动通过；是设计不匹配。",
            p["state"] == "REVIEW" and "VESSEL_CA_MISSING" in p["reasons"])

    text = "未见肠系膜上动脉受侵。"
    v = rp.resolve_vessel(rp.vessel_evidence(text, rules["required_vessels"], "synthetic")["SMA"])
    witness("W02_PREFIX_NEGATION_LOST", text, v["state"],
            "否定在血管名称之前，被截掉，错误识别为受侵。", v["state"] == "CONTACT_OR_INVASION")

    text = "肠系膜上动脉近段无接触，远段局部包绕。"
    v = rp.resolve_vessel(rp.vessel_evidence(text, rules["required_vessels"], "synthetic")["SMA"])
    witness("W03_LOCAL_POSITIVE_HIDDEN", text, v["state"],
            "同一片段NEG先于POS返回，局部接触信息被阴性覆盖。", v["state"] == "NO_CONTACT_OR_INVASION")

    text = "肠系膜上动脉无接触，肝内结节可疑转移。"
    v = rp.resolve_vessel(rp.vessel_evidence(text, rules["required_vessels"], "synthetic")["SMA"])
    witness("W04_UNCERTAINTY_SCOPE_LEAK", text, v["state"],
            "肝病变的不确定性串入SMA关系，明确阴性变为无法确定。", v["state"] == "EXPLICIT_INDETERMINATE")

    text = "肠系膜上动脉无接触，脾动脉包绕270°。"
    v = rp.resolve_vessel(rp.vessel_evidence(text, rules["required_vessels"], "synthetic")["SMA"])
    witness("W05_OTHER_VESSEL_ANGLE_LEAK", text, {"state": v["state"], "angles": [e["angle"] for e in v["evidence"]]},
            "脾动脉不在定位词表，270度错误附着于SMA，可进一步制造角度冲突。",
            any(e["angle"] and e["angle"]["lo"] == 270 for e in v["evidence"]))

    text = "既往肠系膜上动脉接触180°，本次肠系膜上动脉无接触。"
    v = rp.resolve_vessel(rp.vessel_evidence(text, rules["required_vessels"], "synthetic")["SMA"])
    witness("W06_CURRENT_AFTER_HISTORY_LOST", text, v["state"],
            "全句前缀含既往，连本次结果也被跳过，变成缺失。", v["state"] == "MISSING")

    text = base.replace("大小约15×18mm", "本次20×18mm，较前25×22mm缩小")
    p = assess(text)
    witness("W07_VALID_COMPARISON_BLOCKED", text, p["reasons"],
            "正常的本次/既往比较被多尺寸计数规则阻断。",
            "MULTIPLE_LESION_MEASUREMENTS_REVIEW" in p["reasons"])

    text = base.replace("大小约15×18mm", "胆总管直径12mm")
    p = assess(text)
    witness("W08_DUCT_SIZE_BORROWED", text, {"size": p["parsed"]["lesion_size"], "state": p["state"]},
            "胆总管直径被当作肿瘤尺寸，完整性被高估。",
            p["parsed"]["lesion_size"].get("values_mm") == [12.0] and p["state"] == "PASS")

    text = "肝脏病变：转移灶未见明显变化。"
    value = rp.field_value(text, r"肝脏(?:病变)?")
    witness("W09_STABLE_METASTASIS_NEGATED", text, value,
            "未见变化被当作未见病变，转移灶误标阴性。", value["state"] == "NEGATIVE")

    text = base.replace("其它评估：肝脏病变：无。腹膜或网膜结节：无。", "肝脏及腹膜未见转移。")
    p = assess(text, impression="胰头占位。")
    witness("W10_NARRATIVE_METASTASIS_MISSED", text, p["reasons"],
            "有明确叙述但未用冒号模板，转移域被算未表征。",
            "METASTATIC_DOMAIN_NOT_REPRESENTED" in p["reasons"])

    text = base + "胰十二指肠切除术后。"
    p = assess(text, verified=False)
    witness("W11_POSTOP_HIDES_PROVENANCE_ALERT", text, p["reasons"],
            "术后早返回掩盖独立的来源未核验标记，且把阶段当报告质量门槛。",
            p["reasons"] == ["POSTOPERATIVE_OR_RECURRENCE_CONTEXT"])

    state = classify_patient(True, 1, 1, 0, ["unrelated synthetic file read error"])
    witness("W12_READ_ERROR_PATIENT_VETO", "1条临床文书+1份合格报告+另1文件读取错误", state,
            "局部读取故障成为全患者通过否决；应独立记录运行覆盖与可用记录。", state == "REVIEW_V2")

    # These are current-rule witnesses, not tests of a newly implemented policy.
    assert all(w["reproduced"] for w in witnesses), "A claimed witness did not reproduce."

    patients = {p["patient_uid"]: p for p in read_jsonl(results / "patient_quality.jsonl")}
    reports = list(read_jsonl(results / "report_quality.jsonl"))
    report_counts = Counter()
    by_signal = defaultdict(set)
    by_state = defaultdict(Counter)
    for r in reports:
        uid = r["patient_uid"]
        reasons = set(r["reason_codes"])
        signals = set(reasons)
        represented = sum(v["represented"] for v in r["parsed"]["vessels"].values())
        if 0 < represented < 5:
            signals.add("AUDIT_PARTIAL_1_TO_4_VESSELS")
        if r["quality_state"] == "PASS" and r["parsed"]["template_detected"]:
            signals.add("AUDIT_PASS_WITH_TEMPLATE")
        if r["quality_state"] == "PASS":
            signals.add("AUDIT_ALL_REPORT_PASS")
        if any(s.startswith("VESSEL_") and s.endswith("_MISSING") for s in reasons):
            signals.add("AUDIT_ANY_VESSEL_MISSING_GATE")
        for signal in signals:
            report_counts[signal] += 1
            by_signal[signal].add(uid)
    for signal, uids in by_signal.items():
        by_state[signal].update(patients[u]["state"] for u in uids)
    relevant_signals = ["AUDIT_ALL_REPORT_PASS", "AUDIT_PASS_WITH_TEMPLATE",
                        "AUDIT_PARTIAL_1_TO_4_VESSELS", "AUDIT_ANY_VESSEL_MISSING_GATE",
                        "POSTOPERATIVE_OR_RECURRENCE_CONTEXT", "MULTIPLE_LESION_MEASUREMENTS_REVIEW",
                        "SAME_REPORT_UID_DIFFERENT_CONTENT", "METASTATIC_DOMAIN_NOT_REPRESENTED",
                        "LESION_SIZE_NOT_REPRESENTED", "METASTASIS_FIELD_CONFLICT",
                        "RAW_CLUSTER_NOT_RECONCILED", "CONTRAST_PROTOCOL_UNVERIFIED"]
    counts = {s: {"report_versions": report_counts[s], "patients_with_signal": len(by_signal[s]),
                  "patient_current_states": dict(by_state[s])} for s in relevant_signals}
    source_files = [results / n for n in ["patient_quality.jsonl", "report_quality.jsonl", "summary.json"]]
    source_files += [release / n for n in ["report_parser.py", "run_quality.py", "quality_rules.json"]]
    source_files.append(Path(__file__).resolve())
    output = {"audit_version": "p1-adversarial-audit-v0.1-20261021", "audited_runtime": rules["version"],
              "P0_n": len(patients), "patient_states_unchanged": dict(Counter(p["state"] for p in patients.values())),
              "report_versions": len(reports), "synthetic_witnesses": witnesses,
              "aggregate_rule_exposure": counts,
              "limitations": ["Counts measure rule exposure, not proven clinical errors or revised eligibility.",
                              "Signals overlap; cannot sum or infer an updated P1 cohort.",
                              "No new ABC extractor or cited-report linkage was implemented.",
                              "Original text and frozen membership were not modified."],
              "input_sha256": {str(p.relative_to(repo)): sha256(p) for p in source_files}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"witnesses_reproduced": len(witnesses), "counts": counts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
