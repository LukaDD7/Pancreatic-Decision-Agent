"""Optional source join to existing FHIR envelopes; not a clinical mapping.

Patient alias mapping must be explicit. A matching source ID with different
content is rejected. This does not create orders, observations or references
from inferred decisions, and it does not certify evidence availability.
"""
import base64
from .extract import sha


def source_crosswalk(nodes, events, patient_aliases):
    index = {}
    for e in events:
        if e['kind'] != 'document':
            continue
        key = (e['patient'], e['source_id'], e['source_sha256'])
        if key in index:
            raise ValueError('Ambiguous source version')
        resource = e['resource']
        attachment = resource['content'][0]['attachment']['data']
        text = base64.b64decode(attachment).decode('utf-8')
        if resource['resourceType'] != 'DocumentReference' or sha(text) != e['source_sha256'] or text != e['source_text']:
            raise ValueError('Source/resource content mismatch')
        if resource['subject']['reference'] != 'Patient/'+e['patient']:
            raise ValueError('Source/resource patient mismatch')
        index[key] = e
    links = []
    for n in nodes:
        alias = patient_aliases.get(n['patient_uid'])
        if alias is None:
            continue  # Patient not part of this explicitly scoped adapter run.
        key = (alias, n['source_id'], n['source_sha256'])
        if key not in index:
            raise ValueError('Exact source version missing in scoped FHIR collection')
        e = index[key]
        if e['source_text'][slice(*n['span'])] != n['quote']:
            raise ValueError('Statement/source span mismatch')
        links.append({'node_id': n['id'], 'source_span': n['span'],
                      'fhir_reference': 'DocumentReference/'+e['resource']['id'],
                      'source_sha256': n['source_sha256'],
                      'meaning': 'same original source text; no clinical-reference or availability assertion'})
    return links
