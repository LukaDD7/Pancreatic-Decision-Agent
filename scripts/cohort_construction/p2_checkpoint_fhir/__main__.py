"""Offline adapters. All outputs are new files; existing artifacts are protected."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .common import parse_json_unique
from . import (doctor_bundle_from_packet, packet_from_doctor_bundle,
               researcher_bundle_from_graph, response_bundle_from_submission,
               submission_from_response_bundle)


def read(path):
    return parse_json_unique(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor-export", "doctor-import", "research-export", "response-export", "response-import"):
        p = sub.add_parser(name)
        p.add_argument("--input", required=True)
        p.add_argument("--output", required=True)
        if name.startswith("response"):
            p.add_argument("--packet", required=True)
        if name == "response-import":
            p.add_argument("--expected-reviewer-assignment-id")
        if name == "doctor-export":
            p.add_argument("--episode-id")
        if name == "doctor-import":
            p.add_argument("--expected-packet-sha256")
    args = parser.parse_args(argv)
    value = read(args.input)
    if args.command == "doctor-export":
        result = doctor_bundle_from_packet(value, args.episode_id)
    elif args.command == "doctor-import":
        result = packet_from_doctor_bundle(value, expected_packet_sha256=args.expected_packet_sha256)
    elif args.command == "research-export":
        result = researcher_bundle_from_graph(value)
    elif args.command == "response-export":
        result = response_bundle_from_submission(value, read(args.packet))
    else:
        result = submission_from_response_bundle(value, read(args.packet),
            expected_reviewer_assignment_id=args.expected_reviewer_assignment_id)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
