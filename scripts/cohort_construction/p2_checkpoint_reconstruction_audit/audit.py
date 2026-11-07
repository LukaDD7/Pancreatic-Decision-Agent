"""Independent candidate-graph semantics audit; no discovery conversation state."""
from copy import deepcopy
import re

from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import normalize_sources
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_auto_checkpoint_v04.provider_diagnostics import (
    DiagnosticValueError, schema_diagnostic,
)

PROTOCOL_VERSION = 'checkpoint-candidate-reconstruction-audit-0.2.0'
LEGACY_PROTOCOL_VERSIONS = ('checkpoint-candidate-reconstruction-audit-0.1.0',)
TASK = 'checkpoint_candidate_reconstruction_independent_audit'
DIMENSIONS = ('source_coverage', 'radical_pathway_relevance', 'event_identity',
              'comparison_and_evolution', 'relations_and_episode',
              'program_assembly_fidelity', 'time_and_visibility')
STATUSES = ('AUTO_GRAPH_AUDIT_PASS', 'AUTO_GRAPH_AUDIT_REVISE', 'AUTO_GRAPH_AUDIT_HOLD')
SEVERITIES = ('BLOCKING', 'NON_BLOCKING')
FINDING_STATUSES = ('OPEN', 'RESOLVED', 'NOT_APPLICABLE')
DIMENSION_STATUSES = ('RESOLVED', 'OPEN', 'NOT_APPLICABLE')
APPLICABILITY = ('APPLICABLE', 'NOT_APPLICABLE', 'UNKNOWN')
OPERATIONS = ('REQUEST_SOURCE_COVERAGE', 'REQUEST_EVENT_RECONSTRUCTION',
              'REQUEST_RELATION_RECONSTRUCTION', 'REQUEST_EPISODE_RECONSTRUCTION',
              'REQUEST_PROGRAM_REPAIR', 'REQUEST_HUMAN_REVIEW')
REQUEST_KINDS = ('SOURCE_COVERAGE', 'EVENT_RECONSTRUCTION', 'RELATION_RECONSTRUCTION',
                 'EPISODE_RECONSTRUCTION', 'PROGRAM_REPAIR', 'HUMAN_REVIEW')


def _object_list(value, name):
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, dict) for item in value):
        raise ValueError(name)
    return deepcopy(value)


def _quote_evidence(value, sources, *, response_path=()):
    if not isinstance(value, list):
        raise ValueError('GRAPH_AUDIT_EVIDENCE_LIST_REQUIRED')
    result = []
    for index, item in enumerate(value):
        path = [*response_path, index]
        if not isinstance(item, dict):
            raise ValueError('GRAPH_AUDIT_EVIDENCE_OBJECT_REQUIRED')
        sid, quote = item.get('source_id'), item.get('exact_quote')
        if sid not in sources or not isinstance(quote, str) or not quote:
            raise ValueError('GRAPH_AUDIT_EVIDENCE_SOURCE_UNKNOWN')
        text = sources[sid]['text']
        count = text.count(quote)
        if count != 1:
            normalized = re.sub(r'\s+', '', quote)
            compact = re.sub(r'\s+', '', text)
            normalized_count = compact.count(normalized) if normalized else 0
            diagnostic = dict(kind='EVIDENCE_QUOTE', code='GRAPH_AUDIT_EVIDENCE_QUOTE_NOT_UNIQUE',
                instance_path=[*path, 'exact_quote'], source_id=sid,
                source_sha256=sources[sid]['sha256'], exact_quote=quote,
                exact_match_count=count, normalized_match_count=normalized_count,
                whitespace_only_candidate_match=bool(normalized and normalized_count == 1),
                auto_repair_permitted=False)
            raise DiagnosticValueError('GRAPH_AUDIT_EVIDENCE_QUOTE_NOT_UNIQUE', [diagnostic])
        start = text.index(quote)
        result.append(dict(source_id=sid, source_sha256=sources[sid]['sha256'],
            exact_quote=quote, start=start, end=start + len(quote),
            source_version=sources[sid]['source_version'], verbatim_verified=True))
    return result


