"""CLI for offline developmental tasks; never sends source text to a model."""
import argparse
import json
import os
from pathlib import Path

from .core import ValidationError, write
from .workflow import compare, prepare, reveal, submit, summarize

ROOT = Path(__file__).resolve().parents[4]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare')
    p.add_argument('--release', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--seed', type=int, default=20261029)
    p.add_argument('--per-stratum', type=int, default=2)
    p.add_argument('--context-days', type=int, default=0,
                   help='Explicit proxy-clock retrieval scope; not clinical availability or Episode boundaries. Default current unit only.')
    p = commands.add_parser('submit')
    p.add_argument('--batch', type=Path, required=True)
    p.add_argument('--source-review', type=Path, required=True)
    p = commands.add_parser('reveal')
    p.add_argument('--batch', type=Path, required=True)
    p.add_argument('--task-id', required=True)
    p.add_argument('--reviewer-id', required=True)
    p = commands.add_parser('compare')
    p.add_argument('--batch', type=Path, required=True)
    p.add_argument('--comparison', type=Path, required=True)
    p = commands.add_parser('summarize')
    p.add_argument('--batch', type=Path, required=True)
    p.add_argument('--output', type=Path, help='Optional new receipt; existing files are never replaced.')
    args = parser.parse_args()
    os.umask(0o077)
    # Actual source material and human submissions stay in project private/.
    # The Python API also requires a resolved private/ path for fixture batches.
    target = args.output if args.command == 'prepare' else args.batch
    if not target.resolve().is_relative_to(ROOT / 'private'):
        parser.error('research task material must remain in project private/')
    if args.command == 'summarize' and args.output is not None:
        if not args.output.resolve().is_relative_to(ROOT / 'private'):
            parser.error('summary receipt must remain in project private/')
    try:
        if args.command == 'prepare':
            result = prepare(args.release, args.output, seed=args.seed,
                             per_stratum=args.per_stratum, context_days=args.context_days)
        elif args.command == 'submit':
            result = submit(args.batch, args.source_review)
        elif args.command == 'reveal':
            result = reveal(args.batch, args.task_id, args.reviewer_id)
        elif args.command == 'compare':
            result = compare(args.batch, args.comparison)
        else:
            result = summarize(args.batch)
            if args.output is not None:
                write(args.output, result)
    except (ValidationError, FileExistsError) as error:
        # Error codes and aggregates only; no raw source text or answers.
        parser.exit(2, type(error).__name__ + ': ' + str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
