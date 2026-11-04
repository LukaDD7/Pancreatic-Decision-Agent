"""Run v0.6 with bounded feedback and final reviewed-dependency validation.

No run is started merely by importing this module.  The output directory must
be new; model and endpoint settings still come from the existing model client.
"""
import argparse
from copy import copy
import hashlib
from pathlib import Path
import sys

from scripts.cohort_construction.p2_auto_checkpoint.extract import read, write
from scripts.cohort_construction.p2_auto_checkpoint_v04 import engine, run as source_run
from scripts.cohort_construction.p2_auto_checkpoint_v04.review_feedback import validate_feedback_config


ROOT = Path(__file__).resolve().parents[3]


def preflight(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--config', type=Path, default=ROOT/'config/p2_auto_checkpoint_v06_deepseek.json')
    parser.add_argument('--output', type=Path, required=True)
    known, _ = parser.parse_known_args(argv)
    config = read(known.config)
    validate_feedback_config(config)
    if config.get('version') != 'p2-auto-checkpoint-0.6.0':
        raise ValueError('V06_RUNNER_REQUIRES_EXPLICIT_V06_CONFIG')
    if config.get('semantic_model') not in ('deepseek-v4.1-flash', 'qwen3.8-max'):
        raise ValueError('V06_MODEL_WRAPPER_NOT_SUPPORTED')
    if known.output.exists():
        raise ValueError('V06_REQUIRES_NEW_OUTPUT_DIRECTORY')
    return known, config


def archive_source_input(case_id, args):
    """Keep the exact imported bytes even when the caller used a temp path.

    This snapshot is researcher-only.  The semantic pipeline still imports its
    source-only representation and ignores existing research annotations.
    """
    source=args.sources/case_id/'source_fhir_r4_bundle.json'
    target=args.output/'exact_source_inputs'/case_id/'source_fhir_r4_bundle.json'
    target.parent.mkdir(parents=True,exist_ok=True)
    content=source.read_bytes()
    # Refuse overwriting an earlier snapshot, including from a duplicate case.
    with target.open('xb') as stream:stream.write(content)
    receipt=dict(input_path=str(source.resolve()),snapshot_path=str(target.resolve()),
        sha256=hashlib.sha256(content).hexdigest(),bytes=len(content),
        exact_bytes_preserved=True,researcher_only=True)
    write(target.parent/'source_input_snapshot_receipt.json',receipt)
    return receipt


def main():
    if '-h' in sys.argv[1:] or '--help' in sys.argv[1:]:
        source_run.main()
        return
    known, config = preflight(sys.argv[1:])
    if config['semantic_model'] == 'deepseek-v4.1-flash':
        from scripts.cohort_construction.p2_auto_checkpoint_model_rerun.run import ShapeRetryClient
    else:
        from scripts.cohort_construction.p2_auto_checkpoint_shape_retry.run import ShapeRetryClient
    if not any(a == '--config' or a.startswith('--config=') for a in sys.argv[1:]):
        sys.argv.extend(['--config', str(known.config)])
    # Exclusive creation protects existing runs, including a concurrent launch.
    known.output.mkdir(parents=True, exist_ok=False)
    paths = []
    for package in ('p2_auto_checkpoint_v04', 'p2_auto_checkpoint_v06',
                    'p2_auto_checkpoint_v05_refine', 'p2_auto_checkpoint_model_rerun',
                    'p2_auto_checkpoint_shape_retry'):
        paths.extend((ROOT/'scripts/cohort_construction'/package).glob('*.py'))
    write(known.output/'v06_execution_contract.json', dict(
        version=config['version'], config_path=str(known.config.resolve()),
        config_sha256=hashlib.sha256(known.config.read_bytes()).hexdigest(),
        code_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)},
        original_v05_config_modified=False, remote_model_run_requested=True,
        doctor_packet_release=False, clinical_reference_read=False))
    original_client = engine.Client
    original_run_case = source_run.run_case
    def run_case_with_snapshot(case_id, args, current_config):
        snapshot=archive_source_input(case_id,args)
        frozen_args=copy(args)
        frozen_args.sources=args.output/'exact_source_inputs'
        summary=original_run_case(case_id,frozen_args,current_config)
        if hashlib.sha256(Path(snapshot['snapshot_path']).read_bytes()).hexdigest()!=snapshot['sha256']:
            raise ValueError('SOURCE_SNAPSHOT_CHANGED_DURING_CASE_RUN')
        summary['exact_source_input_snapshot_sha256']=snapshot['sha256']
        write(args.output/case_id/'summary.json',summary)
        return summary
    engine.Client = ShapeRetryClient
    source_run.run_case = run_case_with_snapshot
    try:
        source_run.main()
    finally:
        engine.Client = original_client
        source_run.run_case = original_run_case


if __name__ == '__main__':
    main()