def build_graph_audit_request(graph, sources, *, discovery_proposals=(),
                              review_proposals=(), feedback_history=(),
                              processing_units=None, unit_inventory=None,
                              source_route_receipts=(), final_graph_audit=None,
                              additional_excluded_units=(), reconstruction_lineage=None,
                              protocol_version=PROTOCOL_VERSION):
    """Freeze declared source scope, upstream proposals and assembled graph.

    No input field is promoted to clinical fact. ``complete_originals_supplied``
    covers only the listed source objects, never the whole patient chart.
    """
    if not isinstance(graph, dict) or graph.get('format') != 'candidate-reconstruction-graph-snapshot-0.1.0':
        raise ValueError('GRAPH_AUDIT_CANDIDATE_GRAPH_REQUIRED')
    if canonical_sha256({k: v for k, v in graph.items() if k != 'graph_sha256'}) != graph.get('graph_sha256'):
        raise ValueError('GRAPH_AUDIT_GRAPH_HASH_MISMATCH')
    if protocol_version not in (PROTOCOL_VERSION, *LEGACY_PROTOCOL_VERSIONS):
        raise ValueError('GRAPH_AUDIT_PROTOCOL_VERSION_INVALID')
    if reconstruction_lineage is not None:
        if protocol_version != PROTOCOL_VERSION:
            raise ValueError('GRAPH_AUDIT_LEGACY_CANNOT_ADD_LINEAGE')
        from .lineage import validate_graph_revision_lineage
        reconstruction_lineage = validate_graph_revision_lineage(reconstruction_lineage, graph)
    normalized = normalize_sources(sources)
    indexed = {source['source_id']: source for source in normalized}
    units = graph.get('discovery_units', []) if processing_units is None else processing_units
    units = _object_list(units, 'GRAPH_AUDIT_PROCESSING_UNITS_REQUIRED')
    covered = set()
    for unit in units:
        ids = unit.get('source_ids')
        if not isinstance(ids, list) or any(sid not in indexed for sid in ids):
            raise ValueError('GRAPH_AUDIT_DECLARED_UNIT_SOURCE_MISSING')
        covered.update(ids)
    uncovered = sorted(set(indexed) - covered)
    request = dict(format='candidate-reconstruction-audit-request-0.2.0' if protocol_version == PROTOCOL_VERSION
            else 'candidate-reconstruction-audit-request-0.1.0',
        task=TASK, protocol_version=protocol_version,
        audit_scope=dict(scope_kind='DECLARED_PROCESSING_UNITS_AND_SUPPLIED_ORIGINALS',
            whole_patient_chart_certified=False,
            complete_originals_supplied=[dict(source_id=s['source_id'], source_version=s['source_version'],
                sha256=s['sha256'], text=s['text']) for s in normalized],
            uncovered_supplied_source_ids=uncovered,
            source_coverage_complete=covered == set(indexed),
            absence_of_unsupplied_records_is_unknown=True),
        processing_units=units,
        zero_discovery_and_rejected_units=deepcopy(graph.get('discovery_units', [])),
        additional_excluded_units=_object_list(additional_excluded_units, 'GRAPH_AUDIT_EXCLUDED_UNITS_REQUIRED'),
        unit_inventory=deepcopy(graph.get('unit_inventory') if unit_inventory is None else unit_inventory),
        discovery_llm_proposals=_object_list(discovery_proposals, 'GRAPH_AUDIT_DISCOVERY_PROPOSALS_REQUIRED'),
        review_llm_proposals=_object_list(review_proposals, 'GRAPH_AUDIT_REVIEW_PROPOSALS_REQUIRED'),
        feedback_history=_object_list(feedback_history, 'GRAPH_AUDIT_FEEDBACK_REQUIRED'),
        candidate_graph=deepcopy(graph),
        source_route_receipts=_object_list(source_route_receipts, 'GRAPH_AUDIT_SOURCE_ROUTES_REQUIRED'),
        final_graph_audit=deepcopy(graph.get('final_graph_audit') if final_graph_audit is None else final_graph_audit),
        candidate_graph_is_not_clinical_reference=True,
        upstream_labels_are_not_ground_truth=True,
        clinical_visibility_certified=False)
    if reconstruction_lineage is not None:
        request['reconstruction_lineage'] = deepcopy(reconstruction_lineage)
        request['audit_round'] = dict(is_post_reconstruction_fresh_audit=True,
            current_state_produced_by_previous_reconstruction=True)
    request['response_binding'] = dict(protocol_version=protocol_version,
        graph_sha256=canonical_sha256(graph), request_sha256=canonical_sha256(request))
    return request


