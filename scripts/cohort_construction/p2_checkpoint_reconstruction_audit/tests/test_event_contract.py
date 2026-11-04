"""CE-GRA-011: schema-minimum responses must be consumable, or fail early."""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator, ValidationError

from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import prepare_candidate_materials
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import fictional_graph
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import (
    GRAPH_RECONSTRUCTION_SCHEMA, LEGACY_REQUEST_FORMAT, build_graph_reconstruction_request,
    run_graph_reconstruction, validate_graph_reconstruction, validate_graph_reconstruction_chain,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_graph_reconstruction import fixtures, proposed, provider_receipt


def test_CE_GRA011_missing_scope_and_four_field_responses_rejected_early():
    _, a, b = fixtures()
    request = build_graph_reconstruction_request(a, b)
    required = GRAPH_RECONSTRUCTION_SCHEMA['properties']['graph']['properties']['events']['items']['required']
    for field in required:
        response = proposed(request)
        response['graph']['events'][0].pop(field)
        with pytest.raises(ValidationError):
            Draft202012Validator(GRAPH_RECONSTRUCTION_SCHEMA).validate(response)
        with pytest.raises(ValueError, match='RESPONSE_SCHEMA_INVALID'):
            validate_graph_reconstruction(request, response)
    response = proposed(request)
    response['graph']['events'] = [{k: e[k] for k in
        ('event_id', 'source_id', 'checkpoint_status', 'action_citations')}
        for e in response['graph']['events']]
    chain = run_graph_reconstruction(a, b, lambda *args: (response, provider_receipt()))
    assert chain['state'] == 'RECONSTRUCTION_HOLD'
    assert chain['failure']['reason_code'] == 'GRAPH_RECONSTRUCTION_RESPONSE_SCHEMA_INVALID'
    assert validate_graph_reconstruction_chain(chain)['failure_replayed']


def test_CE_GRA011_schema_minimum_complete_event_assembles_without_inventing_time():
    _, sources = fictional_graph()
    _, a, b = fixtures()
    request = build_graph_reconstruction_request(a, b)
    response = proposed(request)
    fields = GRAPH_RECONSTRUCTION_SCHEMA['properties']['graph']['properties']['events']['items']['required']
    for event in response['graph']['events']:
        for key in list(event):
            if key not in fields:
                del event[key]
        event.update(choice_time=None, time_basis='unknown', time_validation='UNRESOLVED')
    original = deepcopy(response)
    Draft202012Validator(GRAPH_RECONSTRUCTION_SCHEMA).validate(response)
    graph, _ = validate_graph_reconstruction(request, response)
    assert response == original  # Raw model proposal is never rewritten.
    tasks, failures = prepare_candidate_materials(graph, sources,
        decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC-UNREVIEWED')
    assert len(tasks) == 2 and not failures
    for event in graph['events']:
        assert event['choice_time'] is None and event['time_validation'] == 'UNRESOLVED'
        assert event['evidence_context']['uncertainty']['choice_time_unknown']
    assert all(not task['task_context']['clinical_visibility_certified'] for task in tasks)


def test_CE_GRA011_source_scope_and_all_material_citations_are_verified():
    _, a, b = fixtures()
    request = build_graph_reconstruction_request(a, b)
    response = proposed(request)
    event = response['graph']['events'][0]
    event['source_ids'] = ['DOC-01', 'DOC-02']
    event['prechoice_citations']['valid'] = [dict(source_id='DOC-02', quote='虚构复评：已完成补充影像。')]
    event['prechoice_citations']['invalid'] = [dict(source_id='DOC-01', quote='不存在的历史提案')]
    graph, _ = validate_graph_reconstruction(request, response)
    current = graph['events'][0]
    assert current['source_ids'] == ['DOC-01', 'DOC-02']
    assert current['prechoice_citations']['invalid'] == event['prechoice_citations']['invalid']
    assert len(current['prechoice_citations']['valid']) == 1
    for scope, error in ((['DOC-02'], 'PRIMARY_SOURCE_NOT_DECLARED'),
                         (['DOC-01', 'UNKNOWN'], 'DECLARED_SOURCE_UNKNOWN')):
        changed = deepcopy(response)
        changed['graph']['events'][0]['source_ids'] = scope
        with pytest.raises(ValueError, match=error):
            validate_graph_reconstruction(request, changed)
    for field in ('choice_boundary_citations', 'prechoice_citations', 'postchoice_citations',
                  'validated_observation_citations'):
        changed = deepcopy(response)
        changed['graph']['events'][0][field]['valid'] = [dict(source_id='DOC-01', quote='非原文')]
        with pytest.raises(ValueError, match='MATERIAL_CITATION_NOT_VERIFIABLE'):
            validate_graph_reconstruction(request, changed)
    changed = deepcopy(response)
    changed['graph']['events'][0]['availability_validation'] = [dict(
        validated_basis=dict(valid=[dict(source_id='UNKNOWN', quote='未知原文')], invalid=[], ambiguous=[]))]
    with pytest.raises(ValueError, match='MATERIAL_CITATION_NOT_VERIFIABLE'):
        validate_graph_reconstruction(request, changed)


def test_CE_GRA011_legacy_event_contract_replays_without_upgrading_history():
    _, a, b = fixtures()
    request = build_graph_reconstruction_request(a, b, legacy=True)
    response = proposed(request)
    for event in response['graph']['events']:
        event.pop('source_ids')
    graph, receipt = validate_graph_reconstruction(request, response)
    chain = run_graph_reconstruction(a, b,
        lambda *args: (proposed(build_graph_reconstruction_request(a, b)), provider_receipt()))
    chain.update(request=request, response=response, graph=graph, validation_receipt=receipt)
    chain['provider_response_binding']['response_content_sha256'] = canonical_sha256(response)
    chain['chain_sha256'] = canonical_sha256({k: v for k, v in chain.items() if k != 'chain_sha256'})
    before = deepcopy(chain)
    assert chain['request']['format'] == LEGACY_REQUEST_FORMAT
    assert validate_graph_reconstruction_chain(chain, expected_request=(a, b))['replay_verified']
    assert chain == before and 'source_ids' not in chain['graph']['events'][0]
    with pytest.raises(ValueError, match='RESPONSE_SCHEMA_INVALID'):
        validate_graph_reconstruction(build_graph_reconstruction_request(a, b), response)
