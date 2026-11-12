"""Run one case through discovery and the selected organization stages.

The entry point is deliberately narrow. It reads a source bundle, builds a
per-case harness, runs the stages, and writes only new files into a fresh output
directory. It refuses to write into an existing case output, because the frozen
batches must stay byte-identical.

No live call happens without ``--execute`` and an ``--authorization`` file that
binds this exact run. The dry run builds the unit inventory and prints the plan
without constructing a provider client or reading a credential.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

AUTHORIZATION_FORMAT = "p2-harness-execution-authorization-0.1.0"
CONTRACT_FORMAT = "p2-harness-run-contract-0.1.0"
RUN_SUMMARY_FORMAT = "p2-harness-run-summary-0.1.0"
EXECUTION_SCOPES = {
    "legacy": "SOURCE_DISCOVERY_ORDERING_EPISODE_CANDIDATE_RECONSTRUCTION",
    "discovery": "SOURCE_DISCOVERY_ONLY_CANDIDATE_SPANS_AND_PROGRAM_EVIDENCE_INDEX",
    "case": "SOURCE_DISCOVERY_CASE_ORGANIZATION_CANDIDATE_RECONSTRUCTION",
}
EXECUTION_SCOPE = EXECUTION_SCOPES["legacy"]
AUTH_REQUIRED = {
    "format": AUTHORIZATION_FORMAT,
    "remote_execution_authorized": True,
    "source_fhir_candidate_reconstruction_authorized": True,
    "doctor_packet_release": False,
    "real_doctor_assignment_creation": False,
    "clinical_release_permitted": False,
}
AUTH_BINDING = ("sources_sha256", "config_sha256", "code_sha256", "case_id")


def _read(path):
    path = Path(path)
    if not path.is_file():
        raise SystemExit("FILE_NOT_FOUND:" + str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def _write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return path


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_hashes():
    """Hash the harness package. A run contract that omits this is not binding."""
    package = Path(__file__).resolve().parent
    paths = sorted(list(package.glob("*.py")) + list(package.glob("tools/*.py"))
                   + list(package.glob("stages/*.py"))
                   + list(package.glob("skills/*/SKILL.md")))
    return {str(path.relative_to(ROOT)): file_hash(path) for path in paths}


def build_plan(sources_path, *, config_path=None):
    """Offline: what would this run do, with no model call and no credential read."""
    from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources
    from scripts.cohort_construction.p2_auto_checkpoint_v04.context import build_units

    bundle = _read(sources_path)
    sources, inventory = import_sources(bundle)
    units = build_units(sources, dict(include_support_documents=True))
    return dict(
        sources=len(sources), units=len(units),
        unit_kinds=dict(_count_by(unit.get("kind") for unit in units)),
        document_profiles=dict(_count_by(unit.get("document_profile")
                                         for unit in units)),
        total_source_characters=sum(len(source.get("text") or "")
                                    for source in sources),
        source_import=dict(inventory),
        config=config_path and _read(config_path) or None)


def _count_by(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def run_contract(args, plan):
    """The exact thing an authorization must bind."""
    return dict(
        format=CONTRACT_FORMAT, case_id=args.case_id,
        sources_sha256=file_hash(args.sources),
        config_sha256=file_hash(args.config) if args.config else None,
        code_hashes=code_hashes(), plan=plan,
        execution_scope=EXECUTION_SCOPES[args.stages],
        clinical_validation=False, clinical_reference_created=False,
        reference_read_by_pipeline=False, doctor_packet_release=False)


def validate_authorization(authorization, contract):
    if not isinstance(authorization, dict):
        raise SystemExit("EXECUTION_AUTHORIZATION_INVALID")
    for field, expected in AUTH_REQUIRED.items():
        if not _matches(authorization.get(field), expected):
            raise SystemExit("EXECUTION_AUTHORIZATION_GOVERNANCE_MISMATCH:" + field)
    if type(authorization.get("execution_scope")) is not str \
            or authorization.get("execution_scope") != contract["execution_scope"]:
        raise SystemExit("EXECUTION_AUTHORIZATION_GOVERNANCE_MISMATCH:execution_scope")
    for field in ("sources_sha256", "config_sha256", "case_id"):
        if authorization.get(field) != contract[field]:
            raise SystemExit("EXECUTION_AUTHORIZATION_BINDING_MISMATCH:" + field)
    if authorization.get("code_sha256") != _code_digest(contract["code_hashes"]):
        raise SystemExit("EXECUTION_AUTHORIZATION_CODE_MISMATCH")
    limits = authorization.get("limits")
    if not isinstance(limits, dict):
        raise SystemExit("EXECUTION_AUTHORIZATION_LIMITS_REQUIRED")
    for field in ("max_requests", "max_total_tokens"):
        value = limits.get(field)
        if type(value) is not int or value <= 0:
            raise SystemExit("EXECUTION_AUTHORIZATION_LIMIT_INVALID:" + field)
    return limits


def _matches(actual, expected):
    """Exact value and exact type. ``True == 1`` must not pass a governance gate.

    ``format`` is a string in JSON, so identity comparison is wrong for it; the
    type check is what keeps ``1`` from standing in for ``True``.
    """
    return type(actual) is type(expected) and actual == expected


def _code_digest(code_hashes):
    payload = json.dumps(code_hashes, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _harness_config(raw, limits):
    """Build the config from a JSON object, refusing unknown keys.

    A typo in a config file that silently falls back to a default is how the
    frozen batch ended up with a character ceiling that bounded nothing. Unknown
    keys are an error here.
    """
    from dataclasses import fields as dataclass_fields

    from .harness import HarnessConfig

    allowed = {field.name for field in dataclass_fields(HarnessConfig)}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise SystemExit("HARNESS_CONFIG_UNKNOWN_FIELDS:" + ",".join(unknown))
    config = HarnessConfig(**raw)
    return HarnessConfig(**dict(
        {field: getattr(config, field) for field in allowed},
        max_requests=min(config.max_requests, limits["max_requests"]),
        max_total_tokens=min(config.max_total_tokens, limits["max_total_tokens"])))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", required=True,
                        help="case source_fhir_r4_bundle.json")
    parser.add_argument("--output", required=True, help="new output directory")
    parser.add_argument("--cache", required=True, help="new cache directory")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--stages", choices=("legacy", "discovery", "case"),
                        default="legacy", dest="stages",
                        help=("legacy: discovery+ordering+episodes; discovery: stop before "
                              "organization; case: discovery+single case-level organization"))
    parser.add_argument("--authorization", default=None,
                        help="read-only exact-run authorization; required to execute")
    parser.add_argument("--execute", action="store_true",
                        help="actually call the provider")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--case-receipt", default=None,
                        help="optional case_completion_receipt.json to hash-bind")
    args = parser.parse_args(argv)

    output = Path(args.output)
    if output.exists() and any(output.iterdir()):
        raise SystemExit("OUTPUT_DIRECTORY_NOT_EMPTY:" + str(output))
    if args.case_receipt:
        # A completed case is never re-run. The receipt is only ever read.
        receipt = _read(args.case_receipt)
        if receipt.get("status") == "COMPLETE":
            raise SystemExit("CASE_ALREADY_COMPLETE:" + args.case_id)
    plan = build_plan(args.sources, config_path=args.config)
    contract = run_contract(args, plan)
    if args.dry_run or not args.execute:
        print(json.dumps(dict(dry_run=True, contract=contract), ensure_ascii=False,
                         indent=2, default=str))
        return 0
    if not args.authorization:
        raise SystemExit("EXECUTION_AUTHORIZATION_REQUIRED_BEFORE_PROVIDER_CLIENT")
    limits = validate_authorization(_read(args.authorization), contract)
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    _write_new(output / "run_contract.json", contract)
    return _execute(args, contract, limits, plan)


def _execute(args, contract, limits, plan):
    """Live run. Kept separate so the plan path cannot accidentally spend tokens."""
    from scripts.cohort_construction.model_api import load_boyu_settings

    from .harness import Harness, HarnessConfig
    from .pipeline import CaseRunner
    from .transport import Transport
    from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources
    from scripts.cohort_construction.p2_auto_checkpoint_v04.context import build_units

    output = Path(args.output)
    bundle = _read(args.sources)
    sources, _ = import_sources(bundle)
    units = build_units(sources, dict(include_support_documents=True))
    config = _harness_config(_read(args.config) if args.config else {}, limits)
    settings = load_boyu_settings()
    Path(args.cache).mkdir(parents=True, exist_ok=True)
    os.chmod(args.cache, 0o700)
    transport = Transport(args.cache, settings,
                          config=dict(max_output_tokens=config.max_output_tokens))
    harness = Harness(case_id=args.case_id, run_dir=output / "session",
                      cache_dir=args.cache, transport=transport, config=config)
    runner = CaseRunner(harness=harness, sources=sources, units=units,
                        case_id=args.case_id)
    stages = {}
    failure = None
    try:
        stages["discovery"] = runner.run_discovery()
    except Exception as exc:  # noqa: BLE001 - a failed stage is recorded, not hidden
        failure = _failure("discovery", exc)
    if args.stages == "legacy" and failure is None:
        try:
            stages["ordering"] = runner.run_ordering()
        except Exception as exc:  # noqa: BLE001
            failure = _failure("ordering", exc)
    if args.stages == "legacy" and failure is None:
        try:
            stages["episodes"] = runner.run_episodes()
        except Exception as exc:  # noqa: BLE001
            failure = _failure("episodes", exc)
    elif args.stages == "case" and failure is None:
        try:
            stages["case_organization"] = runner.run_case_organization()
        except Exception as exc:  # noqa: BLE001
            failure = _failure("case_organization", exc)
    artifacts = runner.artifacts()
    # Recovery failures are historical attempts, not an unfinished unit once
    # the final unit record has answered. Keep them in a separate audit list.
    historical_stops = artifacts["stage_stops"]
    current_stops = [
        stop for stop in historical_stops
        if not (stop.get("recovery_attempt") is not None
                and stop.get("stop") not in STAGE_DONE
                and _latest_stage_succeeded(runner, stop))]
    # ``eval.harness_view`` reads the raw artifact. Do not make an exhausted
    # recovery look successful merely because history was moved aside.
    artifacts["current_stage_stops"] = current_stops
    artifacts["stage_stop_history"] = historical_stops
    artifacts["stage_stops"] = current_stops
    _write_new(output / "case_result.json", artifacts)
    _write_new(output / "discovery.json", dict(
        units=artifacts["discovery"]["units"],
        out_of_scope_candidates=artifacts["out_of_scope_candidates"],
        evidence_inventory=artifacts["evidence_inventory"],
        rejected_mentions=artifacts["rejected_mentions"],
        summary=stages.get("discovery")))
    _write_new(output / "relations.json", artifacts["relations"])
    _write_new(output / "episodes.json", artifacts["episodes"] or {})
    _write_new(output / "audit.json", artifacts["audit"] or {})
    _write_new(output / "model_receipts.json", transport.receipts)
    unfinished = _unfinished(artifacts)
    summary = dict(
        format=RUN_SUMMARY_FORMAT, case_id=args.case_id,
        # A case that ran out of budget, or stopped a unit on a format failure,
        # is not COMPLETE. Saying COMPLETE there would both overstate this run
        # and block the rerun that the `--case-receipt` guard refuses. Running
        # every stage is necessary but not sufficient: a stage can answer while
        # leaving units unrun, and that remainder is work, not an empty result.
        status=("FAILED" if failure else "COMPLETE" if not unfinished
                else "INCOMPLETE"),
        unfinished=unfinished,
        stages=stages, failure=failure, counts=artifacts["counts"],
        compactions=len(harness.compaction.history),
        receipts=len(transport.receipts),
        budget=harness.spend(),
        clinical_accuracy_measured=False, blind_packets_created=False,
        reference_read_by_pipeline=False,
        contract_sha256=_contract_digest(contract))
    harness_summary = harness.close()
    _write_new(output / "summary.json", dict(summary, session=harness_summary,
                                             plan=plan))
    print(json.dumps(dict(execute=True, case_id=args.case_id,
                          status=summary["status"], summary=summary),
                     ensure_ascii=False, indent=2, default=str))
    # A failed or unfinished case is explicitly not complete. The exit code says
    # so; the artifacts stay readable for diagnosis.
    return 1 if summary["status"] != "COMPLETE" else 0


# A stage `stop` that means "this stage finished what it set out to do".
STAGE_DONE = ("answered", "assembled")


def _latest_stage_succeeded(runner, stop):
    """Check final state, not a transient recovery attempt recorded in history."""
    if stop.get("stage") == "ordering":
        latest = getattr(runner, "_ordering_records", [])
        return bool(latest and latest[-1]["stop"] in STAGE_DONE)
    if stop.get("stage") == "case_organization":
        latest = getattr(runner, "_case_organization", None)
        return bool(latest and latest.get("stop") in STAGE_DONE)
    if stop.get("stage") == "discovery":
        unit = runner.discovery.get(stop.get("unit_id"))
        return bool(unit and unit.get("stop") in STAGE_DONE)
    return False


def _unfinished(artifacts):
    """Everything that would make calling this case COMPLETE untrue."""
    reasons = []
    for stop in artifacts.get("stage_stops") or []:
        if stop.get("stop") not in STAGE_DONE:
            reasons.append(dict(kind="stage_stop", **stop))
    coverage = artifacts.get("coverage") or {}
    if coverage.get("units_not_run"):
        reasons.append(dict(kind="units_not_run",
                            count=coverage["units_not_run"],
                            unit_ids=coverage.get("units_not_run_ids")))
    return reasons


def _failure(stage, exc):
    """Record the stage that stopped, without pretending later stages ran."""
    return dict(stage=stage, error=type(exc).__name__, detail=str(exc)[:400],
                note=("该阶段未完成。后续阶段没有运行，未运行的阶段不代表结果是空。"))


def _contract_digest(contract):
    payload = json.dumps(contract, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())