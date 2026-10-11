"""CLI for local previews and explicitly executed independent model evaluations."""

import argparse
import json
import os
from pathlib import Path

from .client import ModelSettings
from .runner import run_evaluation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packets", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-url", default=os.environ.get("LLM_API_URL", ""))
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", ""))
    parser.add_argument("--key-env", default="BOYU_API_KEY")
    parser.add_argument("--stages", nargs="+", type=int, default=[1, 2, 3, 4])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--retries", type=int, default=0)
    parser.add_argument("--response-format", choices=["prompt_only", "json_object", "json_schema"], default="prompt_only")
    parser.add_argument("--allow-exploratory-draft", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Send fixed patient contexts to the configured endpoint")
    args = parser.parse_args()
    try:
        bundle = json.loads(args.packets.read_text(encoding="utf-8"))
        settings = ModelSettings(base_url=args.base_url, model=args.model, key_env=args.key_env,
                                 temperature=args.temperature, max_tokens=args.max_tokens, timeout=args.timeout,
                                 retries=args.retries, response_format=args.response_format)
        directory, summary = run_evaluation(bundle, args.output, settings, stages=args.stages,
            repeats=args.repeats, allow_draft=args.allow_exploratory_draft, execute=args.execute,
            progress=lambda stage, repeat, status: print(f"stage={stage} repeat={repeat} status={status}", flush=True))
        print(directory)
        print(f"API attempts={summary['api_attempts']}; valid={summary['valid_samples']}/{summary['attempted_samples']}")
        return 0 if not args.execute or summary["valid_samples"] == summary["attempted_samples"] else 1
    except KeyboardInterrupt:
        print("Evaluation interrupted; completed samples and started attempts remain in local logs.")
        return 130
    except (ValueError, KeyError, TypeError, OSError):
        # Provider errors / malformed configs may contain sensitive text: never print bodies or traces.
        print("Evaluation setup failed; check local packet hashes, draft flag and endpoint/environment settings.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
