"""FHIR R4 source resources and explicitly marked research annotations."""
import base64
from datetime import datetime, timezone, timedelta
import json
import hashlib
import re
from pathlib import Path

from jsonschema import Draft6Validator

from scripts.cohort_construction.p2_fhir_replay.core import pack_event, source_bundle
from scripts.cohort_construction.p2_pathway_replay.core import (
    clock as source_clock, available_source_clock, availability_clock_role,
)
from .extract import digest, body_clock, clock


EXT = 'https://pdac-research.invalid/fhir/StructureDefinition/'


def build(sources, patient_alias, decisions, episodes):
    now = datetime.now(timezone.utc).isoformat(); events, source_refs = [], {}
    for s in sources:
        kind = s['kind'] if s['kind'] in ['document', 'imaging', 'lab'] else 'document'
        original = s.get('original_fields', {})
        # FHIR's string pattern permits ASCII whitespace, not every Unicode
        # whitespace character used in exported Chinese form titles. Only the
        # display label is normalized; the raw title/text remain in provenance.
        display_title=re.sub(r'[^\S \r\n\t]',' ',str(s['title'] or s['kind']))
        # ``issued`` is R4's release instant. 审核时间 is the audit/verification
        # time and, by the user's 2026-11-09 decision, the time the report became
        # visible; it therefore takes precedence over the writing clock when both
        # are supplied. The writing clock remains in the lossless extension.
        release_role = availability_clock_role(s['clocks'])
        # A document date is not a report release timestamp. Keep the generic
        # FHIR issued field empty rather than relabeling source_create_time.
        if kind == 'document':
            release_role = None
        event = pack_event(patient_alias, kind, s['source_id'], display_title, s['text'],
                           event_time=s['clocks'].get('exam_time', {}).get('value'),
                           release_time=s['clocks'].get(release_role, {}).get('value') if release_role else None,
                           recorded_time=s['clocks'].get('source_create_time', {}).get('value'),
                           time_basis=s['clock_basis'], result=original.get('检验结果值'),
                           unit=original.get('检验结果单位'), reference=original.get('检验参考值'))
        if kind=='lab':
            for note in event['resource'].get('note',[]):
                note['text']=json.dumps(json.loads(note['text']),ensure_ascii=True)
        # Pathology and quarantined text are preserved as source documents in
        # this first adapter; quarantine can never be a clean Observation.
        event['resource'].setdefault('extension', []).append(dict(
            url=EXT+'source-clock-and-provenance', valueString=json.dumps(dict(
                family=s['family'], clocks=s['clocks'], provenance=s['provenance'],
                source_id=s['source_id'], source_kind=s['kind'], source_title=s['title'],
                source_fields=s.get('original_fields', {}), clock=s.get('clock'), clock_basis=s.get('clock_basis'),
                release_role=release_role,
                time_provenance=s.get('time_provenance', {}),
                original_observation_text=s['text'] if s['kind']=='lab' else None,
                display_title_whitespace_normalized=display_title!=s['title'],
                flags=s.get('flags', []), source_sha256=s['source_sha256'],
                original_source_sha256=s.get('original_source_sha256'), availability_certified=False), ensure_ascii=True)))
        if s['kind'] == 'quarantine':
            event['resource']['meta'] = {'tag': [{'system': 'urn:pdac:research', 'code': 'source-quarantine'}]}
        events.append(event)
        source_refs[s['source_id']] = event['resource']['resourceType']+'/'+event['resource']['id']
    bundle = source_bundle(events, now)
    for kind, annotations in [('decision-event', decisions), ('candidate-episode', episodes)]:
        for a in annotations:
            rid = kind+'-'+digest([patient_alias, a.get('candidate_id') or a['episode_id']])[:26]
            cited = {s['source_id'] for s in a.get('semantic', {}).get('supports', [])}
            cited |= {s['source_id'] for s in a.get('evolution', {}).get('supports', [])}
            cited |= {s['source_id'] for s in a.get('evolution', {}).get('connection_supports', [])}
            resource = dict(resourceType='Basic', id=rid, subject={'reference': 'Patient/'+patient_alias},
                            code={'coding': [{'system': 'urn:pdac:research-annotation-type', 'code': kind}]},
                            meta={'tag': [{'system': 'urn:pdac:research', 'code': 'ai-generated-not-clinical-order'}]},
                            extension=[dict(url=EXT+'research-annotation-json', valueString=json.dumps(a, ensure_ascii=True))]
                            +[dict(url=EXT+'supporting-source', valueReference={'reference': source_refs[sid]}) for sid in sorted(cited)])
            bundle['entry'].append(dict(fullUrl='https://pdac-research.invalid/fhir/Basic/'+rid, resource=resource))
            if cited:
                provenance = dict(resourceType='Provenance', id='p-'+rid,
                                  target=[{'reference': 'Basic/'+rid}], recorded=now,
                                  agent=[{'who': {'reference': 'Device/research-importer'}}],
                                  entity=[{'role': 'source', 'what': {'reference': source_refs[sid]}} for sid in sorted(cited)])
                bundle['entry'].append(dict(resource=provenance))
    return bundle


