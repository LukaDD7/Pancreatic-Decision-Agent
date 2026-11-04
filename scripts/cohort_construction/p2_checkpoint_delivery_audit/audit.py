"""Independent audit of Episode material and actual derived delivery artifacts."""
from copy import deepcopy
import hashlib
import json
import re
from html.parser import HTMLParser

from scripts.cohort_construction.p2_checkpoint_material_governance.episode import validate_episode_material_set
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256

PROTOCOL_VERSION = 'checkpoint-episode-delivery-audit-0.1.0'
TASK = 'checkpoint_episode_and_actual_fhir_html_independent_audit'
DIMENSIONS = ('episode_membership', 'cross_checkpoint_boundaries', 'evidence_preservation',
              'current_choice_and_future_leakage', 'source_uncertainty',
              'actual_fhir_html_fidelity', 'response_binding_and_evaluability')
STATUSES = ('AUTO_DELIVERY_AUDIT_PASS', 'AUTO_DELIVERY_AUDIT_REVISE', 'AUTO_DELIVERY_AUDIT_HOLD')
SEVERITIES = ('BLOCKING', 'NON_BLOCKING')
FINDING_STATUSES = ('OPEN', 'RESOLVED', 'NOT_APPLICABLE')
DIMENSION_STATUSES = ('RESOLVED', 'OPEN', 'NOT_APPLICABLE')
APPLICABILITY = ('APPLICABLE', 'NOT_APPLICABLE', 'UNKNOWN')
REVISION_KINDS = ('GRAPH_RECONSTRUCTION', 'MATERIAL_RECOMPILATION',
                  'FHIR_RENDERER_REPAIR', 'HTML_RENDERER_REPAIR', 'HUMAN_REVIEW')


