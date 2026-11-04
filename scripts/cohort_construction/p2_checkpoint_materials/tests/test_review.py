import copy
import json

import pytest

from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError, freeze_checkpoint_packet
from scripts.cohort_construction.p2_checkpoint_packet.semantic_audit import revise_partition, validate_semantic_audit
from scripts.cohort_construction.p2_checkpoint_materials.review import create_review_draft, main
from scripts.cohort_construction.p2_checkpoint_materials.tests.test_prepare import event, prepare, source


def test_review_draft_is_explicitly_unresolved_preparation_not_independent_audit():
    s = source()
    manifest, proposal, compiled, preparation_audit = prepare(event(s), [s, source('outside', '范围外虚构原文')])
    mask = dict(next(p for p in compiled['sources'][0]['segments'] if p['disposition'] == 'MASK'), source_id='s1')
    draft = create_review_draft(compiled, manifest, preparation_audit,
        reviewer_id='DRAFT-ONLY', focus_source_id='s1', proposed_mask_spans=[mask])
    assert draft['reviewer_type'] == 'research_preparation'
    assert draft['status'] == 'PENDING' and draft['independence_attested'] is False
    assert all(not checked for checked in draft['checks'].values())
    assert draft['review_scope']['scope_status'] == 'INCOMPLETE'
    assert all(item['disposition'] == 'UNRESOLVED' for item in draft['source_coverage_assessments'])
    assert any(item['assessment'] == 'UNRESOLVED' for item in draft['segment_assessments'])
    assert validate_semantic_audit(compiled, draft)['status'] == 'PENDING'
    with pytest.raises(ContractError, match='UNRESOLVED_REVIEW_SEGMENT'):
        freeze_checkpoint_packet(compiled, draft, {}, packet_id='never', packet_version='0')


def test_review_draft_does_not_retype_preparation_as_independent_pass():
    s = source()
    manifest, _, compiled, preparation_audit = prepare(event(s), [s])
    draft = create_review_draft(compiled, manifest, preparation_audit, reviewer_id='DRAFT-ONLY')
    for status, attested in [('PASS', False), ('PASS', True), ('PENDING', True)]:
        invalid = copy.deepcopy(draft)
        invalid.update(status=status, independence_attested=attested)
        with pytest.raises(ContractError, match='RESEARCH_PREPARATION_IS_NOT_INDEPENDENT_REVIEW'):
            validate_semantic_audit(compiled, invalid)


def test_draft_revision_is_new_researcher_partition_with_review_retained():
    s = source()
    manifest, proposal, compiled, preparation_audit = prepare(event(s), [s])
    mask = dict(next(p for p in compiled['sources'][0]['segments'] if p['disposition'] == 'MASK'), source_id='s1')
    draft = create_review_draft(compiled, manifest, preparation_audit,
        reviewer_id='DRAFT-ONLY', focus_source_id='s1', proposed_mask_spans=[mask])
    original = copy.deepcopy((manifest, proposal, compiled))
    revised = revise_partition(manifest, proposal, draft, compiled=compiled)
    assert (manifest, proposal, compiled) == original
    assert revised['semantic_audit_status'] == 'NOT_ASSESSED'
    assert [p['disposition'] for p in revised['sources'][0]['segments']] == ['REVIEW', 'MASK', 'REVIEW']
    with pytest.raises(ContractError, match='DRAFT_MASK_PROPOSAL_MUST_MATCH_FOCUS_MASK_SPAN'):
        create_review_draft(compiled, manifest, preparation_audit,
            reviewer_id='DRAFT-ONLY', proposed_mask_spans=[mask])


def test_review_cli_writes_pending_receipt_and_refuses_overwrite(tmp_path, capsys):
    s = source()
    manifest, _, compiled, preparation_audit = prepare(event(s), [s])
    for name, value in [('compiled', compiled), ('manifest', manifest), ('audit', preparation_audit)]:
        (tmp_path / f'{name}.json').write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    args = ['--compiled', str(tmp_path / 'compiled.json'), '--manifest', str(tmp_path / 'manifest.json'),
            '--preparation-audit', str(tmp_path / 'audit.json'), '--output', str(tmp_path / 'review.json')]
    result = main(args)
    assert result['status'] == 'AI_REVIEW_DRAFT_PENDING_NOT_INDEPENDENT_AUDIT'
    assert result['model_api_calls'] == 0 and result['doctor_packet_created'] is False
    assert json.loads((tmp_path / 'review.json').read_text())['reviewer_type'] == 'research_preparation'
    with pytest.raises(ValueError, match='REVIEW_DRAFT_REQUIRES_NEW_OUTPUT'):
        main(args)
