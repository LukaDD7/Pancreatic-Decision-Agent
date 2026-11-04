"""Replay frozen model proposals through legacy and final-graph assembly.

This is an offline structural migration comparison: it does not call a model,
read a clinical Reference, or establish clinical accuracy/improvement.  Frozen
input files remain read-only; every run requires a new output directory.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources
from scripts.cohort_construction.p2_auto_checkpoint.semantic import mask_text, patient_names
from scripts.cohort_construction.p2_auto_checkpoint_v04.engine import assemble_processes
from scripts.cohort_construction.p2_auto_checkpoint_v04.final_graph import audit_final_graph


VERSION = "offline-final-graph-replay-v1"
CASE_FILES = ("decision_events.json", "relations.json", "episodes.json", "summary.json",
              "source_fhir_r4_bundle.json", "source_import_receipt.json")
DERIVED_FIELDS = ("episode_id", "canonical_event_id", "status_before_semantic_identity_grouping")
PROJECTION_FIELDS = ("checkpoint_status",) + DERIVED_FIELDS


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read(path):
    return json.loads(Path(path).read_text())


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _file_record(path):
    path = Path(path)
    return dict(path=str(path.absolute()), resolved_path=str(path.resolve()), sha256=file_hash(path),
                bytes=path.stat().st_size)


def reset_assembly_fields(events):
    """Restore only documented identity-grouping changes; never guess a status."""
    reset = deepcopy(events)
    for event in reset:
        if event.get("final_graph_audit"):
            raise ValueError("INPUT_ALREADY_FINAL_GRAPH_AUDITED")
        if event.get("checkpoint_status") == "GROUPED_SAME_EVENT_MENTION":
            original = event.get("status_before_semantic_identity_grouping")
            if not original or not original.startswith("CANDIDATE_"):
                raise ValueError("GROUPED_EVENT_ORIGINAL_CANDIDATE_STATUS_MISSING:" + event["event_id"])
            event["checkpoint_status"] = original
        for field in DERIVED_FIELDS:
            event.pop(field, None)
    return reset


def _counts(events, episodes, relations):
    return dict(event_count=len(events),
                checkpoint_status_counts=dict(sorted(Counter(e["checkpoint_status"] for e in events).items())),
                candidate_checkpoint_count=sum(e["checkpoint_status"].startswith("CANDIDATE_") for e in events),
                candidate_episode_count=len(episodes), relation_count=len(relations),
                supported_relation_count=sum(r["status"] == "supported" for r in relations),
                supported_relation_counts=dict(sorted(Counter(r["relation"] for r in relations if r["status"] == "supported").items())))


def _relation_key(relation):
    return relation["relation"], relation["source_event_id"], relation["target_event_id"]


def _projection(event):
    return {field: event[field] for field in PROJECTION_FIELDS if field in event}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compare_frozen_replay(frozen_events, frozen_episodes, frozen_relations,
                          replay_events, replay_episodes, replay_relations):
    """Report old-assembly replay differences explicitly, never assume parity."""
    frozen_byid = {event["event_id"]: event for event in frozen_events}
    replay_byid = {event["event_id"]: event for event in replay_events}
    differences = []
    for eid in sorted(frozen_byid.keys() | replay_byid.keys()):
        before, after = frozen_byid.get(eid), replay_byid.get(eid)
        if before is None or after is None or _projection(before) != _projection(after):
            differences.append(dict(event_id=eid, frozen=_projection(before) if before else None,
                                    legacy_replay=_projection(after) if after else None))
    # Compare the full exported objects (including citation audit content).
    # Object order does not matter; order within each object remains checked.
    episode_equal = sorted(map(_json, frozen_episodes)) == sorted(map(_json, replay_episodes))
    relation_equal = sorted(map(_json, frozen_relations)) == sorted(map(_json, replay_relations))
    return dict(equal=not differences and episode_equal and relation_equal,
                event_assembly_projection_equal=not differences,
                full_episode_objects_equal=episode_equal, full_relation_objects_equal=relation_equal,
                event_assembly_differences=differences,
                frozen_counts=_counts(frozen_events, frozen_episodes, frozen_relations),
                legacy_replay_counts=_counts(replay_events, replay_episodes, replay_relations),
                interpretation="Any mismatch is a replay/provenance difference, not a clinical error.")


def compare_case(frozen_events, frozen_episodes, frozen_relations):
    """Run both assemblers on equivalent reset copies and retain all proposals."""
    reset = reset_assembly_fields(frozen_events)
    legacy = deepcopy(reset)
    legacy_episodes, legacy_relations = assemble_processes(legacy)
    audited, graph_audit = audit_final_graph(reset)
    preassembly = deepcopy(audited)
    strict_episodes, strict_relations = assemble_processes(audited, final_graph_validation=True)
    legacy_byid = {e["event_id"]: e for e in legacy}
    pre_byid = {e["event_id"]: e for e in preassembly}
    strict_byid = {e["event_id"]: e for e in audited}
    rows = []
    for original in frozen_events:
        eid = original["event_id"]; new = strict_byid[eid]
        audit = new["final_graph_audit"]
        rows.append(dict(event_id=eid, source_id=original.get("source_id"),
                         source_ids=original.get("source_ids", []),
                         frozen_checkpoint_status=original["checkpoint_status"],
                         legacy_replay_checkpoint_status=legacy_byid[eid]["checkpoint_status"],
                         strict_preassembly_checkpoint_status=pre_byid[eid]["checkpoint_status"],
                         strict_checkpoint_status=new["checkpoint_status"],
                         status_changed=legacy_byid[eid]["checkpoint_status"] != new["checkpoint_status"],
                         semantic_review_unchanged=original.get("semantic_review") == new.get("semantic_review"),
                         hard_reasons=audit["hard_reasons"], soft_reasons=audit["soft_reasons"],
                         dependencies=audit["dependencies"],
                         legacy_episode_id=legacy_byid[eid].get("episode_id"),
                         strict_episode_id=new.get("episode_id"),
                         legacy_canonical_event_id=legacy_byid[eid].get("canonical_event_id"),
                         strict_canonical_event_id=new.get("canonical_event_id")))
    if not all(row["semantic_review_unchanged"] for row in rows):
        raise AssertionError("OFFLINE_REPLAY_CHANGED_MODEL_SEMANTIC_PROPOSAL")
    old_links, new_links = defaultdict(list), defaultdict(list)
    for relation in legacy_relations:
        old_links[_relation_key(relation)].append(relation)
    for relation in strict_relations:
        new_links[_relation_key(relation)].append(relation)
    relation_rows = []
    for key in sorted(old_links.keys() | new_links.keys()):
        old = Counter(r["status"] for r in old_links[key]); new = Counter(r["status"] for r in new_links[key])
        lost_supported = max(0, old["supported"] - new["supported"])
        gained_uncertain = max(0, new["uncertain"] - old["uncertain"])
        relation_rows.append(dict(relation=key[0], source_event_id=key[1], target_event_id=key[2],
                                  legacy_status_counts=dict(old), strict_status_counts=dict(new),
                                  supported_to_uncertain_count=min(lost_supported, gained_uncertain),
                                  status_changed=old != new,
                                  final_graph_validations=[r.get("final_graph_validation") for r in new_links[key]]))
    transitions = Counter((row["legacy_replay_checkpoint_status"], row["strict_checkpoint_status"])
                          for row in rows if row["status_changed"])
    frozen_comparison = compare_frozen_replay(frozen_events, frozen_episodes, frozen_relations,
                                             legacy, legacy_episodes, legacy_relations)
    summary = dict(version=VERSION, comparison_kind="offline_program_structural_migration",
                   model_calls=0, reference_read=False, clinical_accuracy_measured=False,
                   clinical_error_count=None, improvement_proven=False,
                   frozen_legacy_replay_comparison=frozen_comparison,
                   legacy_counts=_counts(legacy, legacy_episodes, legacy_relations),
                   strict_counts=_counts(audited, strict_episodes, strict_relations),
                   status_changed_event_count=sum(row["status_changed"] for row in rows),
                   status_transitions=[dict(before=a, after=b, count=count) for (a,b),count in sorted(transitions.items())],
                   supported_to_uncertain_relation_count=sum(r["supported_to_uncertain_count"] for r in relation_rows),
                   checkpoint_demotion_count=graph_audit["demoted_checkpoint_count"],
                   semantic_review_unchanged=True,
                   dependency_reason_counts=dict(sorted(Counter(reason for row in rows for reason in row["hard_reasons"]).items())))
    return dict(reset_events=reset, legacy_events=legacy, legacy_episodes=legacy_episodes,
                legacy_relations=legacy_relations, strict_events=audited,
                strict_episodes=strict_episodes, strict_relations=strict_relations,
                graph_audit=graph_audit, event_comparison=rows, relation_comparison=relation_rows,
                summary=summary)


def _source_receipt(case_dir, frozen_events):
    """Read and verify the delivered source/hash records without altering them."""
    bundle_path = case_dir / "source_fhir_r4_bundle.json"
    sources, imported = import_sources(_read(bundle_path))
    byid = {source["source_id"]: source for source in sources}
    names = patient_names(sources)
    display_hashes = {source["source_id"]: hashlib.sha256(mask_text(source["text"], names).encode()).hexdigest()
                      for source in sources}
    if len(byid) != len(sources):
        raise ValueError("DUPLICATE_SOURCE_ID_IN_FROZEN_INPUT")
    original_receipt = _read(case_dir / "source_import_receipt.json")
    referenced_path = Path(original_receipt["input_path"])
    if not referenced_path.is_absolute():
        referenced_path = case_dir / referenced_path
    if referenced_path.exists():
        referenced_record = _file_record(referenced_path)
        referenced_record["recorded_sha256"] = original_receipt.get("sha256")
        referenced_record["recorded_sha256_matches"] = referenced_record["sha256"] == original_receipt.get("sha256")
    else:
        referenced_record = dict(path=str(referenced_path), exists=False,
                                 recorded_sha256=original_receipt.get("sha256"), recorded_sha256_matches=None)
    mismatches, legacy_display_hashes = [], []
    checked = 0
    def visit(value, path):
        nonlocal checked
        if isinstance(value, dict):
            if value.get("source_id") and value.get("source_sha256"):
                checked += 1
                source = byid.get(value["source_id"])
                if not source or source["source_sha256"] != value["source_sha256"]:
                    record = dict(json_path=path, source_id=value["source_id"],
                                  declared_sha256=value["source_sha256"],
                                  raw_source_sha256=source["source_sha256"] if source else None,
                                  display_source_sha256=display_hashes.get(value["source_id"]),
                                  citation_status=value.get("status"))
                    # Historical resolve_model_quotes mapped valid/ambiguous
                    # citation hashes back to raw, but retained the exact
                    # masked-display hash on failed lookups. Accept only this
                    # independently reproduced, explicitly invalid case.
                    if (source and value.get("status") in ("QUOTE_NOT_IN_SOURCE", "QUOTE_OUTSIDE_ALLOWED_SPANS") and
                            value["source_sha256"] == display_hashes[source["source_id"]]):
                        record["classification"] = "verified_legacy_invalid_citation_masked_display_hash"
                        record["hash_semantics"] = "display_lookup_hash"
                        legacy_display_hashes.append(record)
                    else:
                        mismatches.append(record)
            for key, child in value.items():
                visit(child, path + "/" + str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + "/" + str(index))
    visit(frozen_events, "/decision_events")
    action_checks = []
    for event in frozen_events:
        for index, citation in enumerate(event.get("action_citations", {}).get("valid", [])):
            source = byid.get(citation.get("source_id"))
            start, end = citation.get("start"), citation.get("end")
            in_range = (source is not None and type(start) is int and type(end) is int and
                        0 <= start < end <= len(source["text"]))
            verified = (in_range and source["text"][start:end] == citation.get("quote") and
                        source["source_sha256"] == citation.get("source_sha256"))
            action_checks.append(dict(event_id=event["event_id"], citation_index=index,
                                      source_id=citation.get("source_id"), start=start, end=end,
                                      declared_source_sha256=citation.get("source_sha256"),
                                      raw_source_sha256=source["source_sha256"] if source else None,
                                      raw_span_and_quote_verified=bool(verified)))
    # A recorded mismatch must stop migration, not merely become a reassuring
    # hash manifest attached to incomparable inputs.
    if mismatches:
        raise ValueError("FROZEN_EVENT_SOURCE_HASH_MISMATCH:" + str(len(mismatches)))
    if not all(check["raw_span_and_quote_verified"] for check in action_checks):
        raise ValueError("FROZEN_VALID_ACTION_RAW_SPAN_MISMATCH")
    if referenced_record.get("recorded_sha256_matches") is False:
        raise ValueError("FROZEN_IMPORT_RECEIPT_HASH_MISMATCH")
    return dict(delivered_source_file=_file_record(bundle_path), import_validation=imported,
                source_hashes=[dict(source_id=s["source_id"], source_sha256=s["source_sha256"],
                                    source_kind=s["kind"]) for s in sorted(sources, key=lambda item:item["source_id"])],
                event_source_hash_entries_checked=checked, event_source_hash_mismatches=mismatches,
                verified_legacy_invalid_citation_display_hashes=legacy_display_hashes,
                legacy_invalid_citation_display_hash_count=len(legacy_display_hashes),
                legacy_hash_interpretation=("Frozen invalid citation hashes are not rewritten. Only exact masked-display "
                    "SHA256 matches with known invalid citation status are classified separately; all valid action "
                    "citations are verified against raw source hashes, character offsets and raw quotes."),
                valid_action_citation_count=len(action_checks), valid_action_raw_span_checks=action_checks,
                recorded_import_receipt=original_receipt, recorded_original_input=referenced_record)


def _code_receipts():
    root = Path(__file__).resolve().parents[3]
    names = ("p2_auto_checkpoint_v06/offline.py", "p2_auto_checkpoint_v04/engine.py",
             "p2_auto_checkpoint_v04/final_graph.py", "p2_auto_checkpoint/fhir_io.py",
             "p2_auto_checkpoint/extract.py", "p2_auto_checkpoint/semantic.py", "p2_fhir_replay/core.py")
    return [_file_record(root / "scripts/cohort_construction" / name) for name in names]


def _markdown(report):
    lines = ["# 冻结LLM输出的离线结构迁移比较", "",
             "本次仅重新运行程序的最终依赖核验和Episode组装。模型原始提案未修改，未调用模型、未读取临床Reference。下列变化是程序结构信号，不能解释为临床错误人数或已证明的性能改善。", "",
             "|病例|旧程序候选Checkpoint|严格核验候选Checkpoint|旧程序Episode|严格核验Episode|状态变化对象|supported→uncertain关系|旧程序重放与冻结结果一致|",
             "|---|---:|---:|---:|---:|---:|---:|---|"]
    for case in report["cases"]:
        old, new = case["legacy_counts"], case["strict_counts"]
        lines.append("|{case_id}|{oc}|{nc}|{oe}|{ne}|{sc}|{rc}|{eq}|".format(
            case_id=case["case_id"], oc=old["candidate_checkpoint_count"], nc=new["candidate_checkpoint_count"],
            oe=old["candidate_episode_count"], ne=new["candidate_episode_count"], sc=case["status_changed_event_count"],
            rc=case["supported_to_uncertain_relation_count"],
            eq="是" if case["frozen_legacy_replay_comparison"]["equal"] else "否，详见逐例summary.json"))
    total = report["totals"]
    lines += ["", f"共{total['case_count']}例、{total['event_count']}个管理记录对象；候选Checkpoint由{total['legacy_candidate_checkpoint_count']}变为{total['strict_candidate_checkpoint_count']}，候选Episode由{total['legacy_candidate_episode_count']}变为{total['strict_candidate_episode_count']}。",
              "", f"{total['supported_to_uncertain_relation_count']}条关系由程序supported转为uncertain；{total['status_changed_event_count']}个对象的最终状态发生变化。", "",
              "每个病例目录保留原始模型提案、副本重放结果、逐对象原因和关系ID解析依据。input_manifest.json记录输入及代码hash；summary.json记录旧程序重放是否复现冻结结果。", ""]
    lines += ["历史无效引文的source_sha256有一部分保存的是脱敏显示文本hash。source_hash_receipt.json逐条区分并复算其显示文本hash与原文hash，未改写历史字段；所有有效动作引文另核对了原文字符区间、原句和原文hash。", ""]
    return "\n".join(lines)


def replay_release(input_dir, output_dir):
    """Create a new offline migration directory; reject every overwrite."""
    input_dir, output_dir = Path(input_dir).resolve(), Path(output_dir).absolute()
    if output_dir.resolve().is_relative_to(input_dir):
        raise ValueError("OUTPUT_MUST_NOT_BE_WITHIN_FROZEN_INPUT")
    case_dirs = sorted(path for path in input_dir.iterdir() if path.is_dir() and path.name.startswith("SCALE-"))
    if not case_dirs:
        raise ValueError("NO_FROZEN_SCALE_CASES")
    # Perform completeness checks before reserving the output directory.
    for case_dir in case_dirs:
        for name in CASE_FILES:
            if not (case_dir / name).is_file():
                raise FileNotFoundError(case_dir / name)
    if output_dir.exists():
        raise FileExistsError(output_dir)
    input_records = [_file_record(case_dir / name) for case_dir in case_dirs for name in CASE_FILES]
    input_records += [_file_record(input_dir / name) for name in ("summary.json", "case_provenance.json")
                      if (input_dir / name).is_file()]
    source_receipts = {case_dir.name: _source_receipt(case_dir, _read(case_dir / "decision_events.json"))
                       for case_dir in case_dirs}
    referenced_source_records = [receipt["recorded_original_input"] for receipt in source_receipts.values()
                                 if receipt["recorded_original_input"].get("sha256")]
    code_records = _code_receipts()
    output_dir.mkdir(parents=True, exist_ok=False)
    summaries = []
    for case_dir in case_dirs:
        frozen = _read(case_dir / "decision_events.json")
        source_receipt = source_receipts[case_dir.name]
        compared = compare_case(frozen, _read(case_dir / "episodes.json"), _read(case_dir / "relations.json"))
        target = output_dir / case_dir.name; target.mkdir()
        legacy_target = target / "legacy_replay"; legacy_target.mkdir()
        _write(legacy_target / "decision_events.json", compared["legacy_events"])
        _write(legacy_target / "episodes.json", compared["legacy_episodes"])
        _write(legacy_target / "relations.json", compared["legacy_relations"])
        for filename, key in (("decision_events.json", "strict_events"), ("episodes.json", "strict_episodes"),
                              ("relations.json", "strict_relations"), ("final_graph_audit.json", "graph_audit"),
                              ("event_comparison.json", "event_comparison"), ("relation_comparison.json", "relation_comparison")):
            _write(target / filename, compared[key])
        _write(target / "source_hash_receipt.json", source_receipt)
        summary = dict(case_id=case_dir.name, **compared["summary"])
        _write(target / "summary.json", summary); summaries.append(summary)
    unchanged = all(file_hash(record["path"]) == record["sha256"]
                    for record in input_records + referenced_source_records)
    if not unchanged:
        raise AssertionError("FROZEN_INPUT_CHANGED_DURING_OFFLINE_REPLAY")
    code_unchanged = all(file_hash(record["path"]) == record["sha256"] for record in code_records)
    totals = dict(case_count=len(summaries), event_count=sum(s["legacy_counts"]["event_count"] for s in summaries),
                  legacy_candidate_checkpoint_count=sum(s["legacy_counts"]["candidate_checkpoint_count"] for s in summaries),
                  strict_candidate_checkpoint_count=sum(s["strict_counts"]["candidate_checkpoint_count"] for s in summaries),
                  legacy_candidate_episode_count=sum(s["legacy_counts"]["candidate_episode_count"] for s in summaries),
                  strict_candidate_episode_count=sum(s["strict_counts"]["candidate_episode_count"] for s in summaries),
                  status_changed_event_count=sum(s["status_changed_event_count"] for s in summaries),
                  checkpoint_demotion_count=sum(s["checkpoint_demotion_count"] for s in summaries),
                  supported_to_uncertain_relation_count=sum(s["supported_to_uncertain_relation_count"] for s in summaries),
                  frozen_legacy_replay_mismatch_case_count=sum(not s["frozen_legacy_replay_comparison"]["equal"] for s in summaries))
    source_integrity = dict(valid_action_raw_spans_verified=sum(r["valid_action_citation_count"] for r in source_receipts.values()),
                            legacy_invalid_display_lookup_hash_count=sum(r["legacy_invalid_citation_display_hash_count"] for r in source_receipts.values()),
                            unresolved_event_source_hash_mismatch_count=sum(len(r["event_source_hash_mismatches"]) for r in source_receipts.values()),
                            referenced_original_input_hash_matches=sum(r["recorded_original_input"].get("recorded_sha256_matches") is True
                                                                      for r in source_receipts.values()),
                            referenced_original_input_files_missing=sum(r["recorded_original_input"].get("exists") is False
                                                                       for r in source_receipts.values()))
    report = dict(version=VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(),
                  input_directory=str(input_dir), output_directory=str(output_dir),
                  model_calls=0, reference_read=False, clinical_accuracy_measured=False,
                  clinical_error_count=None, improvement_proven=False,
                  model_semantic_reviews_unchanged=all(s["semantic_review_unchanged"] for s in summaries),
                  frozen_input_files_unchanged=unchanged,
                  code_files_unchanged_during_replay=code_unchanged, source_integrity=source_integrity,
                  totals=totals, cases=summaries)
    manifest = dict(version=VERSION, inputs=input_records, code=code_records,
                    referenced_original_source_inputs=referenced_source_records,
                    code_files_unchanged_during_replay=code_unchanged,
                    frozen_input_files_unchanged=unchanged,
                    source_hash_receipts=[str(output_dir / s["case_id"] / "source_hash_receipt.json") for s in summaries])
    _write(output_dir / "input_manifest.json", manifest)
    _write(output_dir / "summary.json", report)
    (output_dir / "report.md").write_text(_markdown(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = replay_release(args.input, args.output)
    print(json.dumps(report["totals"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