class _DOMInventory(HTMLParser):
    """Read actual markup; script/style contents remain in serialized HTML."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.links = []
        self._visible = []
        self._hidden_depth = 0
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.elements.append(dict(tag=tag, attributes=deepcopy(values)))
        for key in ('src', 'href', 'xlink:href', 'formaction'):
            if key in values:
                self.links.append(dict(tag=tag, attribute=key, value=values[key]))
        if tag in ('script', 'style', 'template', 'noscript') or 'hidden' in values:
            self._hidden_depth += 1
    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)
    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'template', 'noscript') and self._hidden_depth:
            self._hidden_depth -= 1
    def handle_data(self, data):
        if not self._hidden_depth and data.strip():
            self._visible.append(data)


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _actual_delivery(deliveries, *, expected_materials=None, episode_id=None):
    if not isinstance(deliveries, list):
        raise ValueError('DELIVERY_AUDIT_ACTUAL_ARTIFACT_LIST_REQUIRED')
    result = []
    for delivery in deliveries:
        if not isinstance(delivery, dict):
            raise ValueError('DELIVERY_AUDIT_ARTIFACT_OBJECT_REQUIRED')
        checkpoint_id = delivery.get('checkpoint_id')
        packet, bundle, html = delivery.get('packet'), delivery.get('bundle'), delivery.get('html')
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ValueError('DELIVERY_AUDIT_CHECKPOINT_ID_REQUIRED')
        if not isinstance(packet, dict) or not isinstance(bundle, dict) or not isinstance(html, str) or not html:
            raise ValueError('DELIVERY_AUDIT_ACTUAL_PACKET_BUNDLE_HTML_REQUIRED')
        from scripts.cohort_construction.p2_checkpoint_fhir.doctor import packet_from_doctor_bundle
        try:
            packet_from_doctor_bundle(bundle, expected_packet_sha256=packet['packet_sha256'])
        except Exception as exc:
            if expected_materials is not None:
                raise ValueError('DELIVERY_AUDIT_PACKET_FHIR_IDENTITY_MISMATCH') from exc
            identity_error = 'DELIVERY_AUDIT_PACKET_FHIR_IDENTITY_MISMATCH'
        else:
            identity_error = None
        if expected_materials is not None:
            if checkpoint_id not in expected_materials:
                raise ValueError('DELIVERY_AUDIT_TARGET_NOT_IN_MATERIAL_SET')
            expected = expected_materials[checkpoint_id]
            if (packet['target'] != expected['compiled']['target']
                    or packet['provenance']['compiled_partition_sha256'] != expected['compiled']['compiled_sha256']
                    or [{key: value for key, value in material.items() if key != 'material_id'}
                        for material in packet['materials']] != expected['doctor_view']['materials']):
                raise ValueError('DELIVERY_AUDIT_COMPILED_PACKET_TARGET_MISMATCH')
            if episode_id is not None:
                episode_extension = any(extension.get('url', '').endswith('/episode-id')
                    and extension.get('valueString') == episode_id
                    for entry in bundle.get('entry', [])
                    for extension in entry.get('resource', {}).get('extension', []))
                if not episode_extension:
                    raise ValueError('DELIVERY_AUDIT_EPISODE_ID_MISMATCH')
        assignment_id = delivery.get('assignment_id')
        if expected_materials is not None and (not isinstance(assignment_id, str) or not assignment_id.strip()):
            raise ValueError('DELIVERY_AUDIT_STAGING_ASSIGNMENT_ID_REQUIRED')
        if assignment_id is not None and expected_materials is not None:
            from scripts.cohort_construction.p2_checkpoint_review_app.build import build_reviewer_html
            expected_html = build_reviewer_html(bundle, assignment_id=assignment_id,
                expected_packet_sha256=packet['packet_sha256'])
            if html != expected_html:
                raise ValueError('DELIVERY_AUDIT_HTML_PACKET_ASSIGNMENT_MISMATCH')
        bundle_bytes = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        html_bytes = html.encode('utf-8')
        parser = _DOMInventory()
        parser.feed(html)
        result.append(dict(checkpoint_id=checkpoint_id, assignment_id=assignment_id,
            packet_sha256=canonical_sha256(packet), fhir_bundle_sha256=_hash(bundle_bytes),
            html_sha256=_hash(html_bytes), packet=deepcopy(packet), bundle=deepcopy(bundle),
            html=html, dom=dict(elements=parser.elements, links=parser.links,
                visible_text='\n'.join(parser._visible)),
            delivery_gate=dict(staging_only=delivery.get('staging_only') is True,
                identity_error=identity_error,
                doctor_assignment_created=delivery.get('doctor_assignment_created') is True,
                external_release_authorized=delivery.get('external_release_authorized') is True,
                actual_release_permitted=False)))
    if len({item['checkpoint_id'] for item in result}) != len(result):
        raise ValueError('DELIVERY_AUDIT_DUPLICATE_CHECKPOINT')
    return result


def build_delivery_audit_request(material_set, deliveries, *, researcher_originals=()):
    """Bind whole Episode material to actual private staging artifacts."""
    if not isinstance(material_set, dict) or not isinstance(material_set.get('set_sha256'), str):
        raise ValueError('DELIVERY_AUDIT_MATERIAL_SET_REQUIRED')
    body = {key: value for key, value in material_set.items() if key != 'set_sha256'}
    if canonical_sha256(body) != material_set['set_sha256']:
        raise ValueError('DELIVERY_AUDIT_MATERIAL_SET_HASH_MISMATCH')
    validate_episode_material_set(material_set)
    episode_id = material_set.get('episode_reference', {}).get('episode_id')
    expected_materials = {item['compiled']['target']['checkpoint_id']: item
                          for item in material_set.get('checkpoint_materials', [])}
    actual = _actual_delivery(deliveries, expected_materials=expected_materials, episode_id=episode_id)
    checkpoint_ids = {item['compiled']['target']['checkpoint_id'] for item in material_set.get('checkpoint_materials', [])}
    supplied_ids = {item['checkpoint_id'] for item in actual}
    if supplied_ids != checkpoint_ids:
        raise ValueError('DELIVERY_AUDIT_EPISODE_DELIVERY_COVERAGE_MISMATCH')
    if any(item['delivery_gate']['doctor_assignment_created'] or item['delivery_gate']['external_release_authorized']
           for item in actual):
        raise ValueError('DELIVERY_AUDIT_DOES_NOT_AUTHORIZE_RELEASE')
    originals = deepcopy(list(researcher_originals))
    if not isinstance(originals, list) or any(not isinstance(item, dict) or not isinstance(item.get('text'), str)
            or not isinstance(item.get('source_id'), str) for item in originals):
        raise ValueError('DELIVERY_AUDIT_RESEARCHER_ORIGINALS_INVALID')
    request = dict(format='episode-delivery-audit-request-0.1.0', task=TASK,
        protocol_version=PROTOCOL_VERSION,
        target=dict(episode_id=episode_id, episode_material_set_sha256=material_set['set_sha256']),
        episode_material_set=deepcopy(material_set), researcher_originals=originals,
        actual_deliveries=actual, deterministic_inspection=inspect_delivery_artifacts(deliveries),
        audit_scope=dict(actual_artifacts_not_preview=True, hidden_markup_and_metadata_in_scope=True,
            whole_patient_chart_certified=False,
            clinical_release_permitted=False),
        upstream_labels_are_not_ground_truth=True)
    request['response_binding'] = dict(protocol_version=PROTOCOL_VERSION,
        episode_material_set_sha256=material_set['set_sha256'],
        delivery_input_sha256=canonical_sha256(actual), request_sha256=canonical_sha256(request))
    return request


def validate_delivery_audit(request, response):
    """Require exact source evidence, full dimensions and fail-closed states."""
    if not isinstance(response, dict):
        raise ValueError('DELIVERY_AUDIT_RESPONSE_OBJECT_REQUIRED')
    _validate_schema(response, DELIVERY_AUDIT_SCHEMA)
    binding = request['response_binding']
    if any(response.get(key) != binding[key] for key in binding):
        raise ValueError('DELIVERY_AUDIT_RESPONSE_BINDING_MISMATCH')
    if response.get('target') != request['target']:
        raise ValueError('DELIVERY_AUDIT_TARGET_MISMATCH')
    if response.get('reviewer_type') != 'llm_episode_delivery_audit':
        raise ValueError('DELIVERY_AUDIT_REVIEWER_TYPE_INVALID')
    dimensions = [item['dimension'] for item in response['dimensions']]
    if len(dimensions) != len(set(dimensions)) or set(dimensions) != set(DIMENSIONS):
        raise ValueError('DELIVERY_AUDIT_DIMENSIONS_MISMATCH')
    corpus = []
    for source in request['researcher_originals']:
        corpus.append((source['source_id'], source['text']))
    for material in request['episode_material_set'].get('checkpoint_materials', []):
        for source in material.get('manifest', {}).get('sources', []):
            corpus.append((source['source_id'], source['text']))
    for finding in response['findings']:
        for item in finding['evidence']:
            sid, quote = item['source_id'], item['exact_quote']
            candidates = [text for source_id, text in corpus if source_id == sid]
            if len(candidates) != 1 or candidates[0].count(quote) != 1:
                raise ValueError('DELIVERY_AUDIT_EVIDENCE_QUOTE_NOT_UNIQUE')
    status = response['status']
    if status == 'AUTO_DELIVERY_AUDIT_PASS':
        if (request.get('deterministic_inspection', {}).get('blocking_findings')
                or any(item['status'] != 'RESOLVED' or item['applicability'] != 'APPLICABLE' for item in response['dimensions'])
                or any(item['status'] == 'OPEN' and item['severity'] == 'BLOCKING' for item in response['findings'])
                or response['revision_requests'] or response['required_actions']):
            raise ValueError('DELIVERY_AUDIT_EMPTY_OR_UNRESOLVED_PASS')
    elif status == 'AUTO_DELIVERY_AUDIT_REVISE' and not response['revision_requests']:
        raise ValueError('DELIVERY_AUDIT_REVISE_REQUIRES_REQUEST')
    elif status == 'AUTO_DELIVERY_AUDIT_HOLD' and not (
            response['required_actions'] or response['revision_requests']
            or any(item['status'] == 'OPEN' and item['severity'] == 'BLOCKING' for item in response['findings'])
            or any(item['status'] == 'OPEN' or item['applicability'] == 'UNKNOWN' for item in response['dimensions'])):
        raise ValueError('DELIVERY_AUDIT_HOLD_REQUIRES_REASON')
    return response


def inspect_delivery_artifacts(deliveries):
    """Deterministically inspect actual FHIR and HTML, without clinical inference."""
    actual = _actual_delivery(deliveries)
    findings = []
    for delivery in actual:
        if delivery['delivery_gate']['identity_error']:
            findings.append(dict(checkpoint_id=delivery['checkpoint_id'],
                code='FHIR_PACKET_IDENTITY_MISMATCH', severity='BLOCKING'))
        packet, bundle = delivery['packet'], delivery['bundle']
        expected_texts = [material['text'] for material in packet.get('materials', [])]
        for resource in bundle.get('entry', []):
            value = resource.get('resource', {})
            if value.get('resourceType') != 'DocumentReference':
                continue
            for content in value.get('content', []):
                data = content.get('attachment', {}).get('data')
                if data is not None:
                    import base64
                    try:
                        text = base64.b64decode(data, validate=True).decode('utf-8')
                    except Exception:
                        findings.append(dict(checkpoint_id=delivery['checkpoint_id'],
                            code='FHIR_ATTACHMENT_NOT_VALID_UTF8', severity='BLOCKING'))
                    else:
                        if text not in expected_texts:
                            findings.append(dict(checkpoint_id=delivery['checkpoint_id'],
                                code='FHIR_ATTACHMENT_NOT_PACKET_MATERIAL', severity='BLOCKING'))
        serialized = json.dumps(bundle, ensure_ascii=False)
        if any(field in serialized for field in ('compiled_partition', 'researcher_original', 'semantic_audit')):
            findings.append(dict(checkpoint_id=delivery['checkpoint_id'],
                code='RESEARCHER_OBJECT_IN_FHIR', severity='BLOCKING'))
        html = delivery['html']
        if re.search(r'<[^>]+\bhidden\s*=\s*["\'][^"\']+["\']', html, re.I) or re.search(
                r'<[^>]+\bstyle\s*=\s*["\'][^"\']*(?:display\s*:\s*none|visibility\s*:\s*hidden)', html, re.I):
            findings.append(dict(checkpoint_id=delivery['checkpoint_id'],
                code='HTML_HIDDEN_CONTENT_REQUIRES_REMOVAL', severity='BLOCKING'))
        if '<!--' in html:
            findings.append(dict(checkpoint_id=delivery['checkpoint_id'],
                code='HTML_COMMENT_REQUIRES_REMOVAL', severity='BLOCKING'))
        if re.search(r'<(?:script|style|template)\b[^>]*>[^<]*(?:src\s*=|(?:load|fetch|import)\s*\(\s*["\'](?:https?:)?//)', html, re.I):
            findings.append(dict(checkpoint_id=delivery['checkpoint_id'],
                code='HTML_EXTERNAL_OR_INLINE_RESOURCE_REFERENCE', severity='BLOCKING'))
        if re.search(r'<[^>]+\b(?:src|href|formaction)\s*=\s*["\'](?:https?:)?//', html, re.I):
            findings.append(dict(checkpoint_id=delivery['checkpoint_id'],
                code='HTML_EXTERNAL_LINK_REQUIRES_REVIEW', severity='NON_BLOCKING'))
    return dict(format='deterministic-delivery-inspection-0.1.0', findings=findings,
        blocking_findings=[item for item in findings if item['severity'] == 'BLOCKING'],
        no_clinical_or_leakage_clearance=True, automatic_llm_audit_still_required=True)


def _validate_schema(value, schema):
    from jsonschema import Draft202012Validator, ValidationError
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        raise ValueError('DELIVERY_AUDIT_SCHEMA_INVALID') from exc


_EVIDENCE = {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
    'required': ['source_id', 'exact_quote'], 'properties': {
        'source_id': {'type': 'string', 'minLength': 1},
        'exact_quote': {'type': 'string', 'minLength': 1}}}}
_FINDING = {'type': 'object', 'additionalProperties': False,
    'required': ['finding_id', 'dimension', 'severity', 'status', 'reason', 'evidence', 'checkpoint_ids'],
    'properties': {'finding_id': {'type': 'string', 'minLength': 1},
        'dimension': {'enum': list(DIMENSIONS)}, 'severity': {'enum': list(SEVERITIES)},
        'status': {'enum': list(FINDING_STATUSES)}, 'reason': {'type': 'string', 'minLength': 1},
        'evidence': _EVIDENCE, 'checkpoint_ids': {'type': 'array', 'items': {'type': 'string'}}}}
_REVISION = {'type': 'object', 'additionalProperties': False,
    'required': ['request_id', 'request_kind', 'reason', 'checkpoint_ids', 'required_new_version'],
    'properties': {'request_id': {'type': 'string', 'minLength': 1},
        'request_kind': {'enum': list(REVISION_KINDS)}, 'reason': {'type': 'string', 'minLength': 1},
        'checkpoint_ids': {'type': 'array', 'items': {'type': 'string'}},
        'required_new_version': {'const': True}}}
DELIVERY_AUDIT_SCHEMA = {'$schema': 'https://json-schema.org/draft/2020-12/schema',
    'type': 'object', 'additionalProperties': False,
    'required': ['format', 'review_id', 'target', 'protocol_version', 'episode_material_set_sha256',
                 'delivery_input_sha256', 'request_sha256', 'reviewer_type', 'dimensions', 'findings',
                 'revision_requests', 'status', 'required_actions'],
    'properties': {'format': {'const': 'episode-delivery-audit-0.1.0'},
        'review_id': {'type': 'string', 'minLength': 1},
        'target': {'type': 'object', 'additionalProperties': False,
            'required': ['episode_id', 'episode_material_set_sha256'], 'properties': {
                'episode_id': {'type': ['string', 'null']},
                'episode_material_set_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}},
        'protocol_version': {'const': PROTOCOL_VERSION},
        'episode_material_set_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'delivery_input_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'request_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'reviewer_type': {'const': 'llm_episode_delivery_audit'},
        'dimensions': {'type': 'array', 'minItems': len(DIMENSIONS), 'maxItems': len(DIMENSIONS),
            'items': {'type': 'object', 'additionalProperties': False,
                'required': ['dimension', 'applicability', 'status', 'reason'],
                'properties': {'dimension': {'enum': list(DIMENSIONS)},
                    'applicability': {'enum': list(APPLICABILITY)},
                    'status': {'enum': list(DIMENSION_STATUSES)}, 'reason': {'type': 'string', 'minLength': 1}}}},
        'findings': {'type': 'array', 'items': _FINDING},
        'revision_requests': {'type': 'array', 'items': _REVISION},
        'status': {'enum': list(STATUSES)},
        'required_actions': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}}}}
