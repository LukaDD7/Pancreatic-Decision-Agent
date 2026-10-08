"""Timeline-first, local-only evidence extraction (v0.3). No model execution."""

import argparse
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re

from .four_stage import (
    ABDOMEN_RE, DIAGNOSTIC_PATH_RE, GENERIC_PLAN_RE, MODALITY_RE, NOTE_TITLE_RE,
    PLAN_RE, QUESTIONS, SYSTEM_PROMPT, TIME_RE, build_sources, episode_key,
    future_dates, is_postoperative, parse_time, protected_output,
    render_context, save, sentence_start, sha,
)

VERSION = "four-stage-v0.3-timeline-first"
SECTION_END = re.compile(r"(?:^|\n)\s*(?:诊疗计划|其他诊疗计划|治疗计划|最后诊断|拟施手术|拟手术名称|手术指征|术前准备)\s*[:：]")


def history_ranges(text):
    """Protect explicit historical sections from current-plan keyword cuts."""
    ranges = []
    for pattern, stop in (
        (r"现病史\s*[:：]", r"\n\s*(?:仍需治疗|入院前|既往史|个人史|体格检查)"),
        (r"(?:^|\n)\s*2[、.．]\s*", r"\n\s*3[、.．]\s*"),
        (r"(?:^|\n)\s*诊断依据\s*[:：]", r"\n\s*(?:鉴别诊断|诊疗计划|治疗计划|注意事项)\s*[:：]"),
    ):
        for start in re.finditer(pattern, text):
            end = re.search(stop, text[start.end():])
            ranges.append((start.end(), start.end() + end.start() if end else len(text)))
    return ranges


def clinical_note(source):
    return bool(NOTE_TITLE_RE.search(source["title"]) or
                re.search(r"入\s*院\s*记\s*录", source["body"][:120]))


def note_end(source):
    text = source["body"]
    ranges = history_ranges(text)
    historical = lambda p: any(a <= p < b for a, b in ranges)
    ends = [len(text)]
    for m in SECTION_END.finditer(text):
        if not historical(m.start()):
            ends.append(sentence_start(text, m.end() - 1))
    for regex in (PLAN_RE, GENERIC_PLAN_RE):
        for m in regex.finditer(text):
            if not historical(m.start()):
                ends.append(sentence_start(text, m.start()))
    if re.search(r"入\s*院\s*记\s*录", text[:120]) or "入院记录" in source["title"]:
        for m in re.finditer(r"(?:^|\n)[^\n]{0,40}初步诊断\s*[:：]", text):
            ends.append(sentence_start(text, m.end() - 1))
    end = min(ends)
    while end and text[end - 1].isspace():
        end -= 1
    return end


def timing(source):
    raw, kind = source["raw"], source["kind"]
    if kind == "imaging":
        # Explicit release fields take precedence; malformed fields never fall back.
        for field in ("report_time", "report_datetime", "report_release_time"):
            if raw.get(field):
                t = parse_time(raw[field])
                return t, "IMAGING_REPORT_TIME" if t else "INVALID_IMAGING_REPORT_TIME"
        return source["time"], "IMAGING_EXAM_TIME_PROXY"
    return source["time"], {
        "document": "DOCUMENT_BODY_COMPLETION", "laboratory": "LAB_REPORT_TIME",
        "pathology": "PATHOLOGY_REPORT_DATE_OR_TIME",
    }[kind]


def upper_recorded_time(t):
    """Latest possible second at the recorded precision, not an invented clock."""
    return t.start if t.precision == "second" else t.end - timedelta(seconds=1)


def visible_at(t, checkpoint, anchor=False):
    if not t or not checkpoint:
        return False
    cut = parse_time(checkpoint["time"])
    if anchor and checkpoint["relation"] == "<=":
        return True  # The designated event itself is included, even if date-only.
    if cut.precision == "date":
        # Other same-day events have unknown order relative to this event.
        return t.end <= cut.start
    last = upper_recorded_time(t)
    return last <= cut.start if checkpoint["relation"] == "<=" else last < cut.start


def reviewed_spans(sources, review):
    by_id, accepted = {s["id"]: s for s in sources}, []
    for item in review.get("evidence_spans", []):
        if not item.get("accepted"):
            continue
        s = by_id.get(item.get("source_id"))
        start, end = item.get("start"), item.get("end")
        t = parse_time(item.get("event_time"))
        stages = item.get("stages")
        if (not s or s["kind"] != "document" or sha(s["body"]) != item.get("source_text_sha256")
                or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(s["body"])
                or not t or not item.get("reason") or not review.get("reviewer")
                or not isinstance(stages, list) or not stages
                or any(type(k) is not int or k not in (1, 2, 3, 4) for k in stages)):
            raise ValueError("Invalid or unbound reviewed evidence span")
        body = s["body"][start:end]
        dates = [parse_time(m.group()) for m in TIME_RE.finditer(body)]
        if not any(d and d.start == t.start and d.precision == t.precision for d in dates):
            raise ValueError("Reviewed event date must occur in its original source span")
        if future_dates(body, t.end):
            raise ValueError("Reviewed history contains dates later than its event")
        accepted.append({"id": s["id"] + "-H" + str(len(accepted) + 1), "kind": "document",
                         "title": "既往就诊病史原文节选", "body": body, "time": t,
                         "source": s, "review": item})
    return accepted


def plan_candidates(sources):
    plans = []
    for s in sources:
        if s["kind"] != "document" or is_postoperative(s) or not (clinical_note(s) or "术前" in s["title"]):
            continue
        ranges = history_ranges(s["body"])
        for m in PLAN_RE.finditer(s["body"]):
            if "胰" not in m.group() or not re.search(r"根治|切除", m.group()):
                continue
            if any(a <= m.start() < b for a, b in ranges):
                continue
            plans.append({"source_id": s["id"], "time": s["time"], "episode_key": episode_key(s),
                          "span": list(m.span()), "raw_plan": m.group()})
            break
    return plans


def checkpoints_for(sources, spans, plans, review):
    by_id = {s["id"]: s for s in sources}
    issues = []
    valid = sorted((p for p in plans if p["time"] and p["time"].precision != "date"),
                   key=lambda p: (p["time"].start, p["source_id"]))
    if any(not p["time"] or p["time"].precision == "date" for p in plans):
        issues.append("PLAN_WITH_UNRESOLVED_COMPLETION_TIME")
    if len({p["episode_key"] for p in valid}) > 1:
        issues.append("MULTIPLE_PLANNED_ADMISSIONS")
    chosen = review.get("plan_source_id")
    plan = next((p for p in valid if p["source_id"] == chosen), None) if chosen else (valid[0] if valid else None)
    if chosen and not plan:
        raise ValueError("Reviewed plan source is not a valid timed plan")
    if not plan:
        return [None] * 4, None, issues + ["NO_TIMED_PANCREATIC_RESECTION_PLAN"]
    endpoint = {"time": plan["time"].raw, "relation": "<", "anchor_source_ids": [plan["source_id"]],
                "basis": "BEFORE_TARGET_PLAN_DOCUMENT", "reason": "Exclude the evaluated plan document in full"}
    scans = sorted((s for s in sources if s["kind"] == "imaging" and ABDOMEN_RE.search(s["title"])
                    and MODALITY_RE.search(s["title"]) and visible_at(timing(s)[0], endpoint)),
                   key=lambda s: (timing(s)[0].start, s["id"]))
    discovery_id = review.get("discovery_source_id")
    discovery_source = next((s for s in spans if s["id"] == discovery_id), None) or by_id.get(discovery_id)
    if discovery_id and not discovery_source:
        raise ValueError("Discovery source is absent")
    if not discovery_source:
        discovery_source = scans[0] if scans else None
        issues.append("FIRST_AVAILABLE_SCAN_NOT_CONFIRMED_FIRST_DISCOVERY")
    discovery = None
    if discovery_source:
        t = discovery_source["time"] if "review" in discovery_source else timing(discovery_source)[0]
        if not t or not visible_at(t, endpoint):
            raise ValueError("Discovery event is not safely before the plan boundary")
        if discovery_source["kind"] != "imaging" and "review" not in discovery_source:
            raise ValueError("Discovery requires a report or a bound reviewed reference")
        discovery = {"time": t.raw, "relation": "<=", "anchor_source_ids": [discovery_source["id"]],
                     "basis": "REVIEWED_DISCOVERY_REFERENCE" if "review" in discovery_source else timing(discovery_source)[1],
                     "reason": review.get("discovery_reason") or "First available related scan, not certified disease onset"}
        companions = review.get("discovery_companion_source_ids", [])
        if (not isinstance(companions, list) or len(set(companions)) != len(companions)
                or (companions and not review.get("discovery_reason"))):
            raise ValueError("Invalid discovery companion references")
        for source_id in companions:
            companion = next((s for s in spans if s["id"] == source_id), None)
            # Noncontiguous citations of the same external examination may be
            # co-anchors, but a matching day alone never opens other records.
            if (not companion or "review" not in discovery_source
                    or companion["source"]["id"] != discovery_source["source"]["id"]
                    or companion["time"].start != t.start or companion["time"].precision != t.precision
                    or source_id == discovery_source["id"] or 1 not in companion["review"]["stages"]):
                raise ValueError("Discovery companion must be a reviewed citation of the same source event")
            discovery["anchor_source_ids"].append(source_id)
    ids = review.get("staging_source_ids")
    if ids is not None and (not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)):
        raise ValueError("Invalid staging source list")
    enhanced = [s for s in scans if "增强" in s["title"]]
    round_sources = [by_id.get(i) for i in ids] if ids else enhanced[:1]
    if ids and (not review.get("staging_reason") or any(not s or s not in enhanced for s in round_sources)):
        raise ValueError("Reviewed staging reports must be enhanced scans before the plan")
    staging = None
    if round_sources:
        last = max(round_sources, key=lambda s: upper_recorded_time(timing(s)[0]))
        t = timing(last)[0]
        # Keep coarse precision; do not invent the last second of a minute/day.
        staging = {"time": t.raw, "relation": "<=", "anchor_source_ids": [s["id"] for s in round_sources],
                   "basis": "SELECTED_STAGING_REPORTS_RETURNED", "reason": review.get("staging_reason") or "First available enhanced report"}
    if discovery and staging and parse_time(staging["time"]).start < parse_time(discovery["time"]).start:
        raise ValueError("Staging checkpoint precedes discovery")
    return [discovery, staging, endpoint, dict(endpoint)], plan, issues


def source_record(s, body=None, source_id=None, display_time=None, time_label=None):
    kind = s["kind"]
    t, basis = timing(s)
    labels = {"document": "文书正文完成时间", "imaging": "报告时间" if basis == "IMAGING_REPORT_TIME" else "检查时间",
              "laboratory": "报告时间", "pathology": "病理报告日期/时间"}
    body = s["body"] if body is None else body
    return {"source_id": source_id or s["id"], "kind": kind, "title": s["title"], "body": body,
            "display_time": display_time or (t.raw if t else None), "time_label": time_label or labels[kind],
            "body_sha256": sha(body)}


def pack(sources, spans, checkpoint, stage, plan_id, review):
    records, decisions, issues = [], [], []
    for s in sources:
        t, basis = timing(s)
        reason, span = None, None
        if not checkpoint:
            reason = "NO_CHECKPOINT"
        elif s["kind"] == "document" and s["id"] == plan_id:
            reason = "TARGET_PLAN_DOCUMENT_ISOLATED"
        elif s["kind"] == "document" and (is_postoperative(s) or "术前" in s["title"]):
            # Prior procedure facts need a reviewed excerpt, not wholesale admission.
            reason = "PROCEDURE_OR_PLAN_DOCUMENT_REQUIRES_REVIEW"
        elif s["kind"] == "document" and not clinical_note(s):
            reason = "NON_CLINICAL_NOTE_OR_UNRECOGNIZED_TEMPLATE"
        elif not t:
            reason = s["time_issue"] or "AVAILABLE_TIME_MISSING_OR_INVALID"
        elif basis == "IMAGING_EXAM_TIME_PROXY" and not review.get("allow_imaging_exam_proxy"):
            reason = "IMAGING_RETURN_TIME_REQUIRES_REVIEW"
        elif not visible_at(t, checkpoint, s["id"] in checkpoint["anchor_source_ids"]):
            reason = "NOT_AVAILABLE_BY_CHECKPOINT"
        elif s["kind"] == "pathology" and not DIAGNOSTIC_PATH_RE.search(str(s["raw"].get("specimen_type", "")) + str(s["raw"].get("specimen_name", ""))):
            reason = "PATHOLOGY_TYPE_REQUIRES_REVIEW"
        elif not s["body"]:
            reason = "SOURCE_TEXT_MISSING"
        if reason is None and s["kind"] == "document":
            end = note_end(s)
            span = [0, end]
            text = s["body"][:end]
            if re.search(r"\[(?:主诉|现病史|专科检查|辅助检查)\]", text):
                reason = "UNFILLED_CLINICAL_TEMPLATE"
            elif len(text.strip()) < 30:
                reason = "NO_SAFE_CLINICAL_TEXT"
            elif future_dates(text, parse_time(checkpoint["time"]).start +
                              (timedelta(seconds=1) if checkpoint["relation"] == "<=" else timedelta())):
                reason = "NOTE_CONTAINS_LATER_EVENT_REQUIRES_SPAN_REVIEW"
            elif end < len(s["body"].rstrip()):
                issues.append("CURRENT_PLAN_OR_MIXED_FOOTER_REMOVED")
        d = {"stage": stage, "source_id": s["id"], "disposition": "excluded" if reason else "included",
             "reason": reason, "availability_basis": basis}
        if span:
            d["span"] = span
        decisions.append(d)
        if reason:
            continue
        if basis == "IMAGING_EXAM_TIME_PROXY":
            issues.append("REVIEWED_IMAGING_RETURN_ASSUMPTION_USING_EXAM_TIME")
        if t.precision != "second":
            issues.append("COARSE_SOURCE_TIME_PRECISION")
        records.append(source_record(s, s["body"][:span[1]] if span else None))
    for s in spans:
        if stage not in s["review"]["stages"]:
            continue
        is_anchor = bool(checkpoint and s["id"] in checkpoint["anchor_source_ids"])
        reason = None
        if not checkpoint or not visible_at(s["time"], checkpoint, is_anchor):
            reason = "REVIEWED_EVENT_NOT_AVAILABLE_BY_CHECKPOINT"
        elif any(r["source_id"] == s["source"]["id"] and s["body"] in r["body"] for r in records):
            reason = "ALREADY_PRESENT_IN_VISIBLE_ORIGINAL_NOTE"
        if reason is None:
            records.append({**source_record(s["source"], s["body"], s["id"], s["time"].raw, "既往事件日期/时间"), "title": s["title"]})
            issues.append("REVIEWED_RETROSPECTIVE_HISTORY_RECONSTRUCTION")
        decisions.append({"stage": stage, "source_id": s["id"], "original_source_id": s["source"]["id"],
                          "disposition": "excluded" if reason else "included_reviewed_span", "reason": reason,
                          "span": [s["review"]["start"], s["review"]["end"]], "availability_basis": "REVIEWED_RETROSPECTIVE_EVENT"})
    records.sort(key=lambda r: (r["kind"] == "laboratory", r["display_time"] or "", r["source_id"]))
    if checkpoint and parse_time(checkpoint["time"]).precision == "date":
        issues.append("DATE_ONLY_ANCHOR_SAME_DAY_ORDER_UNKNOWN")
    if stage in (1, 2) and checkpoint and not set(checkpoint["anchor_source_ids"]).issubset({r["source_id"] for r in records}):
        issues.append("ANCHOR_EVIDENCE_NOT_AVAILABLE")
    context = render_context(records)
    usable = bool(checkpoint and records and "ANCHOR_EVIDENCE_NOT_AVAILABLE" not in issues)
    label, question = QUESTIONS[stage - 1]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "问题：" + question + "\n\n患者资料：\n\n" + context}] if usable else None
    return {"stage": stage, "label": label, "question": question, "checkpoint": checkpoint,
            "status": "DRAFT_REVIEW_REQUIRED" if usable else "UNRESOLVED", "records": records,
            "counts": dict(Counter(r["kind"] for r in records)), "issues": sorted(set(issues)),
            "context": context, "context_sha256": sha(context), "messages": messages,
            "messages_sha256": sha(messages)}, decisions


