"""Bind an independent semantic/material assessment to exact source spans.

An assessment records who reviewed which source ranges, time boundary and
coverage gaps. The program verifies that record; it never manufactures a
semantic PASS from model labels or mechanical partition success.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from .compiler import (
    ContractError, canonical_sha256, compile_partition, text_sha256, validate_contract,
)

AUDIT_VERSION = '0.2.0'
CHECK_NAMES = (
    'segmentation_semantics_reviewed', 'permitted_information_scope_reviewed',
    'cross_document_restatements_reviewed', 'required_source_coverage_reviewed',
    'decision_time_boundary_reviewed', 'all_segments_explicitly_assessed',
)


def _verified_compiled(compiled: dict) -> dict[str, dict]:
    validate_contract('CompiledPartition', compiled)
    if canonical_sha256({k: v for k, v in compiled.items() if k != 'compiled_sha256'}) != compiled['compiled_sha256']:
        raise ContractError('COMPILED_HASH_MISMATCH')
    if compiled['mechanical_audit']['status'] != 'PASS' or compiled['mechanical_audit']['issues']:
        raise ContractError('MECHANICAL_AUDIT_NOT_PASSED')
    result = {}
    for source in compiled['sources']:
        cursor, original = 0, []
        for span in source['segments']:
            if span['start'] != cursor or span['end'] <= cursor:
                raise ContractError('PARTITION_IS_NOT_COMPLETE_AND_EXCLUSIVE')
            if len(span['text']) != span['end'] - span['start']:
                raise ContractError('PARTITION_TEXT_LENGTH_MISMATCH')
            key = (source['source_id'], span['start'], span['end'])
            result[key] = dict(source_id=source['source_id'], source_sha256=source['source_sha256'], **deepcopy(span))
            original.append(span['text'])
            cursor = span['end']
        if cursor != source['length_codepoints'] or text_sha256(''.join(original)) != source['source_sha256']:
            raise ContractError('PARTITION_SOURCE_HASH_MISMATCH')
    return result


def _new_segment_id(audit: dict, span: dict) -> str:
    return 'SA-' + canonical_sha256([
        audit['assessment_id'], span['source_id'], span['start'], span['end'],
        span['assessment'], span.get('revised_disposition'),
    ])[:24]


def validate_semantic_audit(compiled: dict, audit: dict) -> dict:
    """Verify a non-PASS assessment and return its binding summary.

    PASS additionally requires resolved source coverage, a complete review
    scope, no unresolved issues, and acceptance of the current partition.
    Revisions remain PENDING/FAIL until recompilation and a new assessment.
    An explicitly opposite included/excluded source assessment also requires
    a new manifest/partition and reassessment, never silent source movement.
    """
    spans = _verified_compiled(compiled)
    validate_contract('SemanticAudit', audit)
    if audit['compiled_sha256'] != compiled['compiled_sha256'] or audit['target'] != compiled['target']:
        raise ContractError('SEMANTIC_AUDIT_BINDING_MISMATCH')
    if audit['reviewer_type'] == 'research_preparation':
        if audit['status'] != 'PENDING' or audit['independence_attested']:
            raise ContractError('RESEARCH_PREPARATION_IS_NOT_INDEPENDENT_REVIEW')
    if audit['status'] == 'PASS' and not audit['independence_attested']:
        raise ContractError('INDEPENDENCE_ATTESTATION_REQUIRED')
    sources = {source['source_id'] for source in compiled['sources']}
    coverage_ids = set()
    for item in audit['source_coverage_assessments']:
        if item['source_id'] in coverage_ids:
            raise ContractError('DUPLICATE_SOURCE_COVERAGE_ASSESSMENT')
        coverage_ids.add(item['source_id'])
    if coverage_ids != sources | set(audit['review_scope']['excluded_source_ids']):
        raise ContractError('SOURCE_COVERAGE_BINDING_MISMATCH')
    if set(audit['review_scope']['included_source_ids']) != sources:
        raise ContractError('INCLUDED_SOURCE_SCOPE_MISMATCH')
    if sources & set(audit['review_scope']['excluded_source_ids']):
        raise ContractError('OVERLAPPING_INCLUDED_AND_EXCLUDED_SOURCE_SCOPE')
    issue_ids = set()
    for issue in audit['issue_records']:
        if issue['issue_id'] in issue_ids:
            raise ContractError('DUPLICATE_SEMANTIC_ISSUE_ID')
        issue_ids.add(issue['issue_id'])
        if set(issue['source_ids']) - sources - set(audit['review_scope']['excluded_source_ids']):
            raise ContractError('ISSUE_SOURCE_OUTSIDE_REVIEW_SCOPE')
        for quote in issue['supporting_quotes']:
            source_id, start, end = quote['source_id'], quote['start'], quote['end']
            span = spans.get((source_id, start, end))
            if span is None or span['text'] != quote['exact_quote']:
                raise ContractError('SEMANTIC_ISSUE_QUOTE_BINDING_MISMATCH')
    seen, resolved = set(), []
    for segment in audit['segment_assessments']:
        key = (segment['source_id'], segment['start'], segment['end'])
        if key in seen:
            raise ContractError('DUPLICATE_SEGMENT_ASSESSMENT')
        seen.add(key)
        original = spans.get(key)
        if original is None:
            raise ContractError('SEGMENT_ASSESSMENT_OUTSIDE_COMPILED_PARTITION')
        if (segment['source_sha256'] != original['source_sha256']
                or segment['exact_quote'] != original['text']
                or segment['original_disposition'] != original['disposition']):
            raise ContractError('SEGMENT_ASSESSMENT_BINDING_MISMATCH')
        assessment = segment['assessment']
        if assessment == 'ACCEPT':
            if original['disposition'] == 'REVIEW' or 'revised_disposition' in segment:
                raise ContractError('ACCEPT_REQUIRES_PRIOR_KEEP_OR_MASK')
            disposition = original['disposition']
        elif assessment == 'REVISE':
            if 'revised_disposition' not in segment:
                raise ContractError('REVISION_REQUIRES_KEEP_OR_MASK')
            disposition = segment['revised_disposition']
        else:
            if 'revised_disposition' in segment:
                raise ContractError('UNRESOLVED_ASSESSMENT_CANNOT_PROPOSE_DISPOSITION')
            disposition = 'REVIEW'
        resolved.append(dict(segment_id=_new_segment_id(audit, segment), source_id=segment['source_id'],
                             disposition=disposition,
                             resolved_span=dict(start=segment['start'], end=segment['end'],
                                                exact_quote=segment['exact_quote'])))
    if seen != set(spans):
        raise ContractError('EVERY_COMPILED_SEGMENT_REQUIRES_ASSESSMENT')
    if audit['status'] == 'PASS':
        if not all(audit['checks'][name] for name in CHECK_NAMES):
            raise ContractError('SEMANTIC_CHECKS_INCOMPLETE')
        if audit['review_scope']['scope_status'] != 'COMPLETE':
            raise ContractError('INCOMPLETE_REVIEW_SCOPE_CANNOT_PASS')
        if any(item['disposition'] == 'UNRESOLVED' for item in audit['source_coverage_assessments']):
            raise ContractError('UNRESOLVED_SOURCE_COVERAGE_CANNOT_PASS')
        if any(segment['assessment'] == 'UNRESOLVED' for segment in audit['segment_assessments']):
            raise ContractError('UNRESOLVED_SEGMENT_CANNOT_PASS')
        if any(segment['assessment'] == 'REVISE' for segment in audit['segment_assessments']):
            raise ContractError('REVISION_REQUIRES_RECOMPILE_AND_REAUDIT')
        excluded_sources = set(audit['review_scope']['excluded_source_ids'])
        if any((item['source_id'] in sources and item['disposition'] == 'REVIEWED_EXCLUDED')
               or (item['source_id'] in excluded_sources and item['disposition'] == 'REVIEWED_INCLUDED')
               for item in audit['source_coverage_assessments']):
            raise ContractError('SOURCE_SCOPE_REVISION_REQUIRES_NEW_PARTITION_AND_REAUDIT')
        if any(issue['status'] == 'OPEN' and issue['severity'] == 'BLOCKING'
               for issue in audit['issue_records']):
            raise ContractError('OPEN_BLOCKING_ISSUE_CANNOT_PASS')
    summary = dict(assessment_id=audit['assessment_id'], assessment_sha256=canonical_sha256(audit),
                   status=audit['status'], compiled_sha256=compiled['compiled_sha256'],
                   assessed_segment_count=len(seen), review_scope=deepcopy(audit['review_scope']),
                   open_blocking_issue_count=sum(issue['status'] == 'OPEN' and issue['severity'] == 'BLOCKING'
                                                for issue in audit['issue_records']))
    return summary


def revise_partition(manifest: dict, proposal: dict, audit: dict, *, compiled: dict | None = None) -> dict:
    """Apply an independently reviewed revision; keep old inputs unchanged.

    PENDING and FAIL assessments may both carry explicit REVISE instructions.
    The returned partition is a new mechanical artifact, not a semantic PASS.
    """
    compiled = compile_partition(manifest, proposal) if compiled is None else compiled
    validate_semantic_audit(compiled, audit)
    revised = deepcopy(proposal)
    revised.update(proposal_id='SPL-' + canonical_sha256([
        proposal['proposal_id'], audit['assessment_id'], audit['compiled_sha256'],
        audit['review_scope'], audit['decision_time_boundary'],
    ])[:24], proposal_version=audit['audit_protocol_version'], segments=[])
    for segment in audit['segment_assessments']:
        disposition = segment.get('revised_disposition')
        if segment['assessment'] == 'ACCEPT':
            disposition = segment['original_disposition']
        if disposition is None:
            disposition = 'REVIEW'
        revised['segments'].append(dict(segment_id=_new_segment_id(audit, segment),
                                        source_id=segment['source_id'], disposition=disposition,
                                        resolved_span=dict(start=segment['start'], end=segment['end'],
                                                           exact_quote=segment['exact_quote'])))
    result = compile_partition(manifest, revised)
    if any(segment['assessment'] == 'UNRESOLVED' for segment in audit['segment_assessments']):
        if not any(span['disposition'] == 'REVIEW' for source in result['sources'] for span in source['segments']):
            raise ContractError('UNRESOLVED_SEGMENT_MUST_REMAIN_REVIEW')
    return result


def write_assessment(audit: dict, output: str | Path) -> dict:
    """Write a researcher-only assessment without validating it as PASS."""
    validate_contract('SemanticAudit', audit)
    path = Path(output)
    with path.open('x', encoding='utf-8', newline='') as stream:
        json.dump(audit, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    return dict(status='SAVED_RESEARCHER_ASSESSMENT', assessment_status=audit['status'],
                assessment_sha256=canonical_sha256(audit), output=str(path))
