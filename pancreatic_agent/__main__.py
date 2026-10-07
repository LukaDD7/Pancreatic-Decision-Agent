import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .policies import PROMPT_VERSION, SYSTEM_PROMPT, OpenAICompatiblePolicy, ScriptedPolicy, build_messages, render_chart
from .state_machine import ROOT, ACTION_MAP, AuditError, CaseState, LoggedEnvironment, Observation, StateMachine, Status


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    # Previews and traces may contain patient evidence; owner-only on creation.
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Audited offline state machine; preview never calls a model")
    parser.add_argument("command", choices=["preview", "run"])
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--events")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--scripted", help="Local JSON array of outputs for controller testing")
    mode.add_argument("--live", action="store_true", help="Send the reviewed visible state to the configured remote MaaS API")
    parser.add_argument("--model", default="kimi-k3")
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--max-tokens", type=int, default=3000)
    args = parser.parse_args()
    if args.max_tokens <= 0:
        parser.error("--max-tokens must be positive")
    if args.command == "run" and not (args.scripted or args.live):
        parser.error("run requires --scripted or an explicit --live")
    output = ROOT / "outputs" / "episodes" / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}"
    output.mkdir(parents=True, exist_ok=True)
    try:
        state = CaseState.from_dict(read_json(args.baseline))
        state.validate()
        messages = build_messages(state.visible())
        (output / "llm_input_preview.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2))
        (output / "clinical_chart.md").write_text(render_chart(state.visible()), encoding="utf-8")
        manifest = {
            "case_id": state.case_id, "controller_version": "finite-state-v0.2", "policy_version": "pancreas_seq_v0.1",
            "input_format": state.input_format,
            "anchor_policy": "PRE_CLINICIAN_DECISION", "contract_variant": "predecision-review-draft-v0.1",
            "prompt_version": PROMPT_VERSION, "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "baseline_sha256": digest(args.baseline), "event_file_sha256": digest(args.events) if args.events and args.command == "run" else None,
            "output_schema_sha256": digest(ROOT / "contracts/v0.1/06_agent_output.schema.json"),
            "action_mapping_version": ACTION_MAP["version"], "action_mapping_sha256": digest(ROOT / "configs/action_event_map.v1.json"),
            "endpoint": state.endpoint, "temporal_policy": state.temporal_policy,
            "baseline_reviewed": state.baseline_reviewed, "execution_mode": "preview" if args.command == "preview" else "live" if args.live else "scripted",
            "model": args.model if args.live else None, "temperature": args.temperature if args.live else None,
            "top_p": None, "seed": None, "max_tokens": args.max_tokens if args.live else None,
            "consistency_repetitions": 1, "evaluation_rubric_version": "not_evaluated",
        }
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        if args.command == "preview":
            print("Preview saved locally; no API call made.")
            print(output)
            return 0
        events = [Observation.from_dict(e) for e in read_json(args.events)] if args.events else []
        machine = StateMachine(state, LoggedEnvironment(events), output / "trace.jsonl")
        # Check data before creating a remote client; failure never sends patient data.
        try:
            state.validate(for_execution=True)
        except AuditError:
            machine.run(ScriptedPolicy([]))
            (output / "result.json").write_text(json.dumps({"status": machine.status.value, "terminal_output": None, "policy_calls": 0, "model_calls": 0}, indent=2))
            print(machine.status.value)
            print(output)
            return 2
        policy = OpenAICompatiblePolicy(args.model, args.temperature, args.max_tokens) if args.live else ScriptedPolicy(read_json(args.scripted))
        try:
            status = machine.run(policy)
        finally:
            if args.live:
                policy.close()
        calls = sum(x.get("event") == "policy_input" for x in machine.trace)
        (output / "result.json").write_text(json.dumps({"status": status.value, "terminal_output": machine.terminal_output, "policy_calls": calls, "model_calls": calls if args.live else 0}, ensure_ascii=False, indent=2))
        print(status.value)
        print(output)
        return 0 if status == Status.TERMINAL else 2
    except (AuditError, ValueError, TypeError, OSError) as exc:
        # No sensitive provider bodies/credentials are printed.
        print("Input/configuration error:", str(exc) if isinstance(exc, AuditError) else type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