def extract_timeline_case(bundle, alias, review=None):
    review = review or {}
    if review and (review.get("source_bundle_sha256") != sha(bundle) or not review.get("reviewer")):
        raise ValueError("Review must bind the source bundle hash and reviewer")
    if review.get("allow_imaging_exam_proxy") and not review.get("imaging_proxy_reason"):
        raise ValueError("Imaging availability assumption requires an explicit reason")
    sources, duplicates = build_sources(bundle)
    spans = reviewed_spans(sources, review)
    plans = plan_candidates(sources)
    checkpoints, plan, global_issues = checkpoints_for(sources, spans, plans, review)
    payloads = {}
    for s in sources:
        for ref in s["source_refs"]:
            if ref["source_key"]:
                payloads.setdefault((s["kind"], ref["source_key"]), set()).add(sha(s["body"]))
    conflicts = [{"kind": key[0], "source_key": key[1], "payload_hashes": sorted(values)}
                 for key, values in payloads.items() if len(values) > 1]
    if conflicts:
        global_issues.append("CONFLICTING_PAYLOADS_FOR_SAME_SOURCE")
    packets, decisions = [], []
    for k, cp in enumerate(checkpoints, 1):
        p, ds = pack(sources, spans, cp, k, plan["source_id"] if plan else None, review)
        p["issues"] = sorted(set(p["issues"] + global_issues))
        packets.append(p)
        decisions.extend(ds)
    assert packets[2]["context_sha256"] == packets[3]["context_sha256"]
    timeline = []
    for s in sources:
        t, basis = timing(s)
        clinical = parse_time(s["raw"].get("sample_time")) if s["kind"] == "laboratory" else s["time"]
        timeline.append({"source_id": s["id"], "kind": s["kind"], "title": s["title"], "body": s["body"],
                         "clinical_time": clinical.raw if clinical else None,
                         "document_completion_time": s["time"].raw if s["kind"] == "document" and s["time"] else None,
                         "available_time_or_proxy": t.raw if t else None, "time_precision": t.precision if t else None,
                         "availability_basis": basis, "source_refs": s["source_refs"], "source_body_sha256": sha(s["body"]),
                         "sort_time": (clinical or t).raw if clinical or t else None})
    for s in spans:
        timeline.append({"source_id": s["id"], "kind": "reviewed_historical_event", "title": s["title"], "body": s["body"],
                         "clinical_time": s["time"].raw, "document_completion_time": s["source"]["time"].raw if s["source"]["time"] else None,
                         "available_time_or_proxy": s["time"].raw, "time_precision": s["time"].precision,
                         "availability_basis": "REVIEWED_RETROSPECTIVE_EVENT", "original_source_id": s["source"]["id"],
                         "source_refs": s["source"]["source_refs"], "span": [s["review"]["start"], s["review"]["end"]],
                         "source_body_sha256": sha(s["body"]), "sort_time": s["time"].raw})
    timeline.sort(key=lambda x: (x["sort_time"] is None, parse_time(x["sort_time"]).start if x["sort_time"] else datetime.max, x["source_id"]))
    auxiliary, auxiliary_decisions = [], {}
    # Preserve alternative image checkpoints without replacing the four questions.
    for s in sources:
        t, basis = timing(s)
        if (s["kind"] == "imaging" and t and ABDOMEN_RE.search(s["title"]) and MODALITY_RE.search(s["title"])
                and checkpoints[2] and visible_at(t, checkpoints[2])):
            cp = {"time": t.raw, "relation": "<=", "anchor_source_ids": [s["id"]], "basis": basis,
                  "reason": "Alternative checkpoint immediately after this report event"}
            p, ds = pack(sources, spans, cp, 1, plan["source_id"], review)
            auxiliary_decisions[s["id"]] = ds
            auxiliary.append({"anchor_source_id": s["id"], **p})
    auxiliary.sort(key=lambda p: (parse_time(p["checkpoint"]["time"]).start, p["anchor_source_id"]))
    audit = {"version": VERSION, "case_alias": alias, "source_bundle_sha256": sha(bundle), "review_sha256": sha(review),
             "model_calls": 0, "global_issues": global_issues, "selected_plan_source_id": plan["source_id"] if plan else None,
             "target_admission_key": plan["episode_key"] if plan else None,
             "plan_candidates": [{**p, "time": p["time"].raw if p["time"] else None} for p in plans],
             "checkpoints": checkpoints, "source_decisions": decisions, "auxiliary_source_decisions": auxiliary_decisions,
             "duplicates": duplicates, "source_payload_conflicts": conflicts,
             "reviewed_evidence_spans": review.get("evidence_spans", []), "create_time_used": False,
             "timeline_source_count": len(timeline), "lookback_window": None}
    return {"version": VERSION, "case_alias": alias, "packets": packets, "auxiliary_checkpoints": auxiliary}, audit, timeline


