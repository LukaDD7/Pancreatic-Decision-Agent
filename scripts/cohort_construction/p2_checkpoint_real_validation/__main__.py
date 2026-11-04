"""Private fixed-ten development commands; no clinician assignment or release."""
import argparse
import json
from pathlib import Path

from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import read

from .batch import execute_batch, preflight_batch, replay_batch


ROOT = Path(__file__).resolve().parents[3]
from scripts.cohort_construction.p2_checkpoint_material_review_app.build import PRIVATE_ROOT

DEFAULT_STORAGE_ROOT = PRIVATE_ROOT / 'p2_checkpoint_next_20261028'
DEFAULT_BASE = DEFAULT_STORAGE_ROOT / 'model_validation'
DEFAULT_OUTPUT = DEFAULT_STORAGE_ROOT / 'ready_pipeline_real_validation_v063_20261030'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, default=DEFAULT_OUTPUT,
        help='New private batch directory; use a new sibling for every rerun')
    commands = parser.add_subparsers(dest='command', required=True)
    preflight = commands.add_parser('preflight', help='Offline only; never creates a model Client')
    preflight.add_argument('--manifest', type=Path, default=DEFAULT_BASE / 'sampling_manifest.json')
    preflight.add_argument('--derived-release', type=Path, default=DEFAULT_BASE / 'fixed_ten_development_release')
    preflight.add_argument('--output', type=Path)
    execute = commands.add_parser('execute', help='Explicitly authorized real remote development calls')
    execute.add_argument('--preflight', type=Path)
    execute.add_argument('--authorization', type=Path)
    execute.add_argument('--base-config', type=Path, default=ROOT / 'config/p2_checkpoint_dual_audit_v063.json')
    execute.add_argument('--output', type=Path)
    replay = commands.add_parser('replay', help='No-model batch replay')
    replay.add_argument('--output', type=Path)
    replay.add_argument('--current-implementation', action='store_true',
        help='Also require every saved run to match the current source snapshot')
    args = parser.parse_args(argv)
    if args.command == 'preflight':
        result = preflight_batch(manifest=args.manifest, derived_release=args.derived_release,
            output=args.output or args.output_root / 'preflight', private_root=DEFAULT_STORAGE_ROOT)
    elif args.command == 'execute':
        from scripts.cohort_construction.p2_auto_checkpoint_v04.client import Client
        # load_boyu_settings is called only here, after explicit batch execution
        # was requested. Missing credentials stop before any provider request.
        from scripts.cohort_construction.model_api import load_boyu_settings
        load_boyu_settings()
        config = read(args.base_config)
        model_config = read(ROOT / 'config/p2_auto_checkpoint_v06_deepseek.json')
        config.update(model_temperature=model_config['model_temperature'],
            max_output_tokens=model_config['max_output_tokens'], max_concurrent_model_calls=1)
        result = execute_batch(preflight_output=args.preflight or args.output_root / 'preflight',
            authorization=args.authorization or args.output_root / 'execution-authorization.json',
            base_config=config, model_client_factory=Client,
            output=args.output or args.output_root / 'execution', private_root=DEFAULT_STORAGE_ROOT)
    else:
        result = replay_batch(args.output or args.output_root / 'execution',
            verify_implementation=args.current_implementation)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