def validate_graph_audit(request, response):
    """Require complete dimensions, exact binding and verbatim local evidence."""
    if not isinstance(response, dict):
        raise ValueError('GRAPH_AUDIT_RESPONSE_OBJECT_REQUIRED')
    _validate_schema(response, _graph_audit_schema(request['protocol_version']))
    binding = request['response_binding']
    if any(response.get(key) != binding[key] for key in binding):
        raise ValueError('GRAPH_AUDIT_RESPONSE_BINDING_MISMATCH')
    if response.get('reviewer_type') != 'llm_candidate_reconstruction_audit':
        raise ValueError('GRAPH_AUDIT_REVIEWER_TYPE_INVALID')
    if response.get('target') != {'graph_sha256': binding['graph_sha256']}:
        raise ValueError('GRAPH_AUDIT_TARGET_MISMATCH')
    sources = {s['source_id']: s for s in request['audit_scope']['complete_originals_supplied']}
    for index, finding in enumerate(response['findings']):
        _quote_evidence(finding['evidence'], sources,
            response_path=['findings', index, 'evidence'])
    for index, revision in enumerate(response['revision_requests']):
        _quote_evidence(revision['evidence'], sources,
            response_path=['revision_requests', index, 'evidence'])
    dimensions = [item['dimension'] for item in response['dimensions']]
    if len(dimensions) != len(set(dimensions)) or set(dimensions) != set(DIMENSIONS):
        raise ValueError('GRAPH_AUDIT_DIMENSIONS_MISMATCH')
    status = response['status']
    if status == 'AUTO_GRAPH_AUDIT_PASS':
        if (not request['audit_scope']['source_coverage_complete']
                or any(item['status'] != 'RESOLVED' or item['applicability'] != 'APPLICABLE'
                       for item in response['dimensions'])
                or any(item['status'] == 'OPEN' and item['severity'] == 'BLOCKING' for item in response['findings'])
                or response['revision_requests'] or response['required_actions']):
            raise ValueError('GRAPH_AUDIT_EMPTY_OR_UNRESOLVED_PASS')
    elif status == 'AUTO_GRAPH_AUDIT_REVISE' and not response['revision_requests']:
        raise ValueError('GRAPH_AUDIT_REVISE_REQUIRES_REQUEST')
    elif status == 'AUTO_GRAPH_AUDIT_HOLD' and not (
            response['required_actions'] or response['revision_requests']
            or any(item['status'] == 'OPEN' and item['severity'] == 'BLOCKING' for item in response['findings'])
            or any(item['status'] == 'OPEN' or item['applicability'] == 'UNKNOWN' for item in response['dimensions'])):
        raise ValueError('GRAPH_AUDIT_HOLD_REQUIRES_REASON')
    return response


def _graph_audit_schema(protocol_version):
    schema = deepcopy(GRAPH_AUDIT_SCHEMA)
    schema['properties']['protocol_version'] = {'const': protocol_version}
    if protocol_version in LEGACY_PROTOCOL_VERSIONS:
        schema['properties']['format']['const'] = 'candidate-reconstruction-audit-0.1.0'
    return schema


