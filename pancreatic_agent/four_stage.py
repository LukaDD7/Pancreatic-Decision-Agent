"""Deterministic, local-only four-context extraction. No policy or API execution."""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
VERSION = "four-stage-v0.2"
SYSTEM_PROMPT = """你参与一项回顾性临床决策研究。请依据用户提供的当前阶段临床资料，回答该阶段的问题。
这些资料构成本次判断可使用的患者信息。不要假设未提供的检查结果、后续诊疗经过或最终结局。
区分原始记载、临床推断和未知信息；未记录不等于阴性，疑似发现不自动等于已经确诊。
建议应与当前证据及临床问题相匹配。如建议进一步检查，说明要解决什么问题以及结果如何改变处置，避免仅因资料不完整而笼统增加检查。
涉及手术时，说明目的与实施前提，区分诊断性探查和根治性切除。
关键判断引用资料来源编号及简短原文。如引用指南，注明名称和年份；不确定时不要编造出处。
可以提出多个合理选项，请明确首选下一步及其理由。
依次输出：当前判断；首选下一步及实施前提；支持判断的资料依据；主要不确定性及可能改变建议的信息。保持简洁。"""
QUESTIONS = (
    ("发现病变", "目前需要优先解决什么临床问题？下一步建议如何检查或评估？"),
    ("诊断/分期", "目前最可能的诊断是什么？现有资料支持怎样的临床分期判断，还有哪些不能确定？"),
    ("可切除性评估", "现有资料下，如何评价根治性切除的可行性及患者承受治疗的条件？哪些问题仍需明确？"),
    ("治疗选择", "现阶段推荐的下一步治疗或处置策略是什么？请说明目的、实施前提、可替代路径及改变策略的条件。"),
)
TIME_RE = re.compile(
    r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?"
    r"(?:[T\s]+(\d{1,2})[:：时](\d{2})(?:[:：分](\d{2}))?分?)?"
)
PLAN_RE = re.compile(
    r"(?:拟施手术(?:名称和手术方式|名称|方式)?|拟手术名称|拟施术式|拟实施术式)\s*[:：][^\n。；;]{0,160}"
    r"|(?:拟[^\n。；;]{0,10}?行|计划(?:行|实施)|决定(?:行|实施)|建议(?:行|实施)|限期行|择期行|近期行)"
    r"[^\n。；;]{0,120}(?:根治|切除)术"
)
POST_TITLE_RE = re.compile(r"术后|出院|ICU|重症|手术记录|胰腺肿瘤手术|手术患者|手术护理")
NOTE_TITLE_RE = re.compile(r"入院记录|首次病程|查房|日常病程|病程记录")
TAIL_RE = re.compile(r"诊疗计划|其他诊疗计划|治疗计划|最后诊断|拟施手术|拟手术名称|手术指征|术前准备")
GENERIC_PLAN_RE = re.compile(
    r"(?:拟[^\n。；;]{0,10}?行|限期行|择期行|近期行|建议)[^\n。；;]{0,50}(?:手术|根治|切除)"
    r"|(?:排除|无明确|无)[^\n。；;]{0,10}手术禁忌|做好[^\n。；;]{0,10}术前准备"
)
ABDOMEN_RE = re.compile(r"胰|上腹|中腹|全腹|肝|胆")
MODALITY_RE = re.compile(r"CT|MR|磁共振", re.I)
DIAGNOSTIC_PATH_RE = re.compile(r"穿刺|细针|FNA|活检|细胞|刷检|EUS|小标本", re.I)
LAB_FIELDS = ("item_name", "result_raw", "unit_raw", "sample_time", "report_time", "source_abnormal_flag")


