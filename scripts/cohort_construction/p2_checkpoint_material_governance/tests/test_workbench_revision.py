"""V061-GAP-01: actual revision to pending researcher assignment and recovery."""
from copy import deepcopy

import pytest

from scripts.cohort_construction.p2_checkpoint_material_governance.workbench import (
    build_auto_review_assignment, derive_revision_preparation, validate_auto_review_opinion,
    receive_auto_review_submission,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_chain_validation import fictional_chain
from scripts.cohort_construction.p2_checkpoint_material_review_app.build import validate_assignment
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256


def test_revision_derives_provenance_preserving_preparation_and_pending_draft():
    documents, sources, chain, _ = fictional_chain(include_source=True, graph=True)
    original = deepcopy(documents['preparation_audit'])
    package = build_auto_review_assignment(chain, documents['draft'], original,
        assignment_id='SYNTHETIC-REVISED', selected_event=documents['selected_event'], coverage_sources=sources)
    docs = validate_assignment(package['assignment'])['documents']
    prep, draft = docs['preparation_audit'], docs['draft']
    assert prep['original_preparation_audit'] == original == documents['preparation_audit']
    assert prep['citation_hints'] == original['citation_hints']
    assert prep['issues'] == original['issues']
    assert prep['event_sha256'] == original['event_sha256']
    assert prep['manifest_sha256'] == canonical_sha256(chain['manifest'])
    assert prep['compiled_sha256'] != original['compiled_sha256']
    assert set(draft['review_scope']['included_source_ids']) == {s['source_id'] for s in chain['manifest']['sources']}
    assert draft['status'] == 'PENDING' and draft['independence_attested'] is False
    assert all(s['assessment'] == 'UNRESOLVED' for s in draft['segment_assessments'])
    assert validate_auto_review_opinion(package['assignment'], package['auto_review'])


def test_revised_human_export_recovers_new_hash_and_rejects_old_response():
    documents, sources, chain, _ = fictional_chain(include_source=True)
    package = build_auto_review_assignment(chain, documents['draft'], documents['preparation_audit'],
        assignment_id='SYNTHETIC-REVISED', coverage_sources=sources)
    assignment = package['assignment'];draft = validate_assignment(assignment)['documents']['draft']
    draft.update(reviewer_type='human', reviewer_id='FICTIONAL-HUMAN', assessment_id='FICTIONAL-AUDIT', assessed_at='2026-10-29T00:00:00Z')
    response = dict(format='checkpoint-researcher-material-response-0.1.0',
        assignment_id=assignment['assignment_id'], assignment_sha256=assignment['assignment_sha256'],
        input_bindings=deepcopy(assignment['bindings']), audit=draft, audit_sha256=canonical_sha256(draft))
    recovered = receive_auto_review_submission(package, response)
    assert recovered['compiled_sha256'] == chain['compiled']['compiled_sha256']
    assert recovered['status'] == 'PENDING'
    response['audit']['compiled_sha256'] = documents['compiled']['compiled_sha256']
    response['audit_sha256'] = canonical_sha256(response['audit'])
    with pytest.raises(Exception): receive_auto_review_submission(package, response)


def test_original_preparation_and_historical_ledger_cannot_be_rebound():
    documents, sources, chain, _ = fictional_chain()
    bad = deepcopy(documents['preparation_audit']);bad['citation_hints'] = []
    with pytest.raises(Exception, match='ORIGINAL_HASH'):
        derive_revision_preparation(chain, bad)
    package = build_auto_review_assignment(chain, documents['draft'], documents['preparation_audit'],
        assignment_id='SYNTHETIC-REVISED', coverage_sources=sources)
    package['auto_review']['chain']['rounds'][0]['ledger']['operations'] = []
    with pytest.raises(Exception): validate_auto_review_opinion(package['assignment'],package['auto_review'])
