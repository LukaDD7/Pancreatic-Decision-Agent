"""Prepare/replay private synthetic pipeline inputs; execution is not a CLI default.

The CLI deliberately has no remote-model or doctor-allocation command. An
authorized library caller supplies explicit adapters to ``run_episode_pipeline``.
"""
import argparse
import json
from pathlib import Path

from .orchestration import run_episode_pipeline
from .persistence import read
from .replay import validate_pipeline_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    prepare = subparsers.add_parser('prepare', help='Prepare a new offline Episode request')
    prepare.add_argument('--input', type=Path, required=True)
    prepare.add_argument('--output', type=Path, required=True)
    replay = subparsers.add_parser('replay', help='Validate a saved run without a model')
    replay.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'replay':
        receipt = validate_pipeline_run(read(args.run / 'run.json'), output=args.run)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return
    request = read(args.input)
    if request.get('format') != 'checkpoint-ready-pipeline-cli-request-0.1.0':
        raise ValueError('READY_PIPELINE_CLI_REQUEST_FORMAT_INVALID')
    run = run_episode_pipeline(output=args.output, graph=request['graph'],
        sources=request['sources'], episode_id=request['episode_id'],
        semantic_audits=request['semantic_audits'], questionnaire=request['questionnaire'],
        graph_audit_context=request['graph_audit_context'],
        decision_task_id=request['decision_task_id'],
        visibility_policy_version=request['visibility_policy_version'],
        config=request['config'], execute=False,
        max_rounds=request.get('max_rounds', 3), max_model_calls=request.get('max_model_calls', 20))
    print(json.dumps(dict(state=run['state'], run_sha256=run['run_sha256'],
        model_call_performed=False, doctor_assignment_created=False,
        clinical_release_permitted=False), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