def sha(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime
    raw: str
    precision: str


def parse_time(value):
    if not isinstance(value, str):
        return None
    match = TIME_RE.fullmatch(value.strip())
    if not match:
        return None
    y, m, d, h, minute, second = match.groups()
    try:
        start = datetime(int(y), int(m), int(d), int(h or 0), int(minute or 0), int(second or 0))
    except ValueError:
        return None
    precision = "second" if second else "minute" if h else "date"
    delta = {"second": timedelta(seconds=1), "minute": timedelta(minutes=1), "date": timedelta(days=1)}[precision]
    return TimeRange(start, start + delta, match.group(), precision)


def document_time(text):
    candidates = []
    start = len(text) - len(text.lstrip())
    head = TIME_RE.match(text, start)
    if head:
        candidates.append(parse_time(head.group()))
    for label in re.finditer(r"记录时间\s*[:：]\s*", text):
        match = TIME_RE.match(text, label.end())
        if match:
            candidates.append(parse_time(match.group()))
    candidates = [c for c in candidates if c]
    distinct = {(c.start, c.end) for c in candidates}
    if len(distinct) != 1:
        return None, "DOCUMENT_TIME_AMBIGUOUS" if distinct else "DOCUMENT_TIME_MISSING"
    return candidates[0], None


def iso(value):
    return value.isoformat(sep=" ") if value else None


def surgical_plan(text):
    for match in PLAN_RE.finditer(text):
        if "胰" in match.group() and re.search(r"根治|切除", match.group()):
            return match
    return None


def sentence_start(text, position):
    return max(text.rfind("\n", 0, position), text.rfind("。", 0, position), text.rfind("；", 0, position)) + 1


def note_prefix(text, title):
    ends = [len(text)]
    for match in TAIL_RE.finditer(text):
        ends.append(sentence_start(text, match.start()))
    for regex in (PLAN_RE, GENERIC_PLAN_RE):
        for match in regex.finditer(text):
            ends.append(sentence_start(text, match.start()))
    if "入院记录" in title:
        for match in re.finditer(r"初步诊断", text):
            ends.append(sentence_start(text, match.start()))
    end = min(ends)
    while end and text[end - 1].isspace():
        end -= 1
    return end


def history_candidate(text):
    start = re.search(r"(?:^|\n)\s*2[、.．]\s*", text)
    if not start:
        return None
    end = re.search(r"\n\s*3[、.．]\s*", text[start.end():])
    if not end:
        return None
    return [start.end(), start.end() + end.start()]


def future_dates(text, cutoff):
    """Screen explicit dates only; absence of a hit does not certify temporality."""
    return sorted({m.group() for m in TIME_RE.finditer(text)
                   if (t := parse_time(m.group())) and
                   (t.start >= cutoff if t.precision != "date" else
                    t.start.date() > cutoff.date() or
                    (t.start.date() == cutoff.date() and cutoff.time() == datetime.min.time()))})


def source_ref(kind, row, index):
    keys = {"document": "source_record_id", "imaging": "source_record_key", "laboratory": "source_record_key", "pathology": "source_record_key"}
    return {"kind": kind, "row_index": index, "source_key": row.get(keys[kind]),
            "source_file": row.get("source_file", row.get("source_file_name"))}


def build_sources(bundle):
    sources, duplicates, seen = [], [], {}
    fields = (("document", "documents_all", "DOC"), ("imaging", "imaging_all", "IMG"),
              ("laboratory", "laboratory_all", "LAB"), ("pathology", "pathology_all", "PATH"))
    for kind, field, prefix in fields:
        for index, row in enumerate(bundle.get(field, [])):
            if not isinstance(row, dict):
                raise ValueError("Source rows must be objects")
            if kind == "document":
                body = row.get("document_text") or ""
                time, time_issue = document_time(body)
                title = row.get("document_title") or "未命名文书"
                identity = row.get("document_id") or row.get("source_record_id")
            elif kind == "imaging":
                body = row.get("report_deidentified") or ""
                time, time_issue = parse_time(row.get("exam_datetime")), None
                if "exam_time_raw" in row and not row["exam_time_raw"] and time:
                    time = parse_time(time.start.strftime("%Y-%m-%d"))
                title = row.get("exam_method") or "未命名影像"
                identity = row.get("index_report_uid") or row.get("source_record_key")
            elif kind == "laboratory":
                body = {k: row.get(k) for k in LAB_FIELDS}
                time, time_issue = parse_time(row.get("report_time")), None
                title = row.get("item_name") or "未命名检验"
                identity = row.get("source_record_key")
            else:
                body = "\n".join(str(row.get(k) or "") for k in ("pathology_diagnosis", "microscopy")).rstrip()
                report_date = row.get("report_date")
                # This source schema stores date-only pathology as midnight.
                if isinstance(report_date, str) and report_date.endswith(" 00:00:00"):
                    report_date = report_date[:10]
                time, time_issue = parse_time(report_date), None
                title = "病理报告"
                identity = row.get("pathology_record_uid") or row.get("source_record_key")
            ref = source_ref(kind, row, index)
            key = (kind, identity, sha(body), title, time.raw if time else None)
            # Render identical reports once, retaining every original source reference.
            # This is presentation deduplication, not inference about clinical event IDs.
            if kind == "imaging" and body and time:
                key = (kind, "identical_presentation", sha(body), title, time.raw)
            can_merge = bool(identity) or (kind == "imaging" and body and time)
            if can_merge and key in seen:
                seen[key]["source_refs"].append(ref)
                duplicates.append({"source_ref": ref, "duplicate_of": seen[key]["id"],
                                   "reason": "IDENTICAL_IMAGING_PRESENTATION" if kind == "imaging" else "SAME_SOURCE_AND_PAYLOAD"})
                continue
            source = {"id": f"{prefix}{index + 1:04d}", "kind": kind, "title": title, "body": body,
                      "time": time, "time_issue": time_issue, "raw": row, "source_refs": [ref]}
            if can_merge:
                seen[key] = source
            sources.append(source)
    return sources, duplicates


def is_postoperative(source):
    return bool(POST_TITLE_RE.search(source["title"]) or re.search(
        r"(?:^|\n)\s*手术时间\s*[:：][^\n]*\d{1,2}[:：]\d{2}", source["body"]))


def episode_key(source):
    raw = source["raw"]
    return raw.get("admission_time"), str(raw.get("visit_id", ""))


def build_episode(sources, review, lookback_days):
    issues, plans = [], []
    for s in sources:
        if s["kind"] != "document" or is_postoperative(s):
            continue
        if not (NOTE_TITLE_RE.search(s["title"]) or "术前" in s["title"]):
            continue
        match = surgical_plan(s["body"])
        if not match:
            continue
        plans.append({"source_id": s["id"], "time": s["time"], "episode": episode_key(s),
                      "span": list(match.span()), "raw_plan": match.group(), "source": s})
    valid = sorted((p for p in plans if p["time"] and p["time"].precision != "date"),
                   key=lambda p: (p["time"].start, p["source_id"]))
    if any(not p["time"] or p["time"].precision == "date" for p in plans):
        issues.append("PLAN_WITH_UNRESOLVED_COMPLETION_TIME")
    if not valid:
        return None, plans, issues + ["NO_TIMED_PANCREATIC_RESECTION_PLAN"]
    selected_id = review.get("plan_source_id")
    if selected_id:
        selected = next((p for p in valid if p["source_id"] == selected_id), None)
        if not selected:
            raise ValueError("Reviewed plan_source_id is not a valid timed plan")
    else:
        selected = valid[0]
    if len({p["episode"] for p in valid}) > 1:
        issues.append("MULTIPLE_PLANNED_ADMISSIONS")
    admission = parse_time(selected["episode"][0])
    if not admission:
        return None, plans, issues + ["EPISODE_ADMISSION_TIME_MISSING"]
    cutoff = selected["time"].start
    if cutoff < admission.start:
        issues.append("PLAN_BEFORE_ADMISSION")
    window_start = admission.start - timedelta(days=lookback_days)
    other_admissions = {episode_key(s) for s in sources if s["kind"] == "document"
                        and episode_key(s) != selected["episode"] and parse_time(episode_key(s)[0])
                        and window_start <= parse_time(episode_key(s)[0]).start < cutoff}
    if other_admissions:
        issues.append("LOOKBACK_OVERLAPS_OTHER_ADMISSION")
    episode = {"selected_plan_source_id": selected["source_id"], "episode_key": selected["episode"],
               "admission": admission.start, "window_start": window_start, "cutoff": cutoff,
               "cutoff_basis": "document_completion_lower_bound_exclusive", "lookback_days": lookback_days}
    return episode, plans, issues


def render_context(records):
    parts, labs = [], []
    for r in records:
        if r["kind"] == "laboratory":
            labs.append(r)
            continue
        parts.append(f"资料 [{r['source_id']}] · {r['title']}\n{r['time_label']}：{r['display_time'] or '未提供'}\n\n{r['body']}")
    if labs:
        def cell(value):
            return str(value if value is not None else "").replace("|", "\\|").replace("\n", "<br>")
        lines = ["检验结果（逐项原值）", "", "| 来源 | 项目 | 结果 | 单位 | 标本时间 | 报告时间 | 源异常标记 |",
                 "|---|---|---|---|---|---|---|"]
        for r in labs:
            lines.append("| " + " | ".join(cell(v) for v in [r["source_id"], *(r["body"].get(k) for k in LAB_FIELDS)]) + " |")
        parts.append("\n".join(lines))
    return "\n\n---\n\n".join(parts) + ("\n" if parts else "")


def extract_case(bundle, alias, review=None, lookback_days=30):
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")
    review = review or {}
    bundle_hash = sha(bundle)
    if review and review.get("source_bundle_sha256") != bundle_hash:
        raise ValueError("Review configuration does not match source bundle hash")
    sources, duplicates = build_sources(bundle)
    by_id = {s["id"]: s for s in sources}
    episode, plans, global_issues = build_episode(sources, review, lookback_days)
    source_payloads = {}
    for s in sources:
        for ref in s["source_refs"]:
            if ref["source_key"]:
                source_payloads.setdefault((s["kind"], ref["source_key"]), set()).add(sha(s["body"]))
    conflicts = [{"kind": key[0], "source_key": key[1], "payload_hashes": sorted(values)}
                 for key, values in source_payloads.items() if len(values) > 1]
    if conflicts:
        global_issues.append("CONFLICTING_PAYLOADS_FOR_SAME_SOURCE")
    boundaries, imaging_candidates = [None] * 4, []
    if episode:
        imaging_candidates = sorted((s for s in sources if s["kind"] == "imaging" and s["time"]
                                    and episode["window_start"] <= s["time"].start < episode["cutoff"]
                                    and ABDOMEN_RE.search(s["title"]) and MODALITY_RE.search(s["title"])),
                                   key=lambda s: (s["time"].start, s["id"]))
        enhanced = [s for s in imaging_candidates if "增强" in s["title"]]
        for k, candidates in ((0, imaging_candidates), (1, enhanced)):
            if candidates:
                day_end = candidates[0]["time"].start.replace(hour=0, minute=0, second=0) + timedelta(days=1)
                boundaries[k] = min(day_end, episode["cutoff"])
        boundaries[2:] = [episode["cutoff"]] * 2
    template_spans = []
    for s in sources:
        if s["kind"] == "document" and s["title"] == "首次病程记录" and episode and episode_key(s) == episode["episode_key"]:
            span = history_candidate(s["body"])
            if span:
                template_spans.append({"source_id": s["id"], "start": span[0], "end": span[1],
                                       "source_text_sha256": sha(s["body"]), "stages": [1, 2],
                                       "accepted": False, "reviewer": "", "reason": "",
                                       "candidate_text": s["body"][span[0]:span[1]]})
    accepted = []
    for span in review.get("history_spans", []):
        if not span.get("accepted"):
            continue
        source = by_id.get(span.get("source_id"))
        start, end = span.get("start"), span.get("end")
        if (not source or source["kind"] != "document" or not episode
                or episode_key(source) != episode["episode_key"]
                or sha(source["body"]) != span.get("source_text_sha256")
                or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source["body"])
                or not span.get("reviewer") or not span.get("reason")
                or not isinstance(span.get("stages"), list) or not span["stages"]
                or any(type(k) is not int or k not in (1, 2) for k in span["stages"])):
            raise ValueError("Invalid or unbound accepted history span")
        text = source["body"][start:end]
        if PLAN_RE.search(text) or GENERIC_PLAN_RE.search(text):
            raise ValueError("Accepted history contains decision text")
        accepted.append((span, source))
    packets, decisions = [], []
    for stage, ((label, question), boundary) in enumerate(zip(QUESTIONS, boundaries), 1):
        records, issues = [], list(global_issues)
        if boundary is None:
            issues.append("NO_DISCOVERY_IMAGING" if stage == 1 else "NO_ENHANCED_STAGING_IMAGING" if stage == 2 else "NO_PREPLAN_BOUNDARY")
        if boundaries[0] and boundaries[0] == boundaries[1]:
            issues.append("DISCOVERY_STAGING_ROUNDS_OVERLAP")
        for s in sources:
            reason, span = None, None
            t, raw, kind = s["time"], s["raw"], s["kind"]
            if not boundary:
                reason = "NO_STAGE_BOUNDARY"
            elif kind == "document" and episode_key(s) != episode["episode_key"]:
                reason = "OTHER_ADMISSION"
            elif kind == "document" and (s["id"] == episode["selected_plan_source_id"] or "术前" in s["title"] or is_postoperative(s)):
                reason = "PLAN_OR_OPERATIVE_OUTCOME_DOCUMENT"
            elif kind == "document" and not NOTE_TITLE_RE.search(s["title"]):
                reason = "OUTSIDE_CLINICAL_NOTE_TEMPLATES"
            elif not t:
                reason = s["time_issue"] or "RESULT_TIME_MISSING_OR_INVALID"
                issues.append(reason)
            elif t.start < episode["window_start"]:
                reason = "OUTSIDE_LOOKBACK_WINDOW"
            elif t.end > boundary:
                reason = "NOT_FULLY_BEFORE_BOUNDARY"
            elif kind == "imaging" and not s["body"]:
                reason = "IMAGING_REPORT_TEXT_MISSING"
                issues.append(reason)
            elif kind == "pathology" and not DIAGNOSTIC_PATH_RE.search(str(raw.get("specimen_type", "")) + str(raw.get("specimen_name", ""))):
                reason = "PATHOLOGY_TYPE_REQUIRES_REVIEW"
                issues.append(reason)
            if reason is None and kind == "document":
                end = note_prefix(s["body"], s["title"])
                span = [0, end]
                text = s["body"][:end]
                if len(text.strip()) < 30:
                    reason = "NO_SAFE_NOTE_PREFIX"
                elif PLAN_RE.search(text) or GENERIC_PLAN_RE.search(text):
                    reason = "DECISION_TEXT_REMAINS"
                    issues.append(reason)
                elif future_dates(text, boundary):
                    reason = "NOTE_MENTIONS_LATER_DATE"
                    issues.append(reason)
                elif end < len(s["body"].rstrip()):
                    issues.append("NOTE_TAIL_REMOVED_REVIEW_OMISSIONS")
            decision = {"stage": stage, "source_id": s["id"], "disposition": "excluded" if reason else "included", "reason": reason}
            if span:
                decision["span"] = span
            decisions.append(decision)
            if reason:
                continue
            body = s["body"][span[0]:span[1]] if span else s["body"]
            labels = {"document": "文书正文完成时间", "imaging": "检查时间", "laboratory": "报告时间", "pathology": "病理报告日期"}
            records.append({"source_id": s["id"], "kind": kind, "title": s["title"], "body": body,
                            "display_time": t.raw, "time_label": labels[kind], "body_sha256": sha(body)})
            if kind == "imaging":
                issues.append("IMAGING_AVAILABILITY_USES_EXAM_PROXY")
            if t.precision == "date":
                issues.append("DATE_ONLY_RESULT_CONSERVATIVE_INTERVAL")
        for span, s in accepted:
            if stage not in span["stages"] or not boundary:
                continue
            text = s["body"][span["start"]:span["end"]]
            if future_dates(text, boundary):
                raise ValueError("Accepted history mentions a date beyond stage boundary")
            if any(r["source_id"] == s["id"] for r in records):
                # Already visible in its original note: don't duplicate the same history.
                continue
            records.append({"source_id": s["id"] + "-H", "kind": "document", "title": "既往就诊病史原文节选",
                            "body": text, "display_time": None, "time_label": "独立既往记录时间",
                            "body_sha256": sha(text)})
            decisions.append({"stage": stage, "source_id": s["id"], "rendered_source_id": s["id"] + "-H",
                              "disposition": "included_reviewed_history", "span": [span["start"], span["end"]],
                              "reviewer": span["reviewer"], "reason": span["reason"]})
            issues.append("REVIEWED_RETROSPECTIVE_HISTORY_RECONSTRUCTION")
        records.sort(key=lambda r: (r["kind"] == "laboratory", r["display_time"] or "", r["source_id"]))
        if stage in (1, 2) and boundary and not any(
            r["kind"] == "imaging" and ABDOMEN_RE.search(r["title"]) and MODALITY_RE.search(r["title"])
            and (stage == 1 or "增强" in r["title"]) for r in records
        ):
            issues.append("ANCHOR_REPORT_UNAVAILABLE")
        if stage in (3, 4) and boundary and not any(r["kind"] == "imaging" for r in records):
            issues.append("NO_STRUCTURED_IMAGING_BEFORE_PLAN")
        context = render_context(records)
        usable = boundary is not None and bool(records) and "ANCHOR_REPORT_UNAVAILABLE" not in issues
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "问题：" + question + "\n\n患者资料：\n\n" + context}] if usable else None
        packets.append({"stage": stage, "label": label, "question": question,
                        "status": "DRAFT_REVIEW_REQUIRED" if usable else "UNRESOLVED",
                        "boundary_exclusive": iso(boundary), "records": records,
                        "counts": dict(Counter(r["kind"] for r in records)), "issues": sorted(set(issues)),
                        "context": context, "context_sha256": sha(context), "messages": messages,
                        "messages_sha256": sha(messages) if messages else None})
    assert packets[2]["context_sha256"] == packets[3]["context_sha256"]
    inventory = []
    for s in sources:
        t = s["time"]
        inventory.append({"source_id": s["id"], "kind": s["kind"], "title": s["title"], "source_refs": s["source_refs"],
                          "source_body_sha256": sha(s["body"]), "source_archival_create_time": s["raw"].get("create_time"),
                          "time_raw": t.raw if t else None, "time_start": iso(t.start) if t else None,
                          "time_end_exclusive": iso(t.end) if t else None, "time_precision": t.precision if t else None,
                          "time_issue": s["time_issue"],
                          "original_result_time": s["raw"].get({"imaging": "exam_datetime", "laboratory": "report_time", "pathology": "report_date"}.get(s["kind"]))})
    plan_audit = [{k: iso(v.start) if k == "time" and v else v for k, v in p.items() if k != "source"} for p in plans]
    episode_audit = {k: iso(v) if isinstance(v, datetime) else v for k, v in episode.items()} if episode else None
    audit = {"version": VERSION, "case_alias": alias, "source_bundle_sha256": bundle_hash, "model_calls": 0,
             "review_config_sha256": sha(review), "episode": episode_audit, "plan_candidates": plan_audit,
             "global_issues": global_issues, "source_inventory": inventory, "source_decisions": decisions,
             "duplicates": duplicates, "source_payload_conflicts": conflicts,
             "reviewed_history_spans": [s for s, _ in accepted]}
    template = {"source_bundle_sha256": bundle_hash, "plan_source_id": None, "history_spans": template_spans}
    return {"version": VERSION, "case_alias": alias, "packets": packets}, audit, template


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def write_case(output, result, audit, template):
    directory = output / result["case_alias"]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    save(directory / "packets.json", result)
    save(directory / "audit.json", audit)
    save(directory / "review_template.json", {result["case_alias"]: template})
    lines = [f"# {result['case_alias']} 四阶段候选资料包", "", "仅本地规则提取，未调用模型；候选可构建不等于已核实临床准确。", "",
             "| 阶段 | 排他边界 | 文书 | 影像 | 检验条目 | 病理 | 状态 |", "|---|---|---|---|---|---|---|"]
    for p in result["packets"]:
        stem = f"{p['stage']:02d}"
        save(directory / f"{stem}_context.md", p["context"] if p["messages"] else "该阶段未可靠定位，不能作为模型输入。\n")
        message_path = directory / f"{stem}_messages.json"
        if p["messages"]:
            save(message_path, p["messages"])
        elif message_path.exists():
            message_path.unlink()
        c = p["counts"]
        lines.append(f"| {p['stage']} {p['label']} | {p['boundary_exclusive'] or '未定位'} | {c.get('document', 0)} | {c.get('imaging', 0)} | {c.get('laboratory', 0)} | {c.get('pathology', 0)} | {p['status']} |")
    lines += ["", "第三、四阶段 context 完全相同；messages 问题不同。前两阶段允许重叠，也可能无法定位。", "",
              "## 待审问题", ""]
    lines.extend(f"- 阶段 {p['stage']}：" + ", ".join(p["issues"]) for p in result["packets"])
    lines += ["", "## 回述病史候选（尚未自动加入早期资料）", ""]
    for s in template["history_spans"]:
        lines += [f"来源 {s['source_id']}，字符范围 [{s['start']}, {s['end']})", "", s["candidate_text"], ""]
    save(directory / "README.md", "\n".join(lines) + "\n")


