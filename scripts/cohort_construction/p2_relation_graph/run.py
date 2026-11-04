"""CLI: source-verified prepare run or explicitly executed model proposals."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from . import VERSION
from .demo import data
from .model import SemanticClient
from .pipeline import reconstruct


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2)+"\n")


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="use invented examples only")
    parser.add_argument("--units", type=Path)
    parser.add_argument("--sources", type=Path)
    parser.add_argument("--execute", action="store_true", help="send selected source context to configured Boyu endpoint")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--cache-mode", choices=["read_write", "read_only", "refresh", "bypass"], default="read_write")
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--max-units", type=int)
    parser.add_argument("--max-pairs", type=int)
    args = parser.parse_args(argv)
    if args.top_k < 1 or any(x is not None and x < 1 for x in [args.max_units, args.max_pairs]):
        parser.error("budgets must be positive")
    if args.demo and (args.units or args.sources):
        parser.error("demo cannot mix with patient source inputs")
    if not args.demo and not (args.units and args.sources):
        parser.error("provide both --units and --sources, or --demo")
    os.umask(0o077)
    # Protect all source-bearing outputs, including cached responses.
    repo = Path(__file__).resolve().parents[3]
    for path in [args.output, args.cache or args.output/"model_cache"]:
        if not any(path.resolve().is_relative_to(repo/p) for p in ["private", "outputs"]):
            parser.error("source-bearing outputs/cache must stay in ignored private/ or outputs/")
    args.output.mkdir(parents=True, exist_ok=False)
    code_paths = list(Path(__file__).parent.rglob("*.py")) + [
        repo/"scripts/cohort_construction/model_api.py", repo/"config/model_provider.json"]
    code = {str(p.relative_to(repo)): sha(p) for p in code_paths}
    inputs = {}
    if args.demo:
        notes, units = data()
    else:
        notes, units = read_rows(args.sources), read_rows(args.units)
        inputs = {str(p): sha(p) for p in [args.sources, args.units]}
    all_count = len(units)
    if args.max_units:
        units = units[:args.max_units]
    client = SemanticClient(args.cache or args.output/"model_cache", cache_mode=args.cache_mode) if args.execute else None
    graph = reconstruct(units, notes, client=client, top_k=args.top_k, max_pairs=args.max_pairs)
    graph["summary"]["unprocessed_input_units"] = all_count-len(units)
    graph["summary"]["synthetic_only"] = args.demo
    write(args.output/"graph.json", graph)
    write(args.output/"summary.json", graph["summary"])
    changed = [name for name, before in code.items() if sha(repo/name) != before]
    write(args.output/"manifest.json", {"version": VERSION, "inputs_sha256": inputs,
          "code_sha256": code, "graph_sha256": sha(args.output/"graph.json"),
          "top_k": args.top_k, "max_units": args.max_units, "max_pairs": args.max_pairs,
          "mode": graph["mode"], "model": None if client is None else client.settings.model,
          "cache_mode": args.cache_mode,
          "synthetic_only": args.demo, "baseline_extractor_modified": False,
          "code_changed_during_run": changed,
          "source_or_cohort_membership_changed": False,
          "calibration": "NOT_ESTABLISHED", "clinical_reference_generated": False})
    print(json.dumps(graph["summary"], ensure_ascii=False))
    # A failed unit/edge remains visible; nonzero exit prevents automation from
    # treating an incomplete semantic run as a successful unattended run.
    failed = any(x["status"] == "NORMALIZATION_FAILED_REVIEW" for x in graph["normalization_ledger"])
    failed |= any(any(i.startswith("MODEL_OR_GROUNDING_FAILURE") for i in e["issues"]) for e in graph["edges"])
    return 3 if changed else 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
