"""Independently validate and exclusively save an offline researcher response."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError, canonical_sha256
from scripts.cohort_construction.p2_checkpoint_packet.semantic_audit import validate_semantic_audit, write_assessment
from .build import parse_json_unique, require_private_output, validate_assignment

RESPONSE_FORMAT = 'checkpoint-researcher-material-response-0.1.0'


def receive_submission(assignment, response):
    verified = validate_assignment(assignment)
    required = {'format', 'assignment_id', 'assignment_sha256', 'input_bindings', 'audit', 'audit_sha256'}
    if not isinstance(response, dict) or set(response) != required:
        raise ContractError('MRA_RESPONSE_FIELDS_MISMATCH')
    if (response['format'] != RESPONSE_FORMAT or response['assignment_id'] != assignment['assignment_id']
            or response['assignment_sha256'] != assignment['assignment_sha256']
            or response['input_bindings'] != assignment['bindings']):
        raise ContractError('MRA_RESPONSE_ASSIGNMENT_BINDING_MISMATCH')
    audit = response['audit']
    if not isinstance(audit, dict):
        raise ContractError('MRA_AUDIT_OBJECT_REQUIRED')
    if canonical_sha256(audit) != response['audit_sha256']:
        raise ContractError('MRA_RESPONSE_AUDIT_HASH_MISMATCH')
    draft = verified['documents']['draft']
    for key in ('reviewer_id', 'assessment_id', 'audit_protocol_version', 'assessed_at'):
        if not isinstance(audit.get(key), str) or not audit[key].strip():
            raise ContractError('MRA_EXPLICIT_REVIEWER_METADATA_REQUIRED')
    if (audit['assessment_id'] == draft['assessment_id'] or audit['reviewer_id'] == draft['reviewer_id']
            or audit.get('reviewer_type') not in ('human', 'independent_review_process')):
        raise ContractError('MRA_NEW_ACTUAL_REVIEWER_REQUIRED')
    try:
        assessed_at = datetime.fromisoformat(audit['assessed_at'].replace('Z', '+00:00'))
    except (TypeError, ValueError) as exc:
        raise ContractError('MRA_ACTUAL_REVIEW_TIMESTAMP_REQUIRED') from exc
    if assessed_at.utcoffset() is None:
        raise ContractError('MRA_ACTUAL_REVIEW_TIMESTAMP_REQUIRED')
    validate_semantic_audit(verified['documents']['compiled'], audit)
    # The current form's scope is bound to this task. Adding/removing sources
    # requires a new assignment, not silent widening through a response.
    for key in ('included_source_ids', 'excluded_source_ids'):
        if audit.get('review_scope', {}).get(key) != draft['review_scope'][key]:
            raise ContractError('MRA_RESPONSE_REVIEW_SCOPE_MISMATCH')
    unloaded = set(verified['unloaded_source_ids'])
    for coverage in audit.get('source_coverage_assessments', []):
        if coverage['source_id'] in unloaded and coverage['disposition'] != 'UNRESOLVED':
            raise ContractError('MRA_UNLOADED_SOURCE_MUST_REMAIN_UNRESOLVED')
    return deepcopy(audit)


def receive_files(assignment_path, response_path, *, output):
    assignment = parse_json_unique(Path(assignment_path).read_text(encoding='utf-8'))
    response = parse_json_unique(Path(response_path).read_text(encoding='utf-8'))
    audit = receive_submission(assignment, response)
    output = require_private_output(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = write_assessment(audit, output)
    return dict(result, status='RESEARCHER_RESPONSE_VALIDATED_AND_SAVED',
                assignment_id=assignment['assignment_id'], researcher_only=True,
                doctor_packet_created=False, clinical_reference_created=False,
                model_api_calls=0, independence_authenticated=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assignment', type=Path, required=True)
    parser.add_argument('--response', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    result = receive_files(args.assignment, args.response, output=args.output)
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == '__main__':
    main()
