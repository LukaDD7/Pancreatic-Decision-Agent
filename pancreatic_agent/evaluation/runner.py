"""Reusable fixed-context evaluation runner with exact requests and local audit."""

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

from ..episode_extract import VERSION, whole_context
from ..four_stage import protected_output, save, sha
from .client import ChatCompletionClient, TransportFailure
from .protocol import build_messages, protocol_manifest, validate_output


def selected_packets(bundle, stages, allow_draft):
    if bundle.get("version") != VERSION or not re.fullmatch(r"S\d{3,}", bundle.get("case_alias", "")):
        raise ValueError("Expected a HEWV v0.4 fixed-context packet bundle")
    packets = bundle.get("packets", [])
    if len(packets) != 4 or [p.get("stage") for p in packets] != [1, 2, 3, 4]:
        raise ValueError("Expected exactly four ordered question packets")
    if not stages or len(set(stages)) != len(stages) or any(s not in (1, 2, 3, 4) for s in stages):
        raise ValueError("Select unique stage numbers from 1 to 4")
    chosen = [packets[s - 1] for s in stages]
    for p in chosen:
        if p.get("status") == "UNRESOLVED" or not p.get("checkpoint") or not p.get("records"):
            raise ValueError("Unresolved checkpoint cannot be evaluated")
        if p.get("status") != "DRAFT_REVIEW_REQUIRED" or not allow_draft:
            raise ValueError("This protocol requires explicit exploratory draft use")
        if not isinstance(p.get("question"), str) or not p["question"].strip():
            raise ValueError("Question is missing")
        records = p["records"]
        ids = [r["source_id"] for r in records]
        if len(ids) != len(set(ids)):
            raise ValueError("Source IDs must identify individual whole records")
        if any(sha(r["body"]) != r.get("body_sha256") for r in records):
            raise ValueError("Whole-record payload hash mismatch")
        if p.get("context_sha256") != sha(p.get("context")) or p["context"] != whole_context(records):
            raise ValueError("Context must reproduce all supplied whole records")
    return chosen


def code_manifest():
    root = Path(__file__).resolve().parents[1]
    names = [*Path(__file__).parent.glob("*.py"), root / "episode_extract.py", root / "four_stage.py",
             root / "timeline_extract.py", root / "phase_a.py"]
    code = {str(p.relative_to(root)): sha(p.read_text(encoding="utf-8")) for p in names}
    try:
        repo = root.parent
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, stderr=subprocess.DEVNULL,
                                       timeout=10).decode().strip()
        dirty = bool(subprocess.check_output(["git", "--no-optional-locks", "status", "--porcelain"], cwd=repo,
                                            stderr=subprocess.DEVNULL, timeout=10))
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        head, dirty = None, None
    return {"code_sha256": code, "git_head": head, "working_tree_dirty": dirty}


def summarize(results, executed):
    attempted = [r for r in results if r["status"] != "PREVIEW_ONLY"]
    valid = [r for r in attempted if r["status"] == "VALID"]
    groups = []
    for stage in sorted({r["stage"] for r in results}):
        rows = [r for r in results if r["stage"] == stage]
        accepted = [r for r in rows if r["status"] == "VALID"]
        modes = Counter(r["decision"]["mode"] for r in accepted)
        actions = Counter(r["decision"]["primary_action"]["code"] for r in accepted)
        groups.append({"stage": stage, "requested_samples": len(rows), "valid_samples": len(accepted),
                       "mode_counts": dict(modes), "action_counts": dict(actions),
                       "mode_agreement_among_valid": max(modes.values()) / len(accepted) if len(accepted) >= 2 else None,
                       "action_agreement_among_valid": max(actions.values()) / len(accepted) if len(accepted) >= 2 else None})
    return {"execution_requested": executed, "samples": len(results), "attempted_samples": len(attempted),
            "api_attempts": sum(len(r.get("attempts", [])) for r in results),
            "valid_samples": len(valid), "validation_pass_rate": len(valid) / len(attempted) if attempted else None,
            "status_counts": dict(Counter(r["status"] for r in results)), "by_stage": groups,
            "clinical_accuracy": None, "clinical_action_utility": None,
            "interpretation": "格式/可见来源/动作一致性检查；不是临床正确率。重复一致性不等于正确或已校准可靠性。"}


def summary_markdown(summary, results):
    lines = ["# 独立 checkpoint 测试结果", "", summary["interpretation"], "",
             f"API 请求尝试：{summary['api_attempts']}；有效输出：{summary['valid_samples']}/{summary['attempted_samples']}。", "",
             "| 问题 | 重复 | 技术状态 | Mode | 首选动作 |", "|---|---|---|---|---|"]
    for row in results:
        d = row.get("decision") or {}
        lines.append(f"| {row['stage']} | {row['repeat']} | {row['status']} | {d.get('mode', '')} | {d.get('primary_action', {}).get('code', '')} |")
    for row in results:
        if row["status"] != "VALID":
            continue
        d = row["decision"]
        lines.extend(["", f"## 问题 {row['stage']}，重复 {row['repeat']}", "", d["clinical_assessment"], "",
                      "首选动作目的：" + d["primary_action"]["purpose"], "",
                      "证据：" + "；".join(e["source_id"] + "：" + e["interpretation"] for e in d["evidence"]), "",
                      "不确定性：" + "；".join(d["uncertainties"])])
    return "\n".join(lines) + "\n"


