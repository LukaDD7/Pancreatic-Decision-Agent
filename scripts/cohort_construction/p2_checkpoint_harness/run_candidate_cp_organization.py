"""Run supported candidate-Checkpoint relation recording from frozen discovery.

This entry point never reruns discovery and never modifies ``run_case.py`` or its
outputs. A live call requires ``--execute`` plus an authorization bound to the
exact discovery, source bundle, config, code, case and execution scope. Output
and cache paths must both be new.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import run_case as base


EXECUTION_SCOPE = "FROZEN_DISCOVERY_CANDIDATE_CHECKPOINT_RELATION_RECORDING_ONLY"
CONTRACT_FORMAT = "p2-candidate-cp-organization-run-contract-0.1.0"
RUN_SUMMARY_FORMAT = "p2-candidate-cp-organization-run-summary-0.1.0"
AUTH_BINDING = base.AUTH_BINDING + ("discovery_result_sha256",)
EXTERNAL_EXECUTION_CODE_PATHS = (
    "scripts/cohort_construction/model_api.py",
    "scripts/cohort_construction/p2_auto_checkpoint/fhir_io.py",
    "scripts/cohort_construction/p2_auto_checkpoint/extract.py",
    "scripts/cohort_construction/p2_auto_checkpoint_v04/context.py",
    "scripts/cohort_construction/p2_auto_checkpoint_v04/source_time.py",
    "scripts/cohort_construction/p2_auto_checkpoint_v04/client.py",
    "scripts/cohort_construction/p2_auto_checkpoint_v04/prompts.py",
    "scripts/cohort_construction/p2_auto_checkpoint_v04/provider_diagnostics.py",
    "scripts/cohort_construction/p2_fhir_replay/core.py",
    "scripts/cohort_construction/p2_pathway_replay/core.py",
)


def execution_code_hashes():
    hashes = base.code_hashes()
    for relative in EXTERNAL_EXECUTION_CODE_PATHS:
        hashes[relative] = base.file_hash(base.ROOT / relative)
    return dict(sorted(hashes.items()))


def _verify_input_hashes(args, contract):
    expected = dict(discovery_result_sha256=args.discovery_result,
                    sources_sha256=args.sources,
                    config_sha256=args.config)
    for field, path in expected.items():
        if path is None:
            continue
        if base.file_hash(path) != contract[field]:
            raise SystemExit("INPUT_HASH_CHANGED_AFTER_AUTHORIZATION:" + field)


def _output_hashes(output):
    return {str(path.relative_to(output)): base.file_hash(path)
            for path in sorted(output.rglob("*"))
            if path.is_file() and path.name != "summary.json"}


def _prepare(args):
    from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources

    from .stages.candidate_checkpoints import project_candidate_checkpoints
    from .stages.diagnostic_events import build_diagnostic_events

    frozen = base._read(args.discovery_result)
    for field in ("case_id", "candidate_spans", "evidence_inventory"):
        if field not in frozen:
            raise SystemExit("FROZEN_DISCOVERY_FIELD_MISSING:" + field)
    if frozen["case_id"] != args.case_id:
        raise SystemExit("FROZEN_DISCOVERY_CASE_ID_MISMATCH")
    plan = base.build_plan(args.sources, config_path=args.config)
    sources, _ = import_sources(base._read(args.sources))
    diagnostic_events = build_diagnostic_events(sources, frozen["evidence_inventory"])
    projection = project_candidate_checkpoints(
        case_id=args.case_id, candidates=frozen["candidate_spans"], sources=sources,
        diagnostic_events=diagnostic_events)
    projection_sha256 = base._contract_digest(projection)
    plan.update(
        case_id=args.case_id,
        candidate_projection_sha256=projection_sha256,
        candidate_checkpoint_ids=[item["checkpoint_id"]
                                  for item in projection["candidate_checkpoints"]],
        candidate_projection=projection["summary"],
        discovery_rerun=False)
    return plan, projection, projection_sha256


def run_contract(args, plan):
    contract = dict(
        format=CONTRACT_FORMAT, case_id=args.case_id,
        discovery_result_sha256=base.file_hash(args.discovery_result),
        sources_sha256=base.file_hash(args.sources),
        config_sha256=base.file_hash(args.config) if args.config else None,
        code_hashes=execution_code_hashes(), plan=plan,
        candidate_projection_sha256=plan["candidate_projection_sha256"],
        execution_scope=EXECUTION_SCOPE,
        clinical_validation=False, clinical_reference_created=False,
        reference_read_by_pipeline=False, doctor_packet_release=False)
    return contract, AUTH_BINDING


def validate_authorization(authorization, contract):
    return base.validate_authorization(authorization, contract, AUTH_BINDING)


def _require_new_paths(output, cache):
    for label, path in (("OUTPUT", output), ("CACHE", cache)):
        if os.path.lexists(path):
            raise SystemExit(label + "_PATH_MUST_BE_NEW:" + str(path))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery-result", required=True,
                        help="frozen discovery case_result.json; never rerun or modify it")
    parser.add_argument("--sources", required=True, help="source_fhir_r4_bundle.json")
    parser.add_argument("--output", required=True, help="new output directory")
    parser.add_argument("--cache", required=True, help="new provider cache directory")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--authorization", default=None)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    output, cache = Path(args.output), Path(args.cache)
    _require_new_paths(output, cache)
    plan, projection, projection_sha256 = _prepare(args)
    contract, _ = run_contract(args, plan)
    if args.dry_run or not args.execute:
        print(json.dumps(dict(dry_run=True, contract=contract),
                         ensure_ascii=False, indent=2, default=str))
        return 0
    if not args.authorization:
        raise SystemExit("EXECUTION_AUTHORIZATION_REQUIRED_BEFORE_PROVIDER_CLIENT")
    limits = validate_authorization(base._read(args.authorization), contract)
    _verify_input_hashes(args, contract)
    output.mkdir(parents=True)
    os.chmod(output, 0o700)
    cache.mkdir(parents=True)
    os.chmod(cache, 0o700)
    base._write_new(output / "run_contract.json", contract)
    return _execute(args, contract, limits, plan, projection, projection_sha256)


def _execute(args, contract, limits, plan, projection, projection_sha256):
    from scripts.cohort_construction.model_api import load_boyu_settings
    from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources
    from scripts.cohort_construction.p2_auto_checkpoint_v04.context import build_units

    from .harness import Harness
    from .stages.candidate_cp_organization import (
        CandidateCPOrganizationRunner, assemble_candidate_cp_episodes,
        episodes_document, program_diagnostic_pair_relations, relations_document,
    )
    from .tools.sources import SourceIndex
    from .transport import Transport

    output = Path(args.output)
    config = base._harness_config(base._read(args.config) if args.config else {}, limits)
    settings = load_boyu_settings()
    transport = Transport(args.cache, settings,
                          config=dict(max_output_tokens=config.max_output_tokens,
                                      model_temperature=config.model_temperature))
    harness = Harness(case_id=args.case_id, run_dir=output / "session",
                      cache_dir=args.cache, transport=transport, config=config)
    sources, _ = import_sources(base._read(args.sources))
    units = build_units(sources, dict(include_support_documents=True))
    source_index = SourceIndex(sources, units=units)
    runner = CandidateCPOrganizationRunner(
        harness=harness, source_index=source_index, projection=projection,
        case_id=args.case_id)
    failure = None
    try:
        result = runner.run()
    except Exception as exc:  # noqa: BLE001 - preserve failed-run artifacts
        failure = dict(stage="candidate_cp_organization", error=type(exc).__name__,
                       detail=str(exc)[:400])
        diagnostic = program_diagnostic_pair_relations(projection)
        result = dict(
            relations=diagnostic, agent_relations=[],
            program_diagnostic_pair_relations=diagnostic,
            episodes=assemble_candidate_cp_episodes(
                projection["candidate_checkpoints"], diagnostic),
            stage=dict(stage="candidate_cp_organization", stop="failed",
                       final={}, failure=failure))
    stop = result["stage"]["stop"]
    unfinished = [] if stop == "answered" else [dict(
        kind="stage_stop", stage="candidate_cp_organization", stop=stop)]
    status = "FAILED" if failure else "COMPLETE" if not unfinished else "INCOMPLETE"
    base._write_new(output / "candidate_cp_relations.json", relations_document(
        case_id=args.case_id, projection=projection,
        projection_sha256=projection_sha256, result=result))
    base._write_new(output / "candidate_cp_episodes.json", episodes_document(
        case_id=args.case_id, projection_sha256=projection_sha256, result=result))
    base._write_new(output / "model_receipts.json", transport.receipts)
    harness_summary = harness.close()
    output_hashes = _output_hashes(output)
    summary = dict(
        format=RUN_SUMMARY_FORMAT, case_id=args.case_id, status=status,
        unfinished=unfinished,
        stages=dict(
            discovery_input=dict(
                source="frozen_discovery_result",
                sha256=contract["discovery_result_sha256"],
                candidates=plan["candidate_projection"]["candidate_input_count"],
                candidate_checkpoints=len(projection["candidate_checkpoints"]),
                discovery_rerun=False),
            candidate_cp_organization=result["stage"]),
        failure=failure,
        counts=dict(
            candidate_checkpoints=len(projection["candidate_checkpoints"]),
            relations=len(result["relations"]),
            agent_relations=len(result["agent_relations"]),
            program_diagnostic_pairs=len(result["program_diagnostic_pair_relations"]),
            episodes=len(result["episodes"]["episodes"]),
            singleton_episodes=result["episodes"]["singleton_episodes"],
            evidence_used=len(result["episodes"]["evidence_references"]),
            unresolved_relations=len(result["episodes"]["unresolved_relations"])),
        compactions=len(harness.compaction.history), receipts=len(transport.receipts),
        budget=harness.spend(), clinical_accuracy_measured=False,
        blind_packets_created=False, reference_read_by_pipeline=False,
        doctor_packet_release=False, contract_sha256=base._contract_digest(contract),
        candidate_projection_sha256=projection_sha256,
        output_and_cache_were_new=True, output_hashes=output_hashes)
    base._write_new(output / "summary.json", dict(
        summary, session=harness_summary, plan=plan))
    print(json.dumps(dict(execute=True, case_id=args.case_id,
                          status=status, summary=summary),
                     ensure_ascii=False, indent=2, default=str))
    return 0 if status == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
