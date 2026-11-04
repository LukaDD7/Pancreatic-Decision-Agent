"""Fresh identical-request repeatability experiment; never a correctness gold."""
import argparse
from itertools import combinations
import json
import os
from pathlib import Path

from .core import (FIELDS, NORMALIZATION_SCHEMA, RELATION_SCHEMA, SourceStore,
                   assess_relation, digest, normalize)
from .demo import data
from .model import SemanticClient, mention_payload, public_view
from .pipeline import provisional_mentions


def demo_tasks():
    notes, units = data(); store = SourceStore(notes)
    views = [store.view(u["patient_uid"], u["source_id"], alias=f"S{i}") for i, u in enumerate(units)]
    ms = [m for m in provisional_mentions(units) if m["kind"] == "DecisionMention"]
    for m, action in zip(ms, ["PTCD", "ERCP"]):
        m["signature"]["intended_action"] = {"state": "stated", "value": action, "supports": [m["anchor"]]}
    pair = {"id": "synthetic-fixed-pair", "from": ms[0]["id"], "to": ms[1]["id"], "stage": "decision_relation"}
    return [
        {"id": "synthetic-normalization", "task": "normalize_mentions", "schema": NORMALIZATION_SCHEMA,
         "payload": {"unit_span": units[0]["span"], "unit_quote": units[0]["quote"],
                     "sources": [public_view(views[0])],
                     "instruction": "仅拆分unit_span内有原文依据的mention；周围文字只作语境。没有适合角色可返回空列表。"},
         "unit": units[0], "views": [views[0]]},
        {"id": "synthetic-fixed-relation", "task": "decision_relation", "schema": RELATION_SCHEMA,
         "payload": {"previous": mention_payload(ms[0]), "current": mention_payload(ms[1]),
                     "sources": [public_view(v) for v in views],
                     "instruction": "只判reaffirm/revise/supersede/unrelated/uncertain；独立事件未证实则uncertain。"},
         "pair": pair, "mentions": {m["id"]: m for m in ms}, "views": views}]


def agreement(records):
    out = []
    for task_id in sorted({r["task_id"] for r in records}):
        rows = [r for r in records if r["task_id"] == task_id]
        good = [r for r in rows if r["status"] == "VALID_GROUNDED"]
        pairs = list(combinations(good, 2)); scores = []; raw_labels = []; fields = {f: [] for f in FIELDS+['temporality']}
        for a, b in pairs:
            if a["task"] == "normalize_mentions":
                def keyed(row): return {(m["kind"], tuple(m["anchor"]["span"])): m for m in row["normalized"]}
                aa, bb = keyed(a), keyed(b); union = aa.keys() | bb.keys(); common = aa.keys() & bb.keys()
                scores.append(len(common)/len(union) if union else 1.0)
                for key in common:
                    for f in fields:
                        va = aa[key][f] if f == "temporality" else {k: aa[key]["signature"][f][k] for k in ['state','value']}
                        vb = bb[key][f] if f == "temporality" else {k: bb[key]["signature"][f][k] for k in ['state','value']}
                        fields[f].append(va == vb)
            else:
                scores.append(a["normalized"]["relation"] == b["normalized"]["relation"])
                raw_labels.append(a["normalized"]["proposed_relation"] == b["normalized"]["proposed_relation"])
        out.append({"task_id": task_id, "attempted_replicates": len(rows), "valid_grounded_replicates": len(good),
            "failed_replicates": len(rows)-len(good), "valid_pair_comparisons": len(pairs),
            "mention_jaccard_or_relation_label_agreement": sum(scores)/len(scores) if scores else None,
            "raw_relation_label_agreement": sum(raw_labels)/len(raw_labels) if raw_labels else None,
            "field_exact_agreement_on_shared_mentions": {f: sum(xs)/len(xs) if xs else None for f,xs in fields.items()},
            "fresh_requests": sum(not c.get("cached", False) for r in rows for c in r["receipts"]),
            "semantic_request_hashes": sorted({c["request_sha256"] for r in rows for c in r["receipts"]}),
            "interpretation": "Stability only; exact field wording is not semantic correctness. Failures remain in denominator report."})
    return out


def run(tasks, output, *, repeats=3, execute=False, client_factory=SemanticClient):
    if repeats < 2: raise ValueError("at least two independent replicates required")
    output.mkdir(parents=True, exist_ok=False)
    records = []
    for task in tasks:
        for rep in range(repeats):
            row = {"task_id": task["id"], "task": task["task"], "replicate_id": f"rep-{rep+1}",
                   "payload_hash": digest(task["payload"]), "status": "PREVIEW_ONLY", "receipts": []}
            if execute:
                client = client_factory(output/"model_records", cache_mode="bypass", replicate_id=row["replicate_id"], retries=0)
                try:
                    answer = client.ask(task["task"], task["payload"], task["schema"])
                    if task["task"] == "normalize_mentions":
                        row["normalized"] = normalize(task["unit"], answer, task["views"])
                    else:
                        row["normalized"] = assess_relation(task["pair"], answer, task["mentions"], task["views"])
                    row["status"] = "VALID_GROUNDED"
                except Exception as exc:
                    row["status"], row["error_type"] = "FAILED", type(exc).__name__
                row["receipts"] = client.receipts
            records.append(row)
            # Every independent attempt survives interruption; never rerun until valid.
            with (output/"replicates.jsonl").open("a") as f: f.write(json.dumps(row, ensure_ascii=False)+"\n")
    result = {"cache_mode": "bypass", "repeats": repeats, "task_count": len(tasks),
              "summary": agreement(records), "clinical_accuracy": None, "human_gold_created": False}
    (output/"summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--demo", action="store_true", required=True)
    p.add_argument("--repeats", type=int, default=3); p.add_argument("--execute", action="store_true")
    p.add_argument("--output", type=Path, required=True); args = p.parse_args()
    repo = Path(__file__).resolve().parents[3]
    if not any(args.output.resolve().is_relative_to(repo/d) for d in ["private", "outputs"]):
        p.error("repeat outputs must stay in private/ or outputs/")
    os.umask(0o077)
    result = run(demo_tasks(), args.output, repeats=args.repeats, execute=args.execute)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__": main()
