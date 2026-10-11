"""HEWV v0.4: institutional episodes and whole-record visibility, local only."""

import argparse
from collections import Counter
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import jsonschema

from .four_stage import (
    LAB_FIELDS, QUESTIONS, TIME_RE, document_time, parse_time,
    protected_output, render_context, save, sha,
)
from .timeline_extract import history_ranges, semantic_diff, visible_at
from .evaluation.protocol import SYSTEM_PROMPT, build_messages, protocol_manifest

VERSION = "hewv-v0.4"
RESULT_FIELDS = ("result_numeric_value", "result_qualitative_value", "result_text_value")
SURGERY = re.compile(r"(?:拟|计划|建议|决定|限期|择期)[^\n。；;]{0,100}(?:手术治疗|根治术|切除术)")
SOURCE_ARRAYS = (("document", "documents_all", "DOC"), ("imaging", "imaging_all", "IMG"),
                 ("laboratory", "laboratory_all", "LAB"), ("pathology", "pathology_all", "PATH"))


def first_time(row, fields):
    for field in fields:
        if row.get(field):
            t = parse_time(row[field])
            return t, field if t else "INVALID_" + field
    return None, "MISSING_AVAILABILITY_TIME"


def sources_for(bundle, review):
    """One row per supplied record: no deduplication or clinical text selection."""
    scope = review["source_scope"]
    if scope.get("default_origin") not in ("hospital", "external") or not scope.get("reason"):
        raise ValueError("Source scope requires an origin and reason")
    sources = []
    for kind, field, prefix in SOURCE_ARRAYS:
        for i, raw in enumerate(bundle.get(field, []), 1):
            sid = f"{prefix}{i:04d}"
            issue, version_issue = None, None
            if kind == "document":
                body = raw.get("document_text_raw", raw.get("document_text", "")) or ""
                title = raw.get("document_title") or "未命名文书"
                t, issue = document_time(body)
                basis = "DOCUMENT_BODY_COMPLETION"
                if t and any((p := parse_time(m.group())) and p.start >= t.end for m in TIME_RE.finditer(body)):
                    version_issue = "DOCUMENT_VERSION_REQUIRES_REVIEW"
                decision = review.get("document_version_reviews", {}).get(sid)
                if decision:
                    if (decision.get("body_sha256") != sha(body) or not decision.get("reason")
                            or decision.get("whole_record_available") is not True):
                        raise ValueError("Version review must bind the whole source and explain availability")
                    # An explanatory review never repairs a missing/conflicting completion timestamp.
                    version_issue = None
            elif kind == "imaging":
                title = raw.get("exam_method") or "未命名影像"
                body = raw.get("report_deidentified") or "\n".join(
                    str(raw[k]) for k in ("description_deidentified", "diagnosis_deidentified") if raw.get(k))
                t, basis = first_time(raw, ("report_time", "report_datetime", "report_release_time"))
                if basis == "MISSING_AVAILABILITY_TIME":
                    proxy = review.get("imaging_exam_proxy", {})
                    if proxy.get("accepted") is True and proxy.get("reason"):
                        t, basis = first_time(raw, ("exam_datetime",))
                        if t and "exam_time_raw" in raw and not raw["exam_time_raw"]:
                            t = parse_time(t.raw[:10])
                        basis = "IMAGING_EXAM_TIME_PROXY" if t else basis
                    else:
                        issue = "IMAGING_RELEASE_TIME_UNKNOWN"
            elif kind == "laboratory":
                title = raw.get("item_name") or "未命名检验"
                body = {k: raw.get(k) for k in LAB_FIELDS}
                body.update({k: raw[k] for k in RESULT_FIELDS if raw.get(k) is not None})
                t, basis = first_time(raw, ("report_time",))
            else:
                title = "病理报告"
                # Preserve every supplied clinical section, including prior surgical pathology.
                body = "\n".join(str(raw[k]) for k in ("specimen_type", "specimen_name", "clinical_diagnosis",
                    "pathology_diagnosis", "microscopy") if raw.get(k))
                row = dict(raw)
                if isinstance(row.get("report_date"), str) and row["report_date"].endswith(" 00:00:00"):
                    row["report_date"] = row["report_date"][:10]
                t, basis = first_time(row, ("report_time", "report_date"))
            origin = scope.get("origins", {}).get(sid, scope["default_origin"])
            if origin not in ("hospital", "external"):
                raise ValueError("Unknown source origin")
            if origin == "external":
                used_exam_proxy = basis == "IMAGING_EXAM_TIME_PROXY"
                received, receipt_basis = first_time(raw, ("hospital_received_time",))
                # External exam/report dates cannot establish receipt by this hospital.
                if t and received:
                    if received.start >= t.end:
                        t, basis = received, receipt_basis
                        if used_exam_proxy:
                            basis = "EXTERNAL_RECEIPT_AND_EXAM_TIME_PROXY"
                    elif t.start < received.end:
                        t, basis = None, "EXTERNAL_RECEIPT_ORDER_UNCERTAIN"
                else:
                    t, basis = None, "EXTERNAL_RECEIPT_OR_RELEASE_UNKNOWN"
            sources.append({"id": sid, "kind": kind, "title": title, "body": body, "raw": raw,
                            "time": t, "basis": basis, "origin": origin,
                            "issue": issue, "version_issue": version_issue})
    known = {s["id"] for s in sources}
    if set(scope.get("origins", {})) - known or set(review.get("document_version_reviews", {})) - known:
        raise ValueError("Review references an absent source")
    return sources


