"""Source-preserving R4 transport; research clocks deliberately live outside FHIR.

No clinical truth aggregation, guideline action set, or automatic episode
definition is implemented here. Research annotations must never masquerade as
hospital orders. Full source resources belong only to the researcher store.
"""
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid

LOCAL_TZ = timezone(timedelta(hours=8))  # Explicit site assumption, not inferred UTC.


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def instant(value):
    """Reject date-only times: unknown within-day ordering must remain unknown."""
    if not isinstance(value, str) or not re.fullmatch(
        r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?', value
    ):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=LOCAL_TZ)
    except ValueError:
        return None


def iso(value):
    time = instant(value)
    return time.isoformat() if time else None


def resource_id(patient, kind, source_id, content):
    # Version-specific content identity, not an assertion of original EHR version.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, '|'.join([patient, kind, source_id, digest(content)])))


def pack_event(patient, kind, source_id, title, text, event_time=None,
               recorded_time=None, release_time=None, time_basis='unknown',
               result=None, unit=None, reference=None):
    """Construct one source resource plus an explicit local research envelope.

    Lab values remain strings so qualifiers and unparsed values are not lost.
    No status='final', specimen time, LOINC mapping or true availability is guessed.
    """
    rid = resource_id(patient, kind, source_id, text)
    subject = {'reference': f'Patient/{patient}'}
    attachment = {'contentType': 'text/plain; charset=utf-8',
                  'data': base64.b64encode(text.encode()).decode()}
    if kind == 'document':
        resource = {'resourceType': 'DocumentReference', 'id': rid, 'status': 'current',
                    'subject': subject, 'type': {'text': title},
                    'content': [{'attachment': attachment}]}
        # Do not overload DocumentReference.date with body or original creation time.
    elif kind == 'imaging':
        resource = {'resourceType': 'DiagnosticReport', 'id': rid, 'status': 'unknown',
                    'subject': subject, 'code': {'text': title}, 'presentedForm': [attachment]}
        if iso(event_time):
            resource['effectiveDateTime'] = iso(event_time)
        if iso(release_time):
            resource['issued'] = iso(release_time)
    elif kind == 'lab':
        resource = {'resourceType': 'Observation', 'id': rid, 'status': 'unknown',
                    'subject': subject, 'code': {'text': title}}
        if result is not None and str(result).strip():
            resource['valueString'] = str(result)
        resource['note'] = [{'text': json.dumps({'unit_raw': unit, 'reference_raw': reference},
                                               ensure_ascii=False)}]
        if iso(release_time):
            resource['issued'] = iso(release_time)
        # Report time is not specimen/effective time; leave effective[x] absent.
    else:
        raise ValueError(f'Unsupported source kind: {kind}')
    return {'event_id': rid, 'patient': patient, 'kind': kind, 'source_id': source_id,
            'source_sha256': digest(text), 'title': title, 'source_text': text,
            'event_time': iso(event_time), 'recorded_time': iso(recorded_time),
            'release_time': iso(release_time), 'time_basis': time_basis,
            'available_time': None, 'version_availability_verified': False,
            'resource': resource}


def source_bundle(events, conversion_time):
    """FHIR collection: source resources plus conversion Provenance, no server writes."""
    if not iso(conversion_time):
        raise ValueError('Precise conversion time required')
    patients = sorted({e['patient'] for e in events})
    resources = [{'resourceType': 'Patient', 'id': p} for p in patients]
    resources.append({'resourceType': 'Device', 'id': 'research-importer',
                      'deviceName': [{'name': 'PDAC replay importer v0.1', 'type': 'user-friendly-name'}]})
    for event in events:
        resource = copy.deepcopy(event['resource'])
        resources.append(resource)
        resources.append({'resourceType': 'Provenance', 'id': 'p-' + event['event_id'],
                          'target': [{'reference': resource['resourceType'] + '/' + resource['id']}],
                          'recorded': iso(conversion_time),
                          'agent': [{'who': {'reference': 'Device/research-importer'}}],
                          'entity': [{'role': 'source', 'what': {'identifier': {
                              'system': 'urn:pdac-replay:source-sha256', 'value': event['source_sha256']}}}]})
    ids = [(r['resourceType'], r['id']) for r in resources]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate resource identity; resolve source duplicates explicitly')
    return {'resourceType': 'Bundle', 'type': 'collection', 'entry': [
        {'fullUrl': 'https://pdac-replay.invalid/fhir/' + r['resourceType'] + '/' + r['id'],
         'resource': r} for r in resources]}


