"""Explicit offline CLI; output files are created, never overwritten."""

import argparse
import json
import sys
from pathlib import Path

from .compiler import (ContractError, compile_partition, freeze_checkpoint_packet,
                       locate_exact_quote, validate_review_submission)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    # Exclusive creation protects frozen and historical artifacts from overwrite.
    with Path(path).open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    locator = commands.add_parser("locate", help="Return all exact-quote anchors")
    locator.add_argument("--manifest", required=True)
    locator.add_argument("--source-id", required=True)
    locator.add_argument("--exact-quote", required=True)
    locator.add_argument("--prefix")
    locator.add_argument("--suffix")
    locator.add_argument("--output", required=True)
    compiler = commands.add_parser("compile", help="Write researcher partition and mechanical audit")
    compiler.add_argument("--manifest", required=True)
    compiler.add_argument("--proposal", required=True)
    compiler.add_argument("--anchor-catalog", help="JSON array of program-generated source anchors")
    compiler.add_argument("--output", required=True)
    freezer = commands.add_parser("freeze", help="Write doctor-only packet after both audit gates")
    freezer.add_argument("--compiled", required=True)
    freezer.add_argument("--semantic-audit", required=True)
    freezer.add_argument("--questionnaire", required=True)
    freezer.add_argument("--packet-id", required=True)
    freezer.add_argument("--packet-version", required=True)
    freezer.add_argument("--output", required=True)
    validator = commands.add_parser("validate-response", help="Check response packet/form binding only")
    validator.add_argument("--packet", required=True)
    validator.add_argument("--submission", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "locate":
            result = locate_exact_quote(_read(args.manifest), args.source_id, args.exact_quote,
                                        args.prefix, args.suffix)
        elif args.command == "compile":
            result = compile_partition(_read(args.manifest), _read(args.proposal),
                                        anchor_catalog=_read(args.anchor_catalog) if args.anchor_catalog else None)
        elif args.command == "freeze":
            result = freeze_checkpoint_packet(_read(args.compiled), _read(args.semantic_audit),
                                               _read(args.questionnaire), packet_id=args.packet_id,
                                               packet_version=args.packet_version)
        else:
            validate_review_submission(_read(args.submission), _read(args.packet))
            print(json.dumps({"status": "PASS", "scope": "structure_and_binding_only"}))
            return 0
        _write(args.output, result)
        failed = args.command == "compile" and result["mechanical_audit"]["status"] != "PASS"
        print(json.dumps({"output": args.output, "status": "FAIL" if failed else "WRITTEN"}))
        return 3 if failed else 0
    except (ContractError, OSError, ValueError) as exc:
        # Exception messages contain identifiers/codes, never dump source inputs.
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
