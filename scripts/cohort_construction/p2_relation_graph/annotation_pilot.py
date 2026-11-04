"""Prepare independent full-document local reconstruction tasks, never auto-gold."""
import argparse
from collections import defaultdict
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import random

from scripts.cohort_construction.p2_fhir_replay.core import instant
from .core import SourceStore


def sha(text): return hashlib.sha256(text.encode()).hexdigest()


def prepare(notes, units, output, *, seed=20261023, count=6):
    if count < 1: raise ValueError("positive case count required")
    store = SourceStore(notes)
    for unit in units: store.verify_unit(unit)
    rng = random.Random(seed); by_patient = defaultdict(list)
    for note in notes: by_patient[note["patient_uid"]].append(note)
    hit = {(u["patient_uid"], u["source_id"]) for u in units}
    buckets = [
        ("random_full_source", notes, 2),
        ("zero_hit_full_source", [n for n in notes if (n["patient_uid"], n["record_key"]) not in hit], 2),
        ("feedback_cue_challenge", [store.notes[u["patient_uid"],u["source_id"]] for u in units if u["feedback_cues"]], 1),
        ("multiple_action_challenge", [store.notes[u["patient_uid"],u["source_id"]] for u in units if len({a['concept'] for a in u['action_mentions']}) > 1], 1),
    ]
    selected, seen = [], set()
    def sample(label, rows, n):
        groups = defaultdict(dict)
        for row in rows: groups[row["patient_uid"]][row["record_key"]] = row
        patients = sorted(set(groups)-seen); rng.shuffle(patients)
        for patient in patients[:min(n, count-len(selected))]:
            options = [groups[patient][k] for k in sorted(groups[patient])]
            anchor = rng.choice(options)
            selected.append((label, anchor)); seen.add(patient)
    for label, rows, n in buckets: sample(label, rows, n)
    if len(selected) < count: sample("random_fill", notes, count-len(selected))
    output.mkdir(parents=True, exist_ok=False)
    for role in ["annotator_A", "annotator_B", "researcher_only"]: (output/role).mkdir(mode=0o700)
    manifest = {"version": "reconstruction-pilot-v0.1", "seed": seed, "development_only": True,
                "requested_cases": count, "cases": [], "human_gold_created": False,
                "clinical_blind_packets_created": False,
                "sampling": "patient-uniform within predeclared development/challenge strata; no population accuracy estimate"}
    for i, (stratum, anchor) in enumerate(selected, 1):
        case_id = f"LOCAL-{i:02d}"; patient = anchor["patient_uid"]
        at = instant(anchor.get("body_time")) or instant(anchor.get("system_time"))
        if at:
            low, high = at-timedelta(days=3), at+timedelta(days=3)
            chosen = [n for n in by_patient[patient] if
                      (t := (instant(n.get("body_time")) or instant(n.get("system_time")))) is None or low <= t <= high]
            scope_description = "All delivered source documents within +/-3 proxy-clock days of anchor, plus all patient documents with unresolved precise time. Not an episode definition."
        else:
            chosen = by_patient[patient]
            scope_description = "Anchor has unresolved precise time; all delivered patient source documents included. Not an episode definition."
        chosen.sort(key=lambda n: (n.get("body_time") or n.get("system_time") or "", n["record_key"]))
        windows = [{"patient_uid": patient, "source_id": n["record_key"], "source_sha256": sha(n["text"]),
                    "start": 0, "end": len(n["text"])} for n in chosen if n["text"]]
        documents = [{"patient_uid": patient, "source_id": n["record_key"], "source_sha256": sha(n["text"]),
                      "document_type": n["document_type"], "body_time": n.get("body_time"),
                      "recorded_time": n.get("system_time"), "text": n["text"]} for n in chosen]
        bundle = {"case_id": case_id, "scope_description": scope_description, "documents": documents,
                  "instruction": "Read all supplied original documents independently. Label all target judgments, evidence, actions and feedback; may mark uncertainty. No algorithm outputs supplied. This is reconstruction annotation, not clinical acceptable-action Reference."}
        template = {"annotation_version": "reconstruction-gold-v0.1", "annotation_status": "INDEPENDENT_DRAFT",
                    "origin": "independent_human", "annotator_ids": [], "complete_within_declared_scope": False,
                    "scope": {"kind": "source_windows", "windows": windows}, "scope_description": scope_description,
                    "mentions": [], "mention_to_event": {}, "relations": [], "atomicity_notes": [],
                    "outside_scope_reference_requests": []}
        markdown = [f"# {case_id} 独立原文重建标注\n", scope_description,
                    "\n本材料可含后续轨迹，仅用于重建标注；不能用于临床动作盲评。\n"]
        for n in documents:
            markdown += [f"\n## {n['source_id']} | {n['document_type']}\n",
                         f"正文时间 {n['body_time']}；记录时间 {n['recorded_time']}；SHA256 {n['source_sha256']}\n", n["text"]]
        for role in ["annotator_A", "annotator_B"]:
            dest = output/role/case_id; dest.mkdir(mode=0o700)
            for name, value in [("sources.json", bundle), ("annotation.json", template)]:
                (dest/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n")
            (dest/"原文材料.md").write_text("\n".join(markdown)+"\n")
        manifest["cases"].append({"case_id": case_id, "patient_uid": patient, "anchor_source": anchor["record_key"],
            "stratum": stratum, "source_documents": len(documents),
            "source_bundle_sha256": sha(json.dumps(bundle, ensure_ascii=False, indent=2)+"\n"),
            "annotation_status": "AWAITING_TWO_INDEPENDENT_ANNOTATORS"})
    (output/"researcher_only/selection_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
    return {"prepared_cases": len(selected), "source_documents": sum(c['source_documents'] for c in manifest['cases']),
            "human_gold_created": False, "clinical_blind_packets_created": False, "development_only": True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sources", required=True, type=Path); p.add_argument("--units", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path); p.add_argument("--count", type=int, default=6)
    args = p.parse_args(); repo = Path(__file__).resolve().parents[3]
    if not any(args.output.resolve().is_relative_to(repo/d) for d in ["private", "outputs"]):
        p.error("annotation material must remain in private/ or outputs/")
    os.umask(0o077)
    load = lambda path: [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    print(json.dumps(prepare(load(args.sources), load(args.units), args.output, count=args.count), ensure_ascii=False))


if __name__ == "__main__": main()
