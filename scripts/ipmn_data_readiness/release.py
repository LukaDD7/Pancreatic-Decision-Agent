import argparse
import json
from pathlib import Path

from .handoff import build_readiness_handoff


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Bind IPMN readiness reviews to one frozen audit and candidate manifest")
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--review-file", type=Path, required=True)
    parser.add_argument("--candidate-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Refusing to overwrite an existing handoff")
    handoff = build_readiness_handoff(
        read_json(args.summary),
        read_json(args.review_file),
        read_json(args.candidate_manifest) if args.candidate_manifest else None,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(handoff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(handoff["decision"])


if __name__ == "__main__":
    main()