def run_evaluation(bundle, output, settings, *, stages=(1, 2, 3, 4), repeats=1,
                   allow_draft=False, execute=False, client=None, progress=None):
    """Each sample has two fresh messages; PAUSE never releases observations."""
    os.umask(0o077)
    packets = selected_packets(bundle, stages, allow_draft)
    if not isinstance(repeats, int) or not 1 <= repeats <= 20:
        raise ValueError("Repeats must be between 1 and 20")
    output = protected_output(Path(output))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    directory = output / bundle["case_alias"] / run_id
    directory.mkdir(parents=True, mode=0o700)
    (directory / "requests").mkdir(mode=0o700)
    (directory / "results").mkdir(mode=0o700)
    manifest = {**protocol_manifest(), **code_manifest(), "run_id": run_id, "case_alias": bundle["case_alias"],
                "input_packets_sha256": sha(bundle), "model_settings": settings.public_dict(),
                "stages": list(stages), "repeats": repeats, "execution_requested": execute,
                "allow_exploratory_draft": allow_draft, "cross_question_memory": False,
                "observation_replay": False, "clinical_gold_loaded": False,
                "packet_review": [{"stage": p["stage"], "status": p["status"], "issues": p["issues"],
                                   "checkpoint": p["checkpoint"], "context_sha256": p["context_sha256"],
                                   "question_sha256": sha(p["question"])} for p in packets], "status": "RUNNING"}
    save(directory / "manifest.json", manifest)
    owned_client, results = False, []
    try:
        if execute and client is None:
            client = ChatCompletionClient(settings)
            owned_client = True
        for p in packets:
            for repeat in range(1, repeats + 1):
                key = f"stage_{p['stage']:02d}_repeat_{repeat:02d}"
                messages = build_messages(p["question"], p["context"])
                request = {"messages": messages, **settings.request_parameters()}
                save(directory / "requests" / (key + ".json"), request)
                row = {"stage": p["stage"], "repeat": repeat, "request_sha256": sha(request),
                       "messages_sha256": sha(messages), "status": "PREVIEW_ONLY", "raw_output": None,
                       "decision": None, "candidate_output": None, "validation_errors": [], "attempts": [],
                       "metadata": None, "clinical_review": {"appropriateness": None, "preventability": None}}
                if execute:
                    def journal(attempts):
                        row.update(status="IN_FLIGHT", attempts=attempts)
                        save(directory / "results" / (key + ".json"), row)
                    try:
                        response = client.complete(messages, on_attempt=journal)
                        row.update(raw_output=response.text, metadata=response.metadata, attempts=response.attempts,
                                   finish_reason=response.finish_reason)
                        if response.metadata.get("refused"):
                            row.update(status="REFUSED", validation_errors=["MODEL_REFUSAL"])
                        elif not response.text:
                            row.update(status="EMPTY_RESPONSE", validation_errors=["NO_FINAL_TEXT"])
                        elif response.finish_reason != "stop":
                            row.update(status="INCOMPLETE_RESPONSE", validation_errors=["FINISH_REASON_NOT_STOP"])
                        else:
                            candidate, errors = validate_output(response.text, [r["source_id"] for r in p["records"]])
                            row.update(status="INVALID_OUTPUT" if errors else "VALID", validation_errors=errors)
                            row["candidate_output" if errors else "decision"] = candidate
                    except TransportFailure as exc:
                        row.update(status="TRANSPORT_FAILURE", attempts=exc.attempts,
                                   validation_errors=["TRANSPORT_FAILURE"])
                    except KeyboardInterrupt:
                        row.update(status="INTERRUPTED", validation_errors=["REQUEST_INTERRUPTED"])
                        save(directory / "results" / (key + ".json"), row)
                        results.append(row)
                        summary = summarize(results, execute)
                        save(directory / "summary.json", summary)
                        save(directory / "summary.md", summary_markdown(summary, results))
                        raise
                save(directory / "results" / (key + ".json"), row)
                results.append(row)
                summary = summarize(results, execute)
                save(directory / "summary.json", summary)
                save(directory / "summary.md", summary_markdown(summary, results))
                if progress:
                    progress(p["stage"], repeat, row["status"])
        manifest["status"] = "COMPLETED"
    except BaseException:
        manifest["status"] = "INTERRUPTED_OR_FAILED"
        raise
    finally:
        save(directory / "manifest.json", manifest)
        if owned_client:
            client.close()
    return directory, summary