def _generic_attachment_text(attachments):
    """Return text only for the one reversible generic attachment layout.

    P2AUTO-023: concatenating inline parts invents a new clinical document;
    selecting their inline subset silently drops URL content. Generic imports
    therefore reject every multi-part or inline+URL layout. A resource with no
    attachments is still preserved as its complete original JSON by the caller.
    The adapter's own single-inline resources use the separate lossless
    extension path and are unaffected.
    """
    if len(attachments) > 1 or any(
            'data' in attachment and attachment.get('url') not in (None, '')
            for attachment in attachments):
        raise ValueError('FHIR_GENERIC_MULTI_OR_MIXED_ATTACHMENT_UNSUPPORTED')
    if not attachments or 'data' not in attachments[0]:
        return None
    return base64.b64decode(attachments[0]['data'], validate=True).decode()


def import_sources(bundle, *, deidentify=True):
    """Read source resources, ignoring any research Basic annotations.

    Supports this adapter's lossless source extension and ordinary R4 source
    DocumentReference, DiagnosticReport and Observation. Orders are explicitly
    not synthesized from results; wider hospital profiles need another adapter.
    """
    sources=[]; skipped=0
    patients={r['resource']['id'] for r in bundle['entry'] if r['resource']['resourceType']=='Patient'}
    if len(patients)!=1:raise ValueError('FHIR_INPUT_MUST_HAVE_EXACTLY_ONE_PATIENT')
    patient=next(iter(patients))
    patient_refs={'Patient/'+patient} | {e.get('fullUrl') for e in bundle['entry'] if e['resource']['resourceType']=='Patient'}
    for entry in bundle['entry']:
        r=entry['resource']; rt=r['resourceType']
        if rt in ['Basic','Provenance','Patient','Device']:
            skipped+=1; continue
        if rt not in ['DocumentReference','DiagnosticReport','Observation']:
            raise ValueError('UNSUPPORTED_FHIR_SOURCE_TYPE:'+rt)
        if r.get('subject',{}).get('reference') not in patient_refs:
            raise ValueError('FHIR_SOURCE_PATIENT_REFERENCE_MISMATCH')
        meta=next((json.loads(e['valueString']) for e in r.get('extension',[]) if e['url']==EXT+'source-clock-and-provenance'),None)
        attachments=([c['attachment'] for c in r.get('content',[])] if rt=='DocumentReference' else r.get('presentedForm',[]))
        if not meta:
            texts=_generic_attachment_text(attachments)
            text=texts if texts is not None else json.dumps(r,ensure_ascii=False,indent=2)
        else:
            texts=[base64.b64decode(a['data'],validate=True).decode() for a in attachments if 'data' in a]
            text='\n'.join(texts) if texts else json.dumps(r,ensure_ascii=False,indent=2)
        if meta and rt=='Observation':
            text=meta['original_observation_text']
        source_hash=hashlib.sha256(text.encode()).hexdigest()
        if meta:
            if source_hash!=meta['source_sha256']:raise ValueError('FHIR_ATTACHMENT_SOURCE_HASH_MISMATCH')
            sources.append(dict(source_id=meta['source_id'],patient_uid=patient,kind=meta['source_kind'],
                                family=meta['family'],title=meta['source_title'],text=text,source_sha256=source_hash,
                                original_source_sha256=meta.get('original_source_sha256'),
                                clocks=meta['clocks'],clock=meta.get('clock'),clock_basis=meta.get('clock_basis'),
                                time_provenance=meta.get('time_provenance', {}),
                                provenance=meta['provenance'],original_fields=meta['source_fields'],flags=meta['flags']))
            continue
        # Generic resource import: keep the entire original JSON when no text
        # attachment exists. No status, clinical code or release date is guessed.
        kind={'DocumentReference':'document','DiagnosticReport':'imaging','Observation':'lab'}[rt]
        title=(r.get('type') if rt=='DocumentReference' else r.get('code')) or {}
        title=title.get('text') or rt
        effective=fhir_clock(r.get('effectiveDateTime')); issued=fhir_clock(r.get('issued'))
        if kind=='document':
            clocks={'body_record_time':body_clock(text),'source_create_time':fhir_clock(r.get('date'))}
            candidate = dict(source_id=digest([patient,rt,r.get('id'),source_hash])[:24],
                kind=kind,title=title,text=text,source_sha256=source_hash,clocks=clocks,
                clock=clocks['body_record_time'],clock_basis='body_record_time')
            # The generic FHIR adapter shares the same narrow request-signature
            # adapter as native v04. It is still a record-day proxy, not a
            # publication, choice, or clinician-reading timestamp.
            from scripts.cohort_construction.p2_auto_checkpoint_v04.source_time import source_record_time
            _, chosen, basis, _ = available_source_clock(
                candidate, document_record_time=source_record_time)
        else:
            clocks={'report_time':issued,'exam_time' if kind=='imaging' else 'observation_effective_time':effective}
            # Both generic DiagnosticReport and Observation use the shared
            # release resolver. effective/exam time cannot replace issued time.
            _, chosen, basis, _ = available_source_clock(dict(kind=kind, clocks=clocks))
        sources.append(dict(source_id=digest([patient,rt,r.get('id'),source_hash])[:24],patient_uid=patient,
                            kind=kind,family='FHIR/'+rt,title=title,text=text,source_sha256=source_hash,
                            clocks=clocks,clock=chosen,clock_basis=basis,
                            provenance={'native_resource_reference':rt+'/'+r.get('id','')},original_fields={},flags=[]))
    if not deidentify:
        return sources, dict(skipped_non_source_resources=skipped,
                            resources_imported=len(sources),
                            research_annotations_used_as_input=False,
                            direct_identifier_deidentification=None)
    return _deidentify_imported_sources(bundle, sources, patient, skipped)