def semantic_diff(old, new):
    changes = []
    for a, b in zip(old["packets"], new["packets"]):
        left = {r["source_id"]: r for r in a["records"]}
        right = {r["source_id"]: r for r in b["records"]}
        old_payloads = {(r["kind"], r["body_sha256"]) for r in a["records"]}
        new_payloads = {(r["kind"], r["body_sha256"]) for r in b["records"]}
        changes.append({"stage": b["stage"], "old_boundary_exclusive": a.get("boundary_exclusive"),
                        "old_checkpoint": a.get("checkpoint"),
                        "new_checkpoint": b["checkpoint"], "old_counts": a["counts"], "new_counts": b["counts"],
                        "added_source_ids": sorted(right.keys() - left.keys()),
                        "removed_source_ids": sorted(left.keys() - right.keys()),
                        "body_changed_source_ids": sorted(k for k in left.keys() & right.keys() if left[k]["body_sha256"] != right[k]["body_sha256"]),
                        "metadata_changed_source_ids": sorted(k for k in left.keys() & right.keys()
                            if any(left[k].get(f) != right[k].get(f) for f in ("title", "display_time", "time_label"))),
                        "distinct_payloads_added": len(new_payloads - old_payloads),
                        "distinct_payloads_removed": len(old_payloads - new_payloads),
                        "old_context_sha256": a["context_sha256"], "new_context_sha256": b["context_sha256"]})
    return {"old_version": old["version"], "new_version": new["version"], "stages": changes}


