"""CE-GRA-012: a new graph audit sees its own reconstruction lineage."""
from copy import deepcopy

import pytest

from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.audit import (
    PROTOCOL_VERSION, build_graph_audit_request,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.chain import (
    run_graph_audit_chain, validate_graph_audit_chain,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.lineage import (
    build_graph_revision_lineage, validate_graph_revision_lineage,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import (
    build_graph_reconstruction_request, run_graph_reconstruction,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_graph_reconstruction import (
    fixtures, proposed, provider_receipt,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import (
    passing_graph_response,
)


def test_CE_GRA012_fresh_audit_contains_previous_audit_proposal_execution_and_diff():
    old, audit_request, audit_response = fixtures()
    parent = run_graph_audit_chain(audit_request, lambda *args: audit_response)
    request = build_graph_reconstruction_request(audit_request, audit_response)
    reconstruction = run_graph_reconstruction(audit_request, audit_response,
        lambda *args: (proposed(request), provider_receipt()))
    new_graph = reconstruction['graph']
    lineage = build_graph_revision_lineage(parent, reconstruction, new_graph)
    assert lineage['previous_review'] == parent['rounds'][0]['response']
    assert lineage['reconstruction_response'] == reconstruction['response']
    assert lineage['program_execution_receipt'] == reconstruction['validation_receipt']
    assert lineage['actual_program_diff']['graph_changed'] is True
    version_change = next(change for change in lineage['actual_program_diff']['metadata_changes']
        if change['field'] == 'graph_version')
    assert version_change['before'] != version_change['after']
    validate_graph_revision_lineage(lineage, new_graph)

    next_request = build_graph_audit_request(new_graph,
        audit_request['audit_scope']['complete_originals_supplied'],
        processing_units=audit_request['processing_units'], reconstruction_lineage=lineage)
    assert next_request['audit_round']['is_post_reconstruction_fresh_audit'] is True
    chain = run_graph_audit_chain(next_request,
        lambda task, payload, schema: passing_graph_response(payload))
    assert validate_graph_audit_chain(chain)['state'] == 'AUTO_GRAPH_AUDIT_PASS'

    # Historical 0.1 request/response replay remains byte-compatible.
    legacy_request = build_graph_audit_request(old,
        audit_request['audit_scope']['complete_originals_supplied'],
        processing_units=audit_request['processing_units'], protocol_version='checkpoint-candidate-reconstruction-audit-0.1.0')
    legacy = run_graph_audit_chain(legacy_request,
        lambda task, payload, schema: passing_graph_response(payload))
    assert legacy['format'] == 'candidate-reconstruction-audit-chain-0.1.0'
    assert validate_graph_audit_chain(legacy)['state'] == 'AUTO_GRAPH_AUDIT_PASS'


def test_CE_GRA012_lineage_cannot_be_rebound_or_introduced_into_legacy_request():
    _, audit_request, audit_response = fixtures()
    parent = run_graph_audit_chain(audit_request, lambda *args: audit_response)
    request = build_graph_reconstruction_request(audit_request, audit_response)
    reconstruction = run_graph_reconstruction(audit_request, audit_response,
        lambda *args: (proposed(request), provider_receipt()))
    new_graph = reconstruction['graph']
    lineage = build_graph_revision_lineage(parent, reconstruction, new_graph)
    changed = deepcopy(lineage)
    changed['previous_review']['rationale'] = '篡改历史'
    with pytest.raises(ValueError, match='LINEAGE_HASH_MISMATCH'):
        validate_graph_revision_lineage(changed, new_graph)
    with pytest.raises(ValueError, match='LEGACY_CANNOT_ADD_LINEAGE'):
        build_graph_audit_request(new_graph, audit_request['audit_scope']['complete_originals_supplied'],
            processing_units=audit_request['processing_units'], reconstruction_lineage=lineage,
            protocol_version='checkpoint-candidate-reconstruction-audit-0.1.0')