def _deidentify_imported_sources(bundle, sources, patient, skipped):
    import hashlib
    from scripts.cohort_construction.p2_checkpoint_harness.stages.privacy import (
        collect_identifier_values, deidentify_text, deidentify_value)

    patient_resource = next(entry['resource'] for entry in bundle['entry']
                            if entry['resource']['resourceType'] == 'Patient'
                            and entry['resource']['id'] == patient)
    values = collect_identifier_values(sources, patient_resource)
    receipt = dict(skipped_non_source_resources=skipped, resources_imported=len(sources),
                   research_annotations_used_as_input=False,
                   direct_identifier_deidentification=dict(
                       stage='FHIR_SOURCE_IMPORT_BEFORE_EPISODE_DISCOVERY',
                       method='STRUCTURED_VALUES_AND_EXPLICIT_LABELS_THEN_CROSS_SOURCE_LITERALS',
                       ages_and_sex_retained=True, ages_over_89_grouped=True,
                       exact_dates_retained_for_sequential_evaluation=True,
                       hipaa_safe_harbor_claimed=False, source_count=len(sources)))
    for source in sources:
        for field in ('title', 'text'):
            source[field] = deidentify_text(source.get(field) or '', values)
        for field in ('original_fields', 'provenance', 'flags'):
            source[field] = deidentify_value(source.get(field), values)
        source['deidentified_text_sha256'] = hashlib.sha256(source['text'].encode()).hexdigest()
        source['original_source_sha256'] = source.get('original_source_sha256') or source['source_sha256']
        source['source_sha256'] = source['deidentified_text_sha256']
        source['direct_identifier_deidentification'] = dict(
            stage='FHIR_SOURCE_IMPORT_BEFORE_EPISODE_DISCOVERY',
            source_sha256_basis='DEIDENTIFIED_SOURCE_TEXT',
            original_source_sha256_basis='ORIGINAL_SOURCE_BEFORE_DEIDENTIFICATION',
            deidentified_text_sha256=source['deidentified_text_sha256'])
    return sources, receipt


def fhir_clock(raw):
    """Use the same offset/subsecond normalization as native source ingress."""
    return source_clock(raw)


def validate(bundle, schema_path):
    schema = json.loads(Path(schema_path).read_text())
    validators={}
    for entry in bundle['entry']:
        r=entry['resource'];rt=r['resourceType']
        validator=validators.setdefault(rt,Draft6Validator({'$ref':'#/definitions/'+rt,'definitions':schema['definitions']}))
        for e in validator.iter_errors(r):
            # Do not serialize the entire clinical resource into an error.
            raise ValueError('FHIR_SCHEMA_ERROR:'+json.dumps(dict(resource_type=rt,resource_id=r['id'],
                  path=list(e.path),validator=e.validator,schema_path=list(e.schema_path))))
    errors = list(Draft6Validator(schema).iter_errors(bundle))
    if errors:
        raise ValueError('FHIR_BUNDLE_SCHEMA_ERROR:'+str([list(e.path) for e in errors[:3]]))
    ids = {e['resource']['resourceType']+'/'+e['resource']['id'] for e in bundle['entry']}
    if len(ids) != len(bundle['entry']):
        raise ValueError('FHIR_DUPLICATE_ID')
    def visit(x):
        if isinstance(x, dict):
            if 'reference' in x and x['reference'] not in ids:
                raise ValueError('FHIR_UNRESOLVED_REFERENCE')
            for v in x.values():visit(v)
        elif isinstance(x, list):
            for v in x:visit(v)
    visit(bundle)
    return dict(official_r4_json_schema=True, local_references_resolved=True,
                resource_count=len(ids), profiles_fhirpath_terminology_validated=False,
                research_extensions_registered=False, clinical_order_created=False)
