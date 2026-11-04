"""Synthetic round-trip and tamper controls for compact graph-audit transport."""
from copy import deepcopy
import json

import pytest

from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import fictional_graph
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.audit import build_graph_audit_request
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.chain import (
    run_graph_audit_chain, validate_graph_audit_chain,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.prompts import (
    GRAPH_AUDIT_INSTRUCTIONS, graph_audit_client,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.projection import (
    compact_graph_audit_request, expand_graph_audit_request,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import passing_graph_response


def repeated_request():
    graph, sources = fictional_graph()
    event = graph['events'][0]
    blocks = [dict(source_id='DOC-01', text='虚构条件', quote='虚构条件', start=0, end=4,
        repeated='x' * 400)]
    event['actual_model_evidence'] = deepcopy(blocks)
    event['evidence_context']['blocks'] = deepcopy(blocks)
    event['evidence_context']['ledger'] = [dict(source_id='DOC-01', repeated='y' * 400)]
    event['evidence_context']['source_ledger'] = deepcopy(event['evidence_context']['ledger'])
    graph['graph_sha256'] = canonical_sha256({k: v for k, v in graph.items() if k != 'graph_sha256'})
    units = [dict(unit_id='ALL', source_ids=[source['source_id'] for source in sources])]
    return build_graph_audit_request(graph, sources, processing_units=units,
        discovery_proposals=[dict(unit_id='ALL', events=deepcopy(graph['events']))],
        review_proposals=deepcopy(graph['events']))


def test_CE_GRA006_compact_roundtrip_retains_all_originals_events_and_proposals():
    request = repeated_request()
    compact = compact_graph_audit_request(request)
    assert compact['format'] == 'candidate-reconstruction-audit-compact-json-0.2.0'
    assert len(json.dumps(compact, ensure_ascii=False)) < len(json.dumps(request, ensure_ascii=False))
    restored = expand_graph_audit_request(compact)
    assert restored == request
    assert restored['audit_scope']['complete_originals_supplied'] == request['audit_scope']['complete_originals_supplied']
    assert restored['candidate_graph'] == request['candidate_graph']
    assert restored['review_llm_proposals'] == request['review_llm_proposals']
    assert restored['discovery_llm_proposals'] == request['discovery_llm_proposals']
    assert compact['payload']['response_binding']['protocol_version'] == request['response_binding']['protocol_version']


def test_CE_GRA006_compact_tampering_cannot_change_sources_graph_or_hashes():
    compact = compact_graph_audit_request(repeated_request())
    changed = deepcopy(compact)
    changed['payload']['audit_scope']['complete_originals_supplied'][0]['text'] += 'tampered'
    with pytest.raises(ValueError, match='ORIGINAL_HASH_MISMATCH'):
        expand_graph_audit_request(changed)
    changed = deepcopy(compact)
    key = next(iter(changed['blocks']))
    changed['blocks'][key] = ['tampered']
    with pytest.raises(ValueError, match='BLOCK_HASH_MISMATCH|ORIGINAL_HASH_MISMATCH'):
        expand_graph_audit_request(changed)
    changed = deepcopy(compact)
    changed['payload']['review_llm_proposals'] = [{'compact_event_ref': 'unknown'}]
    with pytest.raises(ValueError, match='ORIGINAL_HASH_MISMATCH'):
        expand_graph_audit_request(changed)


def test_CE_GRA006_compact_missing_and_cyclic_references_fail_closed():
    compact = compact_graph_audit_request(repeated_request())
    changed = deepcopy(compact)
    key = next(iter(changed['blocks']))
    del changed['blocks'][key]
    with pytest.raises(ValueError, match='REFERENCE_INVALID|BLOCK_HASH_MISMATCH|ORIGINAL_HASH_MISMATCH'):
        expand_graph_audit_request(changed)
    changed = deepcopy(compact)
    key = next(iter(changed['blocks']))
    changed['blocks'][key] = {'$compact_json_ref': key}
    with pytest.raises(ValueError, match='REFERENCE_INVALID|BLOCK_HASH_MISMATCH|ORIGINAL_HASH_MISMATCH'):
        expand_graph_audit_request(changed)
    changed = deepcopy(compact)
    changed['projection_scope'] = 'SOURCE_SCOPE_REDUCTION'
    with pytest.raises(ValueError, match='SCOPE_INVALID'):
        expand_graph_audit_request(changed)


def test_CE_GRA006_real_graph_adapter_preserves_compact_payload_and_instructions():
    sent = []
    class Client:
        def ask(self, task, payload, schema, system_instructions):
            sent.append((task, payload, system_instructions))
            return passing_graph_response(expand_graph_audit_request(payload))
    request = repeated_request()
    answer = graph_audit_client(Client())('task', compact_graph_audit_request(request), {})
    assert answer['status'] == 'AUTO_GRAPH_AUDIT_PASS'
    assert expand_graph_audit_request(sent[0][1]) == request
    assert sent[0][2] == GRAPH_AUDIT_INSTRUCTIONS
    assert '无损JSON去重投影' in sent[0][2]
    assert 'response_binding中的protocol_version、graph_sha256、request_sha256仍逐字取自payload的response_binding' in sent[0][2]


def test_CE_GRA006_chain_persists_full_request_not_compact_provider_payload():
    request = repeated_request()
    sent = []
    def ask(task, payload, schema):
        sent.append(payload)
        return passing_graph_response(request)
    chain = run_graph_audit_chain(request, ask, compact_transport=True)
    assert sent[0] != request and sent[0]['format'].endswith('compact-json-0.2.0')
    assert expand_graph_audit_request(sent[0]) == request
    assert chain['initial_request'] == request
    assert chain['rounds'][0]['request'] == request
    assert validate_graph_audit_chain(chain)['state'] == 'AUTO_GRAPH_AUDIT_PASS'
    with pytest.raises(ValueError, match='FLAG_INVALID'):
        run_graph_audit_chain(request, ask, compact_transport='yes')