def protected_output(path):
    resolved = Path(path).resolve()
    if not any(resolved.is_relative_to((ROOT / name).resolve()) for name in ("private", "outputs")):
        raise ValueError("Patient outputs must be inside this project's private/ or outputs/")
    return resolved


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--aliases", help="Source line aliases, e.g. S001,S002")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--reviews", type=Path)
    args = parser.parse_args()
    if args.lookback_days <= 0 or (args.limit is not None and args.limit <= 0):
        parser.error("lookback-days and limit must be positive")
    try:
        output = protected_output(args.output)
        if args.input.resolve().is_relative_to(output):
            raise ValueError("Input cannot be inside output directory")
        reviews = json.loads(args.reviews.read_text()) if args.reviews else {}
        selected = set(args.aliases.split(",")) if args.aliases else None
        if selected and any(not re.fullmatch(r"S\d{3,}", a) for a in selected):
            raise ValueError("Invalid source line alias")
        summaries, input_hash = [], hashlib.sha256()
        with args.input.open("rb") as f:
            for index, raw_line in enumerate(f, 1):
                input_hash.update(raw_line)
                if args.limit is not None and index > args.limit:
                    continue
                alias = f"S{index:03d}"
                if selected and alias not in selected:
                    continue
                bundle = json.loads(raw_line)
                result, audit, template = extract_case(bundle, alias, reviews.get(alias), args.lookback_days)
                audit["input_line"] = index
                write_case(output, result, audit, template)
                ps = result["packets"]
                summaries.append({"case_alias": alias, "resolved_slots": sum(p["messages"] is not None for p in ps),
                                  "distinct_resolved_contexts": len({p["context_sha256"] for p in ps if p["messages"]}),
                                  "early_rounds_overlap": bool(ps[0]["boundary_exclusive"] and ps[0]["boundary_exclusive"] == ps[1]["boundary_exclusive"]),
                                  "stage_counts": [p["counts"] for p in ps], "boundaries": [p["boundary_exclusive"] for p in ps],
                                  "global_issues": audit["global_issues"], "review_issues": sorted({x for p in ps for x in p["issues"]})})
        if selected and selected - {s["case_alias"] for s in summaries}:
            raise ValueError("Requested aliases absent from selected source lines")
        if not summaries:
            raise ValueError("No cases selected")
        aggregate = {"version": VERSION, "model_calls": 0, "input_sha256": input_hash.hexdigest(),
                     "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                     "system_prompt_sha256": sha(SYSTEM_PROMPT), "reviews_sha256": sha(reviews),
                     "lookback_days": args.lookback_days, "cases": len(summaries),
                     "resolved_slots_distribution": dict(Counter(s["resolved_slots"] for s in summaries)),
                     "distinct_contexts_distribution": dict(Counter(s["distinct_resolved_contexts"] for s in summaries)),
                     "overlapping_early_rounds": sum(s["early_rounds_overlap"] for s in summaries),
                     "case_summaries": summaries}
        save(output / "summary.json", aggregate)
        import csv
        import io
        table = io.StringIO()
        writer = csv.writer(table)
        writer.writerow(["case_alias", "resolved_slots", "early_rounds_overlap", *[f"stage_{k}_{field}" for k in range(1, 5) for field in ("documents", "imaging", "labs")], "global_issues"])
        for s in summaries:
            writer.writerow([s["case_alias"], s["resolved_slots"], s["early_rounds_overlap"],
                             *[c.get(kind, 0) for c in s["stage_counts"] for kind in ("document", "imaging", "laboratory")], ";".join(s["global_issues"])])
        save(output / "summary.csv", table.getvalue())
        lines = ["# 四阶段规则提取结果", "", "这是结构候选统计，不是临床准确率。所有有效槽仍需资料审阅；无模型调用。", "",
                 f"病例：{len(summaries)}；四槽均可生成候选：{sum(s['resolved_slots'] == 4 for s in summaries)}；前两轮重叠：{aggregate['overlapping_early_rounds']}。", "",
                 "| 病例 | 可构建槽 | 前两轮重叠 | 影像数 1/2/3/4 | 检验数 1/2/3/4 | episode 问题 |", "|---|---|---|---|---|---|"]
        for s in summaries:
            lines.append(f"| [{s['case_alias']}]({s['case_alias']}/README.md) | {s['resolved_slots']}/4 | {s['early_rounds_overlap']} | "
                         + "/".join(str(c.get("imaging", 0)) for c in s["stage_counts"]) + " | "
                         + "/".join(str(c.get("laboratory", 0)) for c in s["stage_counts"]) + " | " + ", ".join(s["global_issues"]) + " |")
        save(output / "REPORT.md", "\n".join(lines) + "\n")
        print(f"Extracted {len(summaries)} local cases; model calls: 0.")
        print(output)
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        # Provider secrets and raw patient contents never appear in CLI errors.
        print("Extraction/configuration error:", str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