def checkpoints_for(sources, review):
    by_id = {s["id"]: s for s in sources}
    entry = review["episode"]
    source = by_id.get(entry.get("entry_source_id"))
    if not source or source["origin"] != "hospital" or not entry.get("reason"):
        raise ValueError("Episode entry must bind a hospital source and reason")
    basis = entry.get("entry_basis")
    if basis == "FIRST_OBSERVED_HOSPITAL_RECORD":
        raw = source["raw"]
        clinical = parse_time(raw.get("exam_datetime")) if source["kind"] == "imaging" else source["time"]
    elif basis == "REVIEWED_FIRST_HOSPITAL_ENCOUNTER":
        field = entry.get("encounter_time_field")
        if field not in ("encounter_start_time", "visit_time", "admission_time"):
            raise ValueError("First encounter needs a native clinical encounter time field")
        clinical = parse_time(source["raw"].get(field))
    else:
        raise ValueError("Unknown episode entry basis")
    if not clinical:
        raise ValueError("Episode entry time is unresolved")
    episode = {**entry, "entry_time": clinical.raw, "first_visit_confirmed": basis == "REVIEWED_FIRST_HOSPITAL_ENCOUNTER"}
    checkpoints, previous = [], None
    configs = review.get("checkpoints", [])
    if len(configs) != 4 or [c.get("stage") for c in configs] != [1, 2, 3, 4]:
        raise ValueError("Exactly four ordered questions are required; unresolved anchors may be empty")
    for config in configs:
        ids = config.get("source_ids", [])
        if len(set(ids)) != len(ids) or any(i not in by_id for i in ids):
            raise ValueError("Checkpoint references invalid sources")
        if not ids:
            checkpoints.append(None)
            continue
        if not config.get("reason"):
            raise ValueError("Checkpoint requires a source selection reason")
        selected = [by_id[i] for i in ids]
        if any(not s["time"] or s["issue"] or s["version_issue"] or not s["body"] for s in selected):
            checkpoints.append(None)
            continue
        if config["stage"] == 1 and any(s["origin"] != "hospital" for s in selected):
            raise ValueError("First observed disease checkpoint must be a hospital observation")
        position = config.get("position")
        if position == "before_record":
            if len(selected) != 1 or selected[0]["kind"] != "document":
                raise ValueError("Predecision boundary requires one complete document")
            if config.get("task_mode") not in ("independent_decision", "plan_review"):
                raise ValueError("Predecision task mode must be explicit")
            relation = "<"
        elif position == "after_records":
            relation = "<="
        else:
            raise ValueError("Unknown checkpoint position")
        t = max((s["time"] for s in selected), key=lambda x: x.start)
        if t.start < clinical.start or (previous and t.start < previous):
            raise ValueError("Checkpoint precedes episode entry or previous checkpoint")
        previous = t.start
        checkpoints.append({**config, "time": t.raw, "relation": relation,
                            "anchor_source_ids": ids})
    return episode, checkpoints


