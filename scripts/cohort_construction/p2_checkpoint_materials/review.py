"""Create an explicitly AI-authored, unresolved researcher review draft.

This prepares reading work; it is not an independent human material audit.
No source text is sent to a model and no doctor packet is generated.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from scripts.cohort_construction.p2_checkpoint_packet.compiler import (
    ContractError, canonical_sha256, validate_contract,
)
from scripts.cohort_construction.p2_checkpoint_packet.semantic_audit import (
    validate_semantic_audit, write_assessment,
)

DRAFT_PROTOCOL = 'checkpoint-material-review-draft-0.2.0'


def create_review_draft(compiled, manifest, preparation_audit, *, reviewer_id,
                        focus_source_id=None, proposed_mask_spans=(), issue_records=(),
                        time_boundary=None, protocol_version=DRAFT_PROTOCOL):
    validate_contract('CompiledPartition', compiled)
    validate_contract('SourceManifest', manifest)
    manifest_sources = {source['source_id']: source for source in manifest['sources']}
    if set(manifest_sources) != {source['source_id'] for source in compiled['sources']}:
        raise ContractError('DRAFT_MANIFEST_SOURCE_SET_MISMATCH')
    for source in compiled['sources']:
        original = manifest_sources[source['source_id']]
        if (source['source_version'] != original['source_version']
                or source['source_sha256'] != original['sha256']):
            raise ContractError('DRAFT_MANIFEST_SOURCE_BINDING_MISMATCH')
    routing = preparation_audit.get('source_routing', [])
    included = sorted(item['source_id'] for item in routing if item['disposition'] == 'INCLUDED_FULL_ORIGINAL_TEXT')
    excluded = sorted(item['source_id'] for item in routing if item['disposition'] == 'EXCLUDED_OUTSIDE_EVENT_SCOPE')
    if set(included) != set(manifest_sources) or set(included) & set(excluded):
        raise ContractError('DRAFT_PREPARATION_SOURCE_SCOPE_MISMATCH')
    proposed = {(span['source_id'], span['start'], span['end']) for span in proposed_mask_spans}
    if focus_source_id is not None and focus_source_id not in manifest_sources:
        raise ContractError('DRAFT_FOCUS_SOURCE_NOT_INCLUDED')
    assessments = []
    for source in compiled['sources']:
        for span in source['segments']:
            key = (source['source_id'], span['start'], span['end'])
            if key in proposed:
                if source['source_id'] != focus_source_id or span['disposition'] != 'MASK':
                    raise ContractError('DRAFT_MASK_PROPOSAL_MUST_MATCH_FOCUS_MASK_SPAN')
                assessment, revised, rationale = 'REVISE', 'MASK', 'AI草稿：保留原MASK提案供审核者核对，尚未获得独立语义许可。'
            else:
                assessment, revised, rationale = 'UNRESOLVED', None, 'AI草稿：尚未由独立审核者阅读并决定此原文字符区间。'
            item = dict(source_id=source['source_id'], start=span['start'], end=span['end'],
                        source_sha256=source['source_sha256'], exact_quote=span['text'],
                        original_disposition=span['disposition'], assessment=assessment,
                        rationale=rationale, issue_codes=[])
            if revised:
                item['revised_disposition'] = revised
            assessments.append(item)
    coverage = []
    for source_id in included:
        coverage.append(dict(source_id=source_id, disposition='UNRESOLVED',
                             rationale='AI草稿：焦点来源已定位，未声称独立审核完成。' if source_id == focus_source_id
                             else 'AI草稿：此纳入来源的语义、必要性和可见性仍待独立审核。',
                             availability_basis='UNRESOLVED_INDEPENDENT_REVIEW_REQUIRED'))
    for source_id in excluded:
        coverage.append(dict(source_id=source_id, disposition='UNRESOLVED',
                             rationale='AI草稿：当前事件材料范围外；是否含必要资料或决策回述尚未逐份排除。',
                             availability_basis='NOT_REVIEWED_NOT_AN_ABSENCE_ASSERTION'))
    audit = dict(schema_version='0.2.0',
                 assessment_id='AID-' + canonical_sha256([compiled['compiled_sha256'], reviewer_id, protocol_version])[:24],
                 compiled_sha256=compiled['compiled_sha256'], target=deepcopy(compiled['target']),
                 status='PENDING', reviewer_id=reviewer_id, reviewer_type='research_preparation',
                 independence_attested=False,
                 assessed_at=datetime.now(timezone.utc).isoformat(), audit_protocol_version=protocol_version,
                 review_scope=dict(scope_status='INCOMPLETE', included_source_ids=included,
                                   excluded_source_ids=excluded,
                                   scope_basis='AI草稿按既有准备回执枚举来源，不声称已阅读全部来源或覆盖全患者资料。'),
                 decision_time_boundary=time_boundary or dict(
                     basis='UNRESOLVED', precision='unknown', exact_time_known=False,
                     retrospective_reconstruction=True, uncertainties=['实际选择时间和资料可见性仍待独立审核。']),
                 checks={name: False for name in ('segmentation_semantics_reviewed', 'permitted_information_scope_reviewed',
                                                  'cross_document_restatements_reviewed', 'required_source_coverage_reviewed',
                                                  'decision_time_boundary_reviewed', 'all_segments_explicitly_assessed')},
                 segment_assessments=assessments, issue_records=deepcopy(list(issue_records)),
                 source_coverage_assessments=coverage,
                 required_actions=['由实际独立审核者阅读原文，逐段确认或修订，并填写真实审核时间及独立性声明。',
                                   '审核跨文书同次决策回述、当时可见性、必要来源范围及本登记问题。',
                                   '在临床任务、Mode/action字典和表单锚点冻结前，不生正式医生包。'])
    validate_semantic_audit(compiled, audit)
    return audit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiled', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--preparation-audit', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reviewer-id', default='AI-DRAFT-NOT-AN-INDEPENDENT-HUMAN-AUDIT')
    parser.add_argument('--focus-source-id')
    parser.add_argument('--proposal-json', type=Path,
                        help='Researcher-only proposed_mask_spans, issue_records and decision_time_boundary.')
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError('REVIEW_DRAFT_REQUIRES_NEW_OUTPUT')
    proposal = json.loads(args.proposal_json.read_text(encoding='utf-8')) if args.proposal_json else {}
    with patch('socket.socket', side_effect=AssertionError('NETWORK_FORBIDDEN')):
        audit = create_review_draft(
            json.loads(args.compiled.read_text(encoding='utf-8')),
            json.loads(args.manifest.read_text(encoding='utf-8')),
            json.loads(args.preparation_audit.read_text(encoding='utf-8')),
            reviewer_id=args.reviewer_id, focus_source_id=args.focus_source_id,
            proposed_mask_spans=proposal.get('proposed_mask_spans', []),
            issue_records=proposal.get('issue_records', []),
            time_boundary=proposal.get('decision_time_boundary'))
        result = write_assessment(audit, args.output)
    receipt = dict(result, status='AI_REVIEW_DRAFT_PENDING_NOT_INDEPENDENT_AUDIT',
                   draft_sha256=canonical_sha256(audit), model_api_calls=0,
                   doctor_packet_created=False, independence_attested=False,
                   input_sha256={name: hashlib.sha256(path.read_bytes()).hexdigest()
                                 for name, path in [('compiled', args.compiled), ('manifest', args.manifest),
                                                   ('preparation_audit', args.preparation_audit)]})
    print(json.dumps(receipt, ensure_ascii=False))
    return receipt


if __name__ == '__main__':
    main()