def reconstruction_clock(event):
    if event['kind'] == 'lab':
        return instant(event['release_time'])
    return instant(event['event_time']) or instant(event['recorded_time'])


def replay(events, patient, checkpoint_time, anchor_id, action_marker, mode='reconstruction'):
    """Return source blocks before a manually verified decision anchor.

    Reconstruction uses declared proxy clocks. Certified mode only admits
    explicitly verified version availability, failing closed per resource.
    Neither mode silently declares a complete or formally blinded state.
    The current note prefix is a reviewed reconstruction, never a backdated
    original resource. Current action and future evidence stay in audit only.
    """
    if mode not in {'reconstruction', 'certified'}:
        raise ValueError('Unknown replay mode')
    cutoff = instant(checkpoint_time)
    if cutoff is None:
        raise ValueError('Checkpoint time lacks precision')
    patient_events = [e for e in events if e['patient'] == patient]
    matches = [e for e in patient_events if e['source_id'] == anchor_id]
    if len(matches) != 1:
        raise ValueError('Anchor source must be unique within patient')
    anchor = matches[0]
    if not action_marker or anchor['source_text'].count(action_marker) != 1:
        raise ValueError('Action boundary must be a unique reviewed source marker')
    if reconstruction_clock(anchor) != cutoff:
        raise ValueError('Anchor body time and declared checkpoint must agree in this prototype')
    cut = anchor['source_text'].index(action_marker)
    blocks, audit = [], []
    for e in patient_events:
        clock = reconstruction_clock(e)
        certified = bool(e['version_availability_verified']) and instant(e['available_time']) is not None
        if e is anchor:
            selected = mode == 'reconstruction'
            reason = 'REVIEWED_CURRENT_PREFIX' if selected else 'CURRENT_PREFIX_NOT_VERSION_CERTIFIED'
            end = cut
        elif mode == 'certified':
            selected = certified and instant(e['available_time']) < cutoff
            reason = 'CERTIFIED_PRIOR_VERSION' if selected else 'NO_VERIFIED_PRIOR_AVAILABILITY'
            end = len(e['source_text'])
        else:
            selected = clock is not None and clock < cutoff
            reason = 'PROXY_TIME_PRIOR' if selected else 'MISSING_SAME_OR_FUTURE_PROXY_TIME'
            end = len(e['source_text'])
        audit.append({'event_id': e['event_id'], 'selected': selected, 'reason': reason,
                      'event_time': e['event_time'], 'release_time': e['release_time'],
                      'recorded_time': e['recorded_time'], 'time_basis': e['time_basis'],
                      'late_creation': bool(instant(e['recorded_time']) and instant(e['recorded_time']) >= cutoff)})
        if selected:
            blocks.append({'event_id': e['event_id'], 'source_id': e['source_id'], 'kind': e['kind'],
                           'title': e['title'], 'source_span': [0, end],
                           'source_sha256': e['source_sha256'],
                           'time': clock.isoformat() if clock else None,
                           'time_basis': e['time_basis'], 'text': e['source_text'][:end]})
    blocks.sort(key=lambda b: (b['time'] or '', b['event_id']))
    return {'patient': patient, 'checkpoint_time': cutoff.isoformat(), 'mode': mode,
            'status': 'RESEARCH_RECONSTRUCTION' if mode == 'reconstruction' else 'INCOMPLETE_CERTIFIED_SUBSET',
            'formal_blind_evaluation_ready': False, 'source_blocks': blocks}, {
                'source_selection': audit, 'held_current_action': anchor['source_text'][cut:],
                'action_span': [cut, len(anchor['source_text'])],
                'anchor_source_id': anchor_id}
