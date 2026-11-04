"""FHIR R4 source resources and explicitly marked research annotations."""
import base64
from datetime import datetime, timezone, timedelta
import json
import hashlib
import re
from pathlib import Path

from jsonschema import Draft6Validator

from scripts.cohort_construction.p2_fhir_replay.core import pack_event, source_bundle
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
        event = pack_event(patient_alias, kind, s['source_id'], display_title, s['text'],
                           event_time=s['clocks'].get('exam_time', {}).get('value'),
                           release_time=s['clocks'].get('report_time', {}).get('value'),
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
                source_fields=s.get('original_fields', {}), clock=s['clock'], clock_basis=s['clock_basis'],
                original_observation_text=s['text'] if s['kind']=='lab' else None,
                display_title_whitespace_normalized=display_title!=s['title'],
                flags=s.get('flags', []), source_sha256=s['source_sha256'], availability_certified=False), ensure_ascii=True)))
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


def import_sources(bundle):
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
        texts=[base64.b64decode(a['data'],validate=True).decode() for a in attachments if 'data' in a]
        text='\n'.join(texts) if texts else json.dumps(r,ensure_ascii=False,indent=2)
        if meta and rt=='Observation':
            text=meta['original_observation_text']
        source_hash=hashlib.sha256(text.encode()).hexdigest()
        if meta:
            if source_hash!=meta['source_sha256']:raise ValueError('FHIR_ATTACHMENT_SOURCE_HASH_MISMATCH')
            sources.append(dict(source_id=meta['source_id'],patient_uid=patient,kind=meta['source_kind'],
                                family=meta['family'],title=meta['source_title'],text=text,source_sha256=source_hash,
                                clocks=meta['clocks'],clock=meta['clock'],clock_basis=meta['clock_basis'],
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
            chosen=clocks['body_record_time']; basis='body_record_time'
        else:
            clocks={'report_time':issued,'exam_time' if kind=='imaging' else 'observation_effective_time':effective}
            chosen=issued if issued['value'] else effective if kind=='imaging' else clock(None)
            basis='issued_proxy' if issued['value'] else 'exam_time_proxy_release_unknown' if kind=='imaging' else 'unknown'
        sources.append(dict(source_id=digest([patient,rt,r.get('id'),source_hash])[:24],patient_uid=patient,
                            kind=kind,family='FHIR/'+rt,title=title,text=text,source_sha256=source_hash,
                            clocks=clocks,clock=chosen,clock_basis=basis,
                            provenance={'native_resource_reference':rt+'/'+r.get('id','')},original_fields={},flags=[]))
    return sources,dict(skipped_non_source_resources=skipped,resources_imported=len(sources),
                        research_annotations_used_as_input=False)


def fhir_clock(raw):
    c=clock(raw)
    if isinstance(raw,str) and c['value'] and c['precision']!='date':
        parsed=datetime.fromisoformat(raw.replace('Z','+00:00'))
        if parsed.tzinfo:
            c['value']=parsed.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    return c


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
