"""Phase A: bind source review, freeze four checkpoints, and replay independent tests."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from .episode_extract import extract_episode_case
from .evaluation.client import ModelSettings
from .evaluation.runner import code_manifest, run_evaluation, selected_packets
from .four_stage import protected_output, save, sha

VERSION = "phase-a-dataset-v1"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def now():
    return datetime.now(timezone.utc).isoformat()


def historical_review_record(review):
    """Recover only recorded facts; never invent API calls, a transcript, or a model ID."""
    if "Codex" not in review["reviewer"]:
        raise ValueError("Historical importer requires a recorded Codex reviewer")
    return {
        "version": "phase-a-source-review-v1", "reviewer_kind": "codex_interactive",
        "reviewer": review["reviewer"], "source_bundle_sha256": review["source_bundle_sha256"],
        "review_manifest_sha256": sha(review), "reviewed_at": None, "model_id": None,
        "execution": "Codex交互式逐原文探索审阅，结论写入review_manifest；提取器自身零模型调用。",
        "record_origin": "Recovered from historical review_manifest; original chat not included",
        "transcript_available": False, "independent_clinician_approved": False,
        "checks": ["本院/外院归属及接收边界", "episode入口与四问锚点",
                   "整篇文书完成时间、未来日期与版本", "独立决策或既有计划复核"],
        "limitations": ["历史对话、精确模型身份和审阅时间未随样本保存，不作推定",
                        "检查时间代理及来源完整性缺口仍按配置和audit保留"],
    }


def validate_review_record(record, source, review):
    if (record.get("version") != "phase-a-source-review-v1"
            or record.get("reviewer_kind") not in ("codex_interactive", "api_llm", "human", "fictional")
            or record.get("source_bundle_sha256") != sha(source)
            or record.get("review_manifest_sha256") != sha(review)
            or record.get("reviewer") != review["reviewer"]
            or not record.get("execution") or not record.get("checks")
            or record.get("independent_clinician_approved") is not False):
        raise ValueError("Source review record must bind reviewer, source and configuration; exploratory only")
    if record["reviewer_kind"] == "api_llm":
        if not record.get("model_id"):
            raise ValueError("API review must record the model")
        for name in ("request_artifact", "response_artifact"):
            artifact = record.get(name, {})
            if (not isinstance(artifact, dict) or not artifact.get("payload")
                    or artifact.get("sha256") != sha(artifact["payload"])):
                raise ValueError("API review must embed hashed request/response artifacts")


def new_dataset(output):
    directory = protected_output(output)
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    return directory, {"version": VERSION, "created_at": now(), "cases": [],
                       "clinical_gold_loaded": False, "source_review_is_exploratory": True,
                       "code": code_manifest()}


def freeze_case(directory, alias, source, review, record, legacy_packets=None):
    if not re.fullmatch(r"S\d{3,}", alias):
        raise ValueError("Invalid case alias")
    episode = review.get("episode", {}).get("episode_id", "E001")
    if not re.fullmatch(r"E\d{3,}", episode):
        raise ValueError("Invalid episode ID")
    key = alias + "_" + episode
    validate_review_record(record, source, review)
    packets, audit, timeline = extract_episode_case(source, alias, review)
    if legacy_packets is not None:
        # Rebuild visibility; stored old system messages may use a different prompt revision.
        for old, new in zip(legacy_packets["packets"], packets["packets"], strict=True):
            for field in ("stage", "question", "checkpoint", "status", "records", "context_sha256", "issues"):
                if old.get(field) != new.get(field):
                    raise ValueError(f"Historical input is not reproducible: {key}, stage {new['stage']}, {field}")
    folder = directory / "cases" / key
    folder.mkdir(parents=True, mode=0o700, exist_ok=False)
    stages = [p["stage"] for p in packets["packets"] if p["status"] == "DRAFT_REVIEW_REQUIRED"]
    if stages:
        selected_packets(packets, stages, True)
    files = {
        "source_bundle.json": source, "review_manifest.json": review,
        "source_review_record.json": record, "packets.json": packets,
        "audit.json": audit, "timeline.json": timeline,
    }
    for p in packets["packets"]:
        files[f"{p['stage']:02d}_context.md"] = p["context"]
        if p["messages"]:
            files[f"{p['stage']:02d}_messages.json"] = p["messages"]
    for name, value in files.items():
        save(folder / name, value)
    samples = [{"sample_id": f"{key}_Q{p['stage']}", "stage": p["stage"],
                "status": p["status"], "checkpoint": p["checkpoint"], "issues": p["issues"],
                "selection_reason": review["checkpoints"][p["stage"] - 1]["reason"],
                "context_sha256": p["context_sha256"], "question_sha256": sha(p["question"]),
                "visible_source_ids": [r["source_id"] for r in p["records"]],
                "source_review_record_sha256": sha(record)} for p in packets["packets"]]
    save(folder / "samples.json", samples)
    return {"key": key, "alias": alias, "stages": stages,
            "packet_path": f"cases/{key}/packets.json", "samples": samples,
            "files_sha256": {p.name: sha(p.read_text(encoding="utf-8")) for p in sorted(folder.iterdir())}}


def finish_dataset(directory, manifest):
    manifest.update(episodes=len(manifest["cases"]), slots=len(manifest["cases"]) * 4,
                    runnable_samples=sum(len(c["stages"]) for c in manifest["cases"]))
    manifest["unresolved_samples"] = manifest["slots"] - manifest["runnable_samples"]
    save(directory / "dataset.json", manifest)
    verify_dataset(directory)
    return manifest


def verify_dataset(directory):
    directory = Path(directory).resolve()
    manifest = read(directory / "dataset.json")
    if manifest.get("version") != VERSION or not manifest.get("cases"):
        raise ValueError("Expected a frozen Phase A dataset")
    keys = set()
    for case in manifest["cases"]:
        key = case["key"]
        if not re.fullmatch(r"S\d{3,}_E\d{3,}", key) or key in keys:
            raise ValueError("Invalid or duplicate episode key")
        keys.add(key)
        if case["packet_path"] != f"cases/{key}/packets.json":
            raise ValueError("Invalid dataset-relative packet path")
        folder = directory / "cases" / key
        for name, digest in case["files_sha256"].items():
            if Path(name).name != name or sha((folder / name).read_text(encoding="utf-8")) != digest:
                raise ValueError("Frozen dataset artifact changed")
        source, review, record = (read(folder / n) for n in
                                  ("source_bundle.json", "review_manifest.json", "source_review_record.json"))
        validate_review_record(record, source, review)
        rebuilt, _, _ = extract_episode_case(source, case["alias"], review)
        saved = read(folder / "packets.json")
        if saved != rebuilt:
            raise ValueError("Frozen packets differ from source and review reconstruction")
        expected_stages = [p["stage"] for p in saved["packets"] if p["status"] == "DRAFT_REVIEW_REQUIRED"]
        if case["stages"] != expected_stages or case["samples"] != read(folder / "samples.json"):
            raise ValueError("Dataset index differs from frozen samples")
        for sample, packet in zip(case["samples"], saved["packets"], strict=True):
            if (sample["sample_id"] != f"{key}_Q{packet['stage']}"
                    or sample["context_sha256"] != packet["context_sha256"]
                    or sample["source_review_record_sha256"] != sha(record)):
                raise ValueError("Sample lineage mismatch")
        if expected_stages:
            selected_packets(saved, expected_stages, True)
    if (manifest["episodes"] != len(keys) or manifest["slots"] != len(keys) * 4
            or manifest["runnable_samples"] != sum(len(c["stages"]) for c in manifest["cases"])
            or manifest["unresolved_samples"] != manifest["slots"] - manifest["runnable_samples"]):
        raise ValueError("Dataset counts do not match frozen cases")
    return manifest


def run_dataset(directory, output, settings, *, execute=False, repeats=1, cases=None, client=None, progress=None):
    # Verify the ENTIRE dataset before creating a client or making any paid call.
    directory = Path(directory).resolve()
    dataset = verify_dataset(directory)
    chosen = dataset["cases"]
    if cases:
        if set(cases) - {c["key"] for c in chosen}:
            raise ValueError("Unknown selected episode")
        chosen = [c for c in chosen if c["key"] in cases]
    output = protected_output(output)
    batch = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8])
    batch.mkdir(parents=True, mode=0o700)
    manifest = {"dataset_sha256": sha(dataset), "started_at": now(), "status": "RUNNING",
                "execute": execute, "repeats": repeats, "settings": settings.public_dict(), "cases": []}
    save(batch / "batch_manifest.json", manifest)
    try:
        for case in chosen:
            if not case["stages"]:
                manifest["cases"].append({"key": case["key"], "status": "NO_RUNNABLE_CHECKPOINT"})
            else:
                run, summary = run_evaluation(read(directory / case["packet_path"]), batch / case["key"],
                                              settings, stages=case["stages"], repeats=repeats,
                                              allow_draft=True, execute=execute, client=client,
                                              progress=(lambda s, r, status: progress(case["key"], s, r, status))
                                              if progress else None)
                lineage = {"dataset_sha256": sha(dataset), "episode": case["key"],
                           "sample_ids": [f"{case['key']}_Q{s}" for s in case["stages"]],
                           "source_review_record_sha256": case["samples"][0]["source_review_record_sha256"]}
                save(run / "sample_lineage.json", lineage)
                manifest["cases"].append({"key": case["key"], "run_path": str(run.relative_to(batch)),
                                          "summary": summary})
            save(batch / "batch_manifest.json", manifest)
        invalid = any(c.get("summary", {}).get("attempted_samples", 0) !=
                      c.get("summary", {}).get("valid_samples", 0) for c in manifest["cases"])
        manifest["status"] = "COMPLETED_WITH_INVALID_SAMPLES" if invalid else "COMPLETED"
    except BaseException:
        manifest["status"] = "INTERRUPTED_OR_FAILED"
        raise
    finally:
        manifest["finished_at"] = now()
        save(batch / "batch_manifest.json", manifest)
    return batch, manifest


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Freeze one reviewed source bundle")
    for name in ("source", "review", "review-record", "output"):
        prepare.add_argument("--" + name, type=Path, required=True)
    prepare.add_argument("--alias", required=True)
    importer = commands.add_parser("import-pilot", help="Freeze historical Codex-reviewed pilot inputs")
    importer.add_argument("--input-root", type=Path, required=True)
    importer.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--dataset", type=Path, required=True)
    run = commands.add_parser("run", help="Preview by default; --execute calls the specified model")
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--base-url", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--key-env", default="BOYU_API_KEY")
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--cases", nargs="+")
    run.add_argument("--execute", action="store_true")
    run.add_argument("--max-tokens", type=int, default=16000)
    run.add_argument("--temperature", type=float, default=1)
    run.add_argument("--timeout", type=float, default=600)
    run.add_argument("--retries", type=int, default=0)
    run.add_argument("--response-format", choices=("prompt_only", "json_object", "json_schema"), default="prompt_only")
    args = parser.parse_args()
    try:
        if args.command in ("prepare", "import-pilot"):
            directory, manifest = new_dataset(args.output)
            if args.command == "prepare":
                source, reviews, record = read(args.source), read(args.review), read(args.review_record)
                review = reviews if "contract_version" in reviews else reviews[args.alias]
                manifest["cases"].append(freeze_case(directory, args.alias, source, review, record))
            else:
                paths = sorted(args.input_root.glob("*/review_manifest.json"))
                if not paths:
                    raise ValueError("No historical review manifests found")
                for path in paths:
                    review = read(path)
                    packets = read(path.with_name("packets.json"))
                    source = read(path.with_name("source_bundle.json"))
                    manifest["cases"].append(freeze_case(directory, packets["case_alias"], source, review,
                                                         historical_review_record(review), packets))
            result = finish_dataset(directory, manifest)
            print(json.dumps({k: result[k] for k in ("episodes", "slots", "runnable_samples", "unresolved_samples")}))
            print(directory)
        elif args.command == "verify":
            result = verify_dataset(args.dataset)
            print(f"Verified {result['episodes']} episodes / {result['slots']} slots")
        else:
            settings = ModelSettings(args.base_url, args.model, args.key_env, args.temperature,
                                     args.max_tokens, args.timeout, args.retries, args.response_format)
            if not 1 <= args.repeats <= 20:
                raise ValueError("Repeats must be between 1 and 20")
            def progress(key, stage, repeat, status):
                print(json.dumps({"sample_id": f"{key}_Q{stage}", "repeat": repeat,
                                  "status": status}), flush=True)
            batch, result = run_dataset(args.dataset, args.output, settings, execute=args.execute,
                                        repeats=args.repeats, cases=args.cases, progress=progress)
            print(batch)
            return int(result["status"] == "COMPLETED_WITH_INVALID_SAMPLES")
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print("Phase A input/configuration error:", str(exc) if type(exc) is ValueError else type(exc).__name__)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
