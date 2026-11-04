"""One conditional proof refinement of a completed case or fixed-ten release."""
import argparse
from pathlib import Path
from urllib.parse import urlsplit

from scripts.cohort_construction.p2_auto_checkpoint.extract import read, write
from scripts.cohort_construction.p2_auto_checkpoint_v04.client import Client
from .refine import refine_case

ROOT = Path(__file__).resolve().parents[3]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/p2_auto_checkpoint_v05.json')
    parser.add_argument('--schema', type=Path, default=ROOT / 'private/p2_auto_checkpoint_v0.1_20261024/standards/fhir.schema.json')
    args = parser.parse_args()
    if args.output.exists() or args.output.resolve().is_relative_to(args.input.resolve()):
        parser.error('--output must be a new directory outside --input')
    config = read(args.config)
    if config['semantic_model'] != 'qwen3.8-max':
        parser.error('This frozen refinement requires the same qwen3.8-max model')
    client = Client(args.cache, config)
    if urlsplit(client.settings.chat_completions_url).hostname != 'apicz.boyuerichdata.com':
        parser.error('Configured destination is outside the authorized model host')
    if (args.input / 'decision_events.json').exists():
        case_dirs = [args.input]
    else:
        case_dirs = sorted(p.parent for p in args.input.glob('SCALE-*/decision_events.json'))
    if not case_dirs:
        parser.error('No completed decision_events.json found')
    summaries = []
    single = len(case_dirs) == 1 and case_dirs[0] == args.input
    for case in case_dirs:
        out = args.output if single else args.output / case.name
        summary = refine_case(case, out, args.cache, config, client, args.schema)
        summaries.append(summary)
        if not single:
            write(args.output / 'summary.json', summaries)
        print(f"{summary['case_id']}: targets={summary['repair_target_events']} candidates={summary['candidate_checkpoints']}", flush=True)


if __name__ == '__main__':
    main()