def reconstruction_handoff(request, response, *, output_graph=None):
    """Return an explicit request, never modify a graph in the audit layer."""
    response = validate_graph_audit(request, deepcopy(response))
    new_graph_required = response['status'] != 'AUTO_GRAPH_AUDIT_PASS' or output_graph is not None
    result = dict(format='candidate-reconstruction-revision-handoff-0.1.0',
        audit_status=response['status'], input_graph_sha256=request['response_binding']['graph_sha256'],
        revision_requests=deepcopy(response['revision_requests']),
        required_actions=deepcopy(response['required_actions']),
        new_graph_supplied=output_graph is not None,
        fresh_graph_material_and_delivery_reaudit_required=new_graph_required,
        old_material_pass_transferable=False, clinical_release_permitted=False)
    if output_graph is not None:
        result['output_graph_sha256'] = canonical_sha256(output_graph)
    return result


def _validate_schema(value, schema):
    from jsonschema import Draft202012Validator, ValidationError
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        raise DiagnosticValueError('GRAPH_AUDIT_SCHEMA_INVALID',
            [schema_diagnostic(exc, schema, value)]) from exc


_EVIDENCE = {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
    'required': ['source_id', 'exact_quote'],
    'properties': {'source_id': {'type': 'string', 'minLength': 1},
                   'exact_quote': {'type': 'string', 'minLength': 1}}}}
_FINDING = {'type': 'object', 'additionalProperties': False,
    'required': ['finding_id', 'dimension', 'severity', 'status', 'reason', 'evidence',
                 'affected_event_ids', 'affected_episode_ids'],
    'properties': {'finding_id': {'type': 'string', 'minLength': 1},
        'dimension': {'enum': list(DIMENSIONS)}, 'severity': {'enum': list(SEVERITIES)},
        'status': {'enum': list(FINDING_STATUSES)}, 'reason': {'type': 'string', 'minLength': 1},
        'evidence': _EVIDENCE,
        'affected_event_ids': {'type': 'array', 'items': {'type': 'string'}},
        'affected_episode_ids': {'type': 'array', 'items': {'type': 'string'}}}}
_REVISION = {'type': 'object', 'additionalProperties': False,
    'required': ['request_id', 'request_kind', 'reason', 'affected_event_ids',
                 'affected_episode_ids', 'evidence', 'required_reconstruction'],
    'properties': {'request_id': {'type': 'string', 'minLength': 1},
        'request_kind': {'enum': list(REQUEST_KINDS)}, 'reason': {'type': 'string', 'minLength': 1},
        'affected_event_ids': {'type': 'array', 'items': {'type': 'string'}},
        'affected_episode_ids': {'type': 'array', 'items': {'type': 'string'}},
        'evidence': _EVIDENCE, 'required_reconstruction': {'type': 'boolean'}}}
GRAPH_AUDIT_SCHEMA = {'$schema': 'https://json-schema.org/draft/2020-12/schema',
    'type': 'object', 'additionalProperties': False,
    'required': ['format', 'review_id', 'target', 'protocol_version', 'graph_sha256',
                 'request_sha256', 'reviewer_type', 'dimensions', 'findings',
                 'revision_requests', 'status', 'required_actions'],
    'properties': {'format': {'const': 'candidate-reconstruction-audit-0.2.0'},
        'review_id': {'type': 'string', 'minLength': 1},
        'target': {'type': 'object', 'additionalProperties': False,
            'required': ['graph_sha256'], 'properties': {'graph_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}},
        'protocol_version': {'enum': [PROTOCOL_VERSION, *LEGACY_PROTOCOL_VERSIONS]},
        'graph_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'request_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'reviewer_type': {'const': 'llm_candidate_reconstruction_audit'},
        'dimensions': {'type': 'array', 'minItems': len(DIMENSIONS), 'maxItems': len(DIMENSIONS),
            'items': {'type': 'object', 'additionalProperties': False,
                'required': ['dimension', 'applicability', 'status', 'reason'],
                'properties': {'dimension': {'enum': list(DIMENSIONS)},
                    'applicability': {'enum': list(APPLICABILITY)},
                    'status': {'enum': list(DIMENSION_STATUSES)},
                    'reason': {'type': 'string', 'minLength': 1}}}},
        'findings': {'type': 'array', 'items': _FINDING},
        'revision_requests': {'type': 'array', 'items': _REVISION},
        'status': {'enum': list(STATUSES)},
        'required_actions': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}}}}