def whole_context(records):
    context = render_context(records)
    extra = [(r["source_id"], key, r["body"][key]) for r in records if r["kind"] == "laboratory"
             for key in RESULT_FIELDS if key in r["body"]]
    if extra:
        def cell(v):
            return str(v).replace("|", "\\|").replace("\n", "<br>")
        context += "\n检验源包其他结果字段（保留字段名，不代替结果原值）\n\n| 来源 | 原字段名 | 源包值 |\n|---|---|---|\n"
        context += "\n".join("| " + " | ".join(cell(v) for v in row) + " |" for row in extra) + "\n"
    return context


def extract_episode_case(bundle, alias, review):
    schema = Path(__file__).resolve().parents[1] / "contracts/v0.4/04_review.schema.json"
    try:
        jsonschema.validate(review, json.loads(schema.read_text()))
    except jsonschema.ValidationError as exc:
        raise ValueError("Invalid HEWV review schema") from exc
    if (review.get("contract_version") != VERSION or review.get("source_bundle_sha256") != sha(bundle)
            or not review.get("reviewer")):
        raise ValueError("HEWV review requires contract version, source hash and reviewer")
    if any(k in review for k in ("evidence_spans", "discovery_source_id", "plan_source_id")):
        raise ValueError("Legacy excerpt/anchor configuration cannot be silently migrated")
    sources = sources_for(bundle, review)
    episode, checkpoints = checkpoints_for(sources, review)
    plans = []
    for s in sources:
        if s["kind"] != "document" or not s["time"]:
            continue
        historical = history_ranges(s["body"])
        if any(not any(a <= m.start() < b for a, b in historical) for m in SURGERY.finditer(s["body"])):
            plans.append(s)
    packets, decisions = [], []
    for stage, cp in enumerate(checkpoints, 1):
        records, issues = [], []
        if cp and cp.get("task_mode") == "independent_decision":
            earlier = [s["id"] for s in plans if parse_time(episode["entry_time"]).start <= s["time"].start
                       < parse_time(cp["time"]).start]
            if earlier:
                issues.append("EARLIER_SURGICAL_PLAN_CANDIDATE:" + ",".join(earlier))
        for s in sources:
            reason = "NO_CHECKPOINT" if not cp else None
            if reason is None:
                if s["version_issue"]:
                    reason = s["version_issue"]
                elif s["issue"] or not s["time"]:
                    reason = s["issue"] or s["basis"]
                elif cp["position"] == "before_record" and s["id"] in cp["source_ids"]:
                    reason = "TARGET_DECISION_DOCUMENT_EXCLUDED_WHOLE"
                elif not visible_at(s["time"], cp, s["id"] in cp["source_ids"]):
                    reason = "NOT_AVAILABLE_BY_CHECKPOINT"
                elif not s["body"]:
                    reason = "SOURCE_TEXT_MISSING"
            unresolved = reason is not None and (reason == "NO_CHECKPOINT" or s["issue"] or
                                                 s["version_issue"] or not s["time"] or reason == "SOURCE_TEXT_MISSING")
            if unresolved:
                issues.append("SOURCE_VISIBILITY_UNRESOLVED")
            decisions.append({"stage": stage, "source_id": s["id"], "disposition": "unresolved_whole" if unresolved else "excluded_whole" if reason else "included_whole",
                              "reason": reason, "availability_basis": s["basis"], "body_sha256": sha(s["body"])})
            if reason:
                continue
            if "EXAM_TIME_PROXY" in s["basis"]:
                issues.append("IMAGING_EXAM_TIME_PROXY")
            if s["time"].precision != "second":
                issues.append("COARSE_TIME_PRECISION")
            records.append({"source_id": s["id"], "kind": s["kind"], "title": s["title"],
                            "body": s["body"], "display_time": s["time"].raw,
                            "time_label": "结果/正文可见时间或已声明代理", "body_sha256": sha(s["body"])})
        records.sort(key=lambda r: (r["kind"] == "laboratory", parse_time(r["display_time"]).start, r["source_id"]))
        included = {r["source_id"] for r in records}
        anchor_missing = cp and cp["position"] == "after_records" and not set(cp["source_ids"]).issubset(included)
        usable = bool(cp and records and not anchor_missing and not any(i.startswith("EARLIER_") for i in issues))
        context = whole_context(records)
        label, question = QUESTIONS[stage - 1]
        if cp and cp.get("task_mode") == "plan_review":
            question = "请复核资料中已有的治疗建议，说明是否支持、需要补充哪些条件或应如何调整。" + question
        messages = build_messages(question, context) if usable else None
        packets.append({"stage": stage, "label": label, "question": question, "checkpoint": cp,
                        "status": "DRAFT_REVIEW_REQUIRED" if usable else "UNRESOLVED", "records": records,
                        "counts": dict(Counter(r["kind"] for r in records)), "issues": sorted(set(issues)),
                        "context": context, "context_sha256": sha(context), "messages": messages,
                        "messages_sha256": sha(messages)})
    timeline = [{"source_id": s["id"], "kind": s["kind"], "title": s["title"], "body": s["body"],
                 "raw": s["raw"], "origin": s["origin"], "availability_basis": s["basis"],
                 "available_time_or_proxy": s["time"].raw if s["time"] else None,
                 "time_issue": s["issue"], "version_issue": s["version_issue"], "body_sha256": sha(s["body"])} for s in sources]
    timeline.sort(key=lambda r: (r["available_time_or_proxy"] is None,
                                parse_time(r["available_time_or_proxy"]).start if r["available_time_or_proxy"] else parse_time("2099-12-31").end,
                                r["source_id"]))
    result = {"version": VERSION, "case_alias": alias, "episode": episode, "packets": packets}
    audit = {"version": VERSION, "source_bundle_sha256": sha(bundle), "review_sha256": sha(review),
             "model_calls": 0, "create_time_used": False, "semantic_text_selection": False,
             "retrospective_backfill": False, "source_count": len(sources), "source_decisions": decisions,
             "plan_candidate_source_ids": [s["id"] for s in plans], "whole_record_deduplication": False,
             **protocol_manifest(), "question_sha256": [sha(p["question"]) for p in packets]}
    return result, audit, timeline


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("input", "reviews", "output"):
        parser.add_argument("--" + option, required=True, type=Path)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    try:
        output = protected_output(args.output)
        if not re.fullmatch(r"S\d{3,}", args.alias) or args.input.resolve().is_relative_to(output):
            raise ValueError("Invalid alias or output location")
        bundle = None
        with args.input.open() as f:
            for i, line in enumerate(f, 1):
                if i == int(args.alias[1:]):
                    bundle = json.loads(line)
                    break
        if bundle is None:
            raise ValueError("Alias absent from input")
        review = json.loads(args.reviews.read_text())[args.alias]
        result, audit, timeline = extract_episode_case(bundle, args.alias, review)
        comparison = None
        if args.compare:
            old = json.loads(args.compare.read_text())
            old_audit = json.loads(args.compare.with_name("audit.json").read_text())
            if old.get("case_alias") != args.alias or old_audit.get("source_bundle_sha256") != sha(bundle):
                raise ValueError("Comparison source bundle mismatch")
            comparison = semantic_diff(old, result)
        d = output / args.alias
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        audit["input_line"] = int(args.alias[1:])
        audit["rule_files_sha256"] = {name: sha(Path(__file__).with_name(name).read_text())
                                      for name in ("episode_extract.py", "four_stage.py", "timeline_extract.py")}
        audit["review_schema_sha256"] = sha((Path(__file__).resolve().parents[1] / "contracts/v0.4/04_review.schema.json").read_text())
        for name, value in (("packets.json", result), ("audit.json", audit), ("timeline.json", timeline), ("review_manifest.json", review)):
            save(d / name, value)
        for p in result["packets"]:
            save(d / f"{p['stage']:02d}_context.md", p["context"])
            target = d / f"{p['stage']:02d}_messages.json"
            if p["messages"]:
                save(target, p["messages"])
            elif target.exists():
                target.unlink()
        if comparison:
            save(d / "semantic_diff.json", comparison)
        elif (d / "semantic_diff.json").exists():
            (d / "semantic_diff.json").unlink()
        print("HEWV local draft saved; model calls: 0.")
        print(d)
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print("Extraction/configuration error:", str(exc) if type(exc) is ValueError else type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