def write_result(output, result, audit, timeline, comparisons):
    d = output / result["case_alias"]
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    d.mkdir(exist_ok=True, mode=0o700)
    d.chmod(0o700)
    save(d / "packets.json", result)
    save(d / "audit.json", audit)
    save(d / "timeline.json", timeline)
    lines = [f"# {result['case_alias']} 时间线优先试验", "", "四个独立问题；无模型调用。报告时点含经审阅的代理假设，尚非临床金标准。", "",
             "| 阶段 | 关系 | 锚点 | 文书 | 影像 | 检验 | 病理 |", "|---|---|---|---:|---:|---:|---:|"]
    for p in result["packets"]:
        stem = f"{p['stage']:02d}"
        save(d / f"{stem}_context.md", p["context"] if p["messages"] else "该阶段未可靠定位。\n")
        if p["messages"]:
            save(d / f"{stem}_messages.json", p["messages"])
        elif (d / f"{stem}_messages.json").exists():
            (d / f"{stem}_messages.json").unlink()
        cp, c = p["checkpoint"] or {}, p["counts"]
        lines.append(f"| [{p['stage']} {p['label']}]({stem}_context.md) | {cp.get('relation', '')} | {cp.get('time', '未知')} | {c.get('document',0)} | {c.get('imaging',0)} | {c.get('laboratory',0)} | {c.get('pathology',0)} |")
    lines += ["", "## 可供比较的逐份影像返回后输入", ""]
    for p in result["auxiliary_checkpoints"]:
        name = "after_" + p["anchor_source_id"] + "_context.md"
        save(d / name, p["context"] if p["messages"] else "报告可见性尚未接受，不能作为模型输入。\n")
        lines.append(f"- [{p['anchor_source_id']} · {p['checkpoint']['time']}]({name})：{p['counts']}")
    lines += ["", "## 待审约定", ""]
    lines.extend(f"- 阶段 {p['stage']}：" + ", ".join(p["issues"]) for p in result["packets"])
    save(d / "README.md", "\n".join(lines) + "\n")
    tl = ["# 完整来源时间线（审阅专用，不是模型输入）", "", "包括拟术式、术后及晚病理，仅用于检查来源和边界。create_time 不参与排序或可见性判断；未知时间单列。", "",
          "临床事件时间与结果可见时间分别保留；检验按采样事件排序时，模型开放仍只按报告时间。日期级记录未赋予具体钟点。", "",
          "## 日期概览", "", "| 日期 | 来源/事件 |", "|---|---|"]
    days = {}
    for x in timeline:
        day = parse_time(x["sort_time"]).start.strftime("%Y-%m-%d") if x["sort_time"] else "未知时间"
        bucket = days.setdefault(day, {"labs": 0, "events": []})
        if x["kind"] == "laboratory":
            bucket["labs"] += 1
        else:
            bucket["events"].append(x["source_id"] + " " + x["title"])
    for day, bucket in days.items():
        events = bucket["events"] + ([f"检验 {bucket['labs']} 项（按事件日期汇总，开放仍按各自报告时间）"] if bucket["labs"] else [])
        tl.append(f"| {day} | " + "；".join(events).replace("|", "\\|") + " |")
    tl += ["", "## 逐条时间索引", "", "| 来源 | 类型/标题 | 临床/记录时间 | 结果可见时间或代理 | 依据 |", "|---|---|---|---|---|"]
    for x in timeline:
        tl.append("| " + " | ".join(str(v or "未知").replace("|", "\\|") for v in (x["source_id"], x["title"], x["clinical_time"], x["available_time_or_proxy"], x["availability_basis"])) + " |")
    save(d / "timeline.md", "\n".join(tl) + "\n")
    save(d / "semantic_diff.json", comparisons)
    diff = ["# 病例 semantic diff", "", "此处比较实际边界、资料集合和正文，不按纯文本行差异判断质量。旧输出保留。", ""]
    for name, comparison in comparisons.items():
        diff += [f"## 相对 {name}", "", "| 阶段 | 旧边界（<） | 新边界 | 旧计数 | 新计数 | 来源新增/移除/正文变化 | 真实正文载荷新增/移除 |", "|---|---|---|---|---|---|---|"]
        for x in comparison["stages"]:
            cp = x["new_checkpoint"] or {}
            old_cp = x["old_checkpoint"] or {}
            old_boundary = (f"{old_cp.get('relation','')} {old_cp.get('time','')}" if old_cp
                            else f"< {x['old_boundary_exclusive']}")
            diff.append(f"| {x['stage']} | {old_boundary} | {cp.get('relation','')} {cp.get('time','')} | {x['old_counts']} | {x['new_counts']} | {len(x['added_source_ids'])}/{len(x['removed_source_ids'])}/{len(x['body_changed_source_ids'])} | {x['distinct_payloads_added']}/{x['distinct_payloads_removed']} |")
    diff += ["", "来源别名变化可能导致新增/移除各一条，但正文载荷未变；真实信息变化应结合最后一列和锚点变化判断。", ""]
    save(d / "SEMANTIC_DIFF.md", "\n".join(diff) + "\n")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--reviews", required=True, type=Path)
    parser.add_argument("--compare", action="append", default=[], type=Path)
    args = parser.parse_args()
    try:
        output = protected_output(args.output)
        if not re.fullmatch(r"S\d{3,}", args.alias):
            raise ValueError("Invalid source line alias")
        if args.input.resolve().is_relative_to(output):
            raise ValueError("Input cannot be inside output directory")
        reviews = json.loads(args.reviews.read_text())
        if args.alias not in reviews:
            raise ValueError("Selected case requires a review manifest")
        target = int(args.alias[1:])
        bundle, input_hash = None, hashlib.sha256()
        with args.input.open("rb") as f:
            for i, line in enumerate(f, 1):
                input_hash.update(line)
                if i == target:
                    bundle = json.loads(line)
        if bundle is None:
            raise ValueError("Alias absent from source file")
        result, audit, timeline = extract_timeline_case(bundle, args.alias, reviews[args.alias])
        audit.update(input_line=target, input_file_sha256=input_hash.hexdigest(),
                     rule_files_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path(__file__), Path(__file__).with_name("four_stage.py"))},
                     system_prompt_sha256=sha(SYSTEM_PROMPT))
        comparisons = {}
        for p in args.compare:
            old = json.loads(p.read_text())
            if old["case_alias"] != args.alias:
                raise ValueError("Comparison case mismatch")
            old_audit = json.loads(p.with_name("audit.json").read_text())
            if old_audit["source_bundle_sha256"] != sha(bundle):
                raise ValueError("Comparison source bundle mismatch")
            comparisons[str(p.parent.parent.name)] = semantic_diff(old, result)
        write_result(output, result, audit, timeline, comparisons)
        save(output / args.alias / "review_manifest.json", reviews[args.alias])
        print(f"Extracted {args.alias}: {len(timeline)} timeline entries; model calls: 0.")
        print(output / args.alias)
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print("Extraction/configuration error:", str(exc) if type(exc) is ValueError else type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
