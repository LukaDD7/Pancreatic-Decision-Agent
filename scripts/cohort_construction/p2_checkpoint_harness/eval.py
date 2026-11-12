"""Compare one case's frozen (v0.8.0) output with a harness run, on measurements.

This does not score clinical quality. It compares what each pipeline *did*:

* how many events were proposed, how many survived to the end
* how many were refused for citation or time problems, and whether they were
  dropped (frozen) or kept with the problem attached (harness)
* how many relations were assembled versus blocked
* how many episodes, and how many events ended up unconnected
* how many provider tokens bought those results

Every number here is a count from the artifacts. Nothing in this module reads a
clinical Reference, and nothing it prints is an accuracy claim.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

FORMAT = "p2-harness-eval-0.1.0"

FROZEN_FILES = ("decision_events.json", "episodes.json", "relations.json",
                "model_receipts.json", "summary.json", "discovery.json")
HARNESS_FILES = ("case_result.json", "model_receipts.json", "summary.json")


def _read(path):
    path = Path(path)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def frozen_view(directory):
    directory = Path(directory)
    events = _read(directory / "decision_events.json") or []
    episodes = _read(directory / "episodes.json") or []
    relations = _read(directory / "relations.json") or []
    receipts = _read(directory / "model_receipts.json") or []
    discovery = _read(directory / "discovery.json") or {}
    summary = _read(directory / "summary.json") or {}
    discovered = (discovery or {}).get("events") or []
    return dict(
        pipeline="v04",
        unit_count=(summary or {}).get("whole_document_or_test_units"),
        discovered_events=len(discovered),
        final_events=len(events),
        statuses=dict(Counter(event.get("checkpoint_status")
                              for event in events)),
        demoted=sum(1 for event in events
                    if str(event.get("checkpoint_status", "")).startswith("NOT_")),
        recorded_functions=dict(Counter(event.get("record_function")
                                        for event in events)),
        episodes=len(episodes),
        relations=len(relations),
        blocked_relations=sum(1 for relation in relations
                              if relation.get("blocked") or relation.get("status")
                              in ("BLOCKED", "blocked")),
        events_without_choice_time=sum(1 for event in events
                                       if event.get("choice_time") in (None, "")),
        failures=len((discovery or {}).get("failures") or []),
        status=_completion_status(summary, bool((discovery or {}).get("failures"))),
        prompt_tokens=_prompt_tokens(receipts),
        requests=len(receipts),
        cached=sum(1 for receipt in receipts if receipt.get("cached")),
        files_present=[name for name in FROZEN_FILES
                       if (directory / name).is_file()])


def harness_view(directory):
    directory = Path(directory)
    artifacts = _read(directory / "case_result.json") or {}
    receipts = _read(directory / "model_receipts.json") or []
    summary = _read(directory / "summary.json") or {}
    counts = artifacts.get("counts") or {}
    episodes = artifacts.get("episodes") or {}
    audit = artifacts.get("audit") or {}
    events = artifacts.get("events") or []
    return dict(
        pipeline="harness",
        unit_count=(artifacts.get("discovery") or {}).get("units_run"),
        discovered_events=len(events),
        final_events=len([event for event in events
                          if not event.get("demoted") and event.get("scope") != "no"]),
        statuses=dict(Counter(event.get("status") for event in events)),
        demoted=counts.get("demoted", 0),
        recorded_functions=dict(Counter(event.get("record_function")
                                        for event in events)),
        episodes=counts.get("episodes", 0),
        relations=counts.get("relations", 0),
        blocked_relations=(audit.get("unresolved_relation_count") or 0),
        events_without_choice_time=audit.get("events_without_documented_choice_time"),
        failures=len([stop for stop in artifacts.get("stage_stops") or []
                      if stop.get("stop") not in ("answered", "assembled")]),
        zero_event_units=(summary.get("stages", {}).get("discovery") or {}).get(
            "zero_event_units"),
        rejected_mentions=len(artifacts.get("rejected_mentions") or []),
        units_not_run=(artifacts.get("coverage") or {}).get("units_not_run"),
        status=summary.get("status"),
        unfinished=summary.get("unfinished"),
        prompt_tokens=_prompt_tokens(receipts),
        requests=len(receipts),
        cached=sum(1 for receipt in receipts if receipt.get("cached")),
        files_present=[name for name in HARNESS_FILES
                       if (directory / name).is_file()])


def _completion_status(summary, has_failures):
    """Read a frozen receipt without inventing a modern ``status`` field.

    Legacy frozen case summaries used ``failures: 0`` rather than COMPLETE.
    A missing failures field remains unknown; it must not certify a directory.
    """
    if summary.get("status"):
        return summary["status"]
    failures = summary.get("failures")
    if isinstance(failures, int):
        return "FAILED" if failures or has_failures else "LEGACY_NO_FAILURES"
    return None


def _prompt_tokens(receipts):
    total = 0
    for receipt in receipts or []:
        usage = receipt.get("usage") or {}
        value = usage.get("prompt_tokens")
        if value is None:
            value = usage.get("input_tokens")
        if isinstance(value, int):
            total += value
    return total


def compare(frozen_dir, harness_dir):
    left = frozen_view(frozen_dir)
    right = harness_view(harness_dir)
    # Files present is not completion. An INCOMPLETE run with 18 failed units
    # and an interrupted ordering turn cannot support an effect comparison even
    # when all three output files exist (live r6). Retain its measurements for
    # diagnosis, but null the deltas and name the blocker.
    complete = {"COMPLETE", "LEGACY_NO_FAILURES"}
    blockers = []
    for label, view in (("FROZEN", left), ("HARNESS", right)):
        if not view["files_present"]:
            blockers.append(label + "_ARTIFACTS_MISSING")
        elif not view.get("status"):
            blockers.append(label + "_RUN_STATUS_MISSING")
        elif view["status"] not in complete:
            blockers.append(label + "_RUN_NOT_COMPLETE")
    comparable = not blockers
    fields = ("unit_count", "discovered_events", "final_events", "demoted",
              "episodes", "relations", "blocked_relations",
              "events_without_choice_time", "requests", "prompt_tokens")
    deltas = {}
    for field in fields:
        a, b = left.get(field), right.get(field)
        deltas[field] = (None if not comparable or a is None or b is None
                         else b - a)
    return dict(
        format=FORMAT, frozen=left, harness=right, deltas=deltas,
        comparable=comparable,
        comparison_blockers=blockers,
        note=("这是流程产出的比较，不是临床准确性比较。"
              "两侧都没有读取医生Reference；数量差异既不证明更好也不证明更差，"
              "只说明两套机制在同一病例上产出了什么。"
              "任何一侧未完成时 comparable=false，计数只用于诊断。"))


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen", required=True, help="v04 case output directory")
    parser.add_argument("--harness", required=True, help="harness case output directory")
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    result = compare(args.frozen, args.harness)
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())