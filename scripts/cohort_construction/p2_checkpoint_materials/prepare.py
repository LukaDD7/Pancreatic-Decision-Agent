"""Prepare exact original-text partitions without granting disclosure permission.

The program proposes MASK for mechanically verified decision/result spans.
Every other character is REVIEW. Even apparently eligible KEEP observations
remain REVIEW until an independent semantic/material assessment is supplied.
"""
from collections import Counter
from copy import deepcopy
import re

from scripts.cohort_construction.p2_checkpoint_packet.compiler import (
    CONTRACT_VERSION,build_source_manifest,canonical_sha256,compile_partition,text_sha256,
)

RULE_VERSION='event-material-review-0.1.0'


class MaterialPreparationError(ValueError):
    def __init__(self,code,audit):
        self.code=code
        self.audit=deepcopy(audit)
        super().__init__(code)


def _issue(audit,code,**details):
    audit['issues'].append(dict(code=code,**details,attribution='processing_or_proposal_issue_not_patient_quality'))


def _reject(audit,code,**details):
    _issue(audit,code,**details)
    audit['preparation_status']='REJECTED'
    raise MaterialPreparationError(code,audit)


def _identity(quote):
    return canonical_sha256({k:quote.get(k) for k in ('source_id','source_sha256','start','end','quote')})


def _verified_hint(citation,source_map,audit,*,origin,index,requested_disposition):
    hint=dict(hint_id='H-'+canonical_sha256([origin,index,citation])[:24],
        origin=origin,index=index,requested_disposition=requested_disposition,
        llm_proposal_not_independent_audit=True,original_citation=deepcopy(citation),status='REJECTED')
    audit['citation_hints'].append(hint)
    if not isinstance(citation,dict):
        hint['rejection_code']='MALFORMED_CITATION';return None
    sid=citation.get('source_id');hint['source_id']=sid
    if not isinstance(sid,str) or sid not in source_map:
        hint['rejection_code']='CITATION_SOURCE_OUTSIDE_DECLARED_SCOPE'
        _issue(audit,hint['rejection_code'],source_id=sid,hint_id=hint['hint_id']);return None
    source=source_map[sid];start=citation.get('start');end=citation.get('end');quote=citation.get('quote')
    reason=None
    if citation.get('source_sha256')!=source['sha256']:reason='CITATION_SOURCE_HASH_MISMATCH'
    elif type(start) is not int or type(end) is not int or not 0<=start<end<=len(source['text']):reason='CITATION_COORDINATES_INVALID'
    elif not isinstance(quote,str) or not quote or source['text'][start:end]!=quote:reason='CITATION_EXACT_TEXT_MISMATCH'
    elif (citation.get('status')!='VALID' or citation.get('raw_quote_verified') is not True
          or citation.get('span_verified') is not True or citation.get('eligible_as_source_support') is not True):reason='CITATION_VERIFICATION_FLAGS_NOT_TRUE'
    if reason:
        hint['rejection_code']=reason
        _issue(audit,reason,source_id=sid,hint_id=hint['hint_id'])
        if requested_disposition=='MASK':audit['quarantined_source_ids'].append(sid)
        return None
    hint.update(status='MECHANICALLY_VERIFIED_LLM_HINT',start=start,end=end,
        source_sha256=source['sha256'],exact_quote=quote)
    return hint


def prepare_material_review(event,sources,*,decision_task_id,visibility_policy_version):
    """Return SourceManifest, SplitProposal, CompiledPartition, researcher audit.

    Source scope is exactly event.source_ids union actual_model_evidence source
    IDs. Full, unchanged source strings are hash-bound. There is no network/model
    call, semantic PASS, clinical Reference, or doctor packet in this bridge.
    """
    audit=dict(rule_version=RULE_VERSION,issue_ids=['CE-MAT-001','CE-MAT-002','CE-MAT-003','CE-MAT-004'],
        preparation_status='PREPARING',event_id=event.get('event_id') if isinstance(event,dict) else None,
        semantic_audit_status='NOT_ASSESSED',researcher_only=True,doctor_packet_created=False,
        clinical_reference_created=False,model_api_calls=0,patient_level_coverage_complete=False,
        source_scope='event.source_ids union event.actual_model_evidence[].source_id',
        source_routing=[],citation_hints=[],availability_checks=[],evidence_block_checks=[],
        segment_decisions=[],quarantined_source_ids=[],issues=[],
        default_disposition='REVIEW',keep_policy='ELIGIBLE_OBSERVATIONS_REMAIN_REVIEW_PENDING_INDEPENDENT_AUDIT')
    if not isinstance(event,dict) or not isinstance(event.get('event_id'),str) or not event['event_id']:
        _reject(audit,'EVENT_ID_REQUIRED')
    if not isinstance(decision_task_id,str) or not decision_task_id or not isinstance(visibility_policy_version,str) or not visibility_policy_version:
        _reject(audit,'EXPLICIT_TASK_AND_VISIBILITY_POLICY_REQUIRED')
    declared=event.get('source_ids')
    if not isinstance(declared,list) or any(not isinstance(s,str) or not s for s in declared):
        _reject(audit,'EVENT_SOURCE_IDS_REQUIRED')
    evidence=event.get('actual_model_evidence',[])
    if not isinstance(evidence,list):_reject(audit,'ACTUAL_MODEL_EVIDENCE_MUST_BE_ARRAY')
    evidence_ids=[]
    for index,block in enumerate(evidence):
        if not isinstance(block,dict) or not isinstance(block.get('source_id'),str) or not block['source_id']:
            _reject(audit,'EVIDENCE_SOURCE_ID_REQUIRED',evidence_index=index)
        evidence_ids.append(block['source_id'])
    selected=set(declared)|set(evidence_ids)
    if not selected:_reject(audit,'NO_DECLARED_SOURCE_SCOPE')
    if not isinstance(sources,list):_reject(audit,'SOURCES_MUST_BE_ARRAY')
    provided={}
    for source in sources:
        if not isinstance(source,dict) or not isinstance(source.get('source_id'),str) or not source['source_id']:
            _reject(audit,'SOURCE_ID_REQUIRED')
        sid=source['source_id']
        if sid in provided:_reject(audit,'DUPLICATE_SOURCE_ID',source_id=sid)
        provided[sid]=source
    audit.update(event_sha256=canonical_sha256(event),provided_source_count=len(provided),
        selected_source_ids=sorted(selected),excluded_source_ids=sorted(set(provided)-selected),
        declared_document_source_ids=sorted(set(declared)),actual_evidence_source_ids=sorted(set(evidence_ids)))
    for sid in sorted(set(provided)|selected):
        audit['source_routing'].append(dict(source_id=sid,
            disposition='MISSING_REQUESTED_SOURCE' if sid not in provided else 'INCLUDED_FULL_ORIGINAL_TEXT' if sid in selected else 'EXCLUDED_OUTSIDE_EVENT_SCOPE',
            in_event_source_ids=sid in declared,in_actual_model_evidence=sid in evidence_ids))
    missing=selected-set(provided)
    if missing:_reject(audit,'SELECTED_SOURCE_MISSING',source_ids=sorted(missing))
    manifest_inputs=[]
    for sid in sorted(selected):
        source=provided[sid];text=source.get('text');recorded=source.get('source_sha256')
        if not isinstance(text,str) or not text:_reject(audit,'SELECTED_SOURCE_TEXT_EMPTY_OR_UNREADABLE',source_id=sid)
        if not isinstance(recorded,str) or not re.fullmatch('[0-9a-f]{64}',recorded):
            _reject(audit,'SELECTED_SOURCE_HASH_MISSING',source_id=sid)
        if recorded!=text_sha256(text):_reject(audit,'SELECTED_SOURCE_HASH_MISMATCH',source_id=sid)
        source_version=source.get('source_version')
        if source_version is not None and (not isinstance(source_version,str) or not source_version):
            _reject(audit,'INVALID_SOURCE_VERSION',source_id=sid)
        version=source_version or 'content-sha256:'+recorded
        manifest_inputs.append(dict(source_id=sid,source_version=version,text=text))
    manifest_id='SM-'+canonical_sha256([RULE_VERSION,event['event_id'],[(s['source_id'],text_sha256(s['text'])) for s in manifest_inputs]])[:24]
    manifest=build_source_manifest(manifest_id,manifest_inputs)
    source_map={s['source_id']:s for s in manifest['sources']}
    audit.update(manifest_id=manifest_id,manifest_sha256=canonical_sha256(manifest),
        source_hashes={sid:s['sha256'] for sid,s in source_map.items()},
        source_version_semantics='explicit source_version if provided, otherwise content hash identity; not an inferred clinical report revision')
    if event.get('source_id') not in set(declared):
        _issue(audit,'PRIMARY_SOURCE_NOT_IN_DECLARED_SOURCE_IDS',source_id=event.get('source_id'))
    for index,block in enumerate(evidence):
        sid=block['source_id'];text=source_map[sid]['text'];start=block.get('start');end=block.get('end')
        quote=block.get('quote');valid=(type(start) is int and type(end) is int and 0<=start<end<=len(text)
            and isinstance(quote,str) and text[start:end]==quote)
        audit['evidence_block_checks'].append(dict(index=index,source_id=sid,start=start,end=end,
            original_quote=quote,status='EXACT_ORIGINAL_SPAN' if valid else 'EVIDENCE_COORDINATES_OR_QUOTE_INVALID'))
        if not valid:_issue(audit,'EVIDENCE_COORDINATES_OR_QUOTE_INVALID',source_id=sid,evidence_index=index)
    masks=[];eligible_keeps=[]
    for field in ('choice_boundary_citations','postchoice_citations'):
        collection=event.get(field)
        if not isinstance(collection,dict) or not isinstance(collection.get('valid'),list):
            _issue(audit,'BOUNDARY_CITATION_COLLECTION_UNAVAILABLE',field=field);continue
        for category in ('invalid','ambiguous'):
            if collection.get(category):
                _issue(audit,'UNRESOLVED_BOUNDARY_CITATIONS',field=field,category=category,count=len(collection[category]))
                for quote in collection[category]:
                    if isinstance(quote,dict) and quote.get('source_id') in selected:
                        audit['quarantined_source_ids'].append(quote['source_id'])
        for index,citation in enumerate(collection['valid']):
            hint=_verified_hint(citation,source_map,audit,origin=field+'.valid',index=index,requested_disposition='MASK')
            if hint:masks.append(hint)
    observations=event.get('validated_observation_citations',{}).get('valid',[]) if isinstance(event.get('validated_observation_citations'),dict) else []
    if not isinstance(observations,list):observations=[];_issue(audit,'VALIDATED_OBSERVATIONS_UNAVAILABLE')
    observation_identities={_identity(q) for q in observations if isinstance(q,dict)}
    availability=event.get('availability_validation',[])
    if not isinstance(availability,list):availability=[];_issue(audit,'AVAILABILITY_VALIDATION_UNAVAILABLE')
    for index,item in enumerate(availability):
        check=dict(index=index,status='NOT_ELIGIBLE_KEEP_HINT',availability=item.get('availability') if isinstance(item,dict) else None)
        audit['availability_checks'].append(check)
        if not isinstance(item,dict) or item.get('availability')!='explicitly_known_before' or item.get('is_declared_observation') is not True or item.get('eligible_as_known_prechoice') is not True:
            continue
        verified=item.get('validated_observation',{}).get('valid',[]) if isinstance(item.get('validated_observation'),dict) else []
        bases=item.get('validated_basis',{}).get('valid',[]) if isinstance(item.get('validated_basis'),dict) else []
        if not isinstance(verified,list) or not isinstance(bases,list) or not verified or not bases:
            check['status']='EXPLICIT_AVAILABILITY_WITHOUT_VALIDATED_QUOTE_OR_BASIS';continue
        verified_bases=[_verified_hint(quote,source_map,audit,
            origin=f'availability_validation[{index}].validated_basis.valid',index=bindex,
            requested_disposition='AVAILABILITY_BASIS_ONLY') for bindex,quote in enumerate(bases)]
        if any(hint is None for hint in verified_bases):
            check['status']='EXPLICIT_AVAILABILITY_BASIS_NOT_MECHANICALLY_VERIFIED';continue
        for qindex,quote in enumerate(verified):
            if not isinstance(quote,dict) or _identity(quote) not in observation_identities:continue
            hint=_verified_hint(quote,source_map,audit,origin=f'availability_validation[{index}].validated_observation.valid',index=qindex,requested_disposition='KEEP_CANDIDATE_ONLY')
            if hint:
                # This deliberately remains a hint. Program eligibility is not
                # independent confirmation of pre-decision clinical visibility.
                eligible_keeps.append(hint);check['status']='KEEP_HINT_PENDING_INDEPENDENT_REVIEW'
    audit['quarantined_source_ids']=sorted(set(audit['quarantined_source_ids']))
    target=dict(checkpoint_id=event['event_id'],decision_task_id=decision_task_id,visibility_policy_version=visibility_policy_version)
    segments=[]
    for sid,source in source_map.items():
        hints=[h for h in masks+eligible_keeps if h['source_id']==sid]
        boundaries=sorted({0,len(source['text'])}|{h[k] for h in hints for k in ('start','end')})
        for start,end in zip(boundaries,boundaries[1:]):
            covering=[h for h in hints if h['start']<=start and end<=h['end']]
            mask_hints=[h for h in covering if h['requested_disposition']=='MASK']
            keep_hints=[h for h in covering if h['requested_disposition']=='KEEP_CANDIDATE_ONLY']
            if sid in audit['quarantined_source_ids']:disposition,reason='REVIEW','SOURCE_HAS_UNRESOLVED_OR_INVALID_BOUNDARY_HINT'
            elif mask_hints and keep_hints:disposition,reason='REVIEW','KEEP_MASK_HINT_CONFLICT'
            elif mask_hints:disposition,reason='MASK','MECHANICALLY_LOCATED_LLM_DECISION_OR_RESULT_HINT'
            elif keep_hints:disposition,reason='REVIEW','KEEP_HINT_REQUIRES_INDEPENDENT_SEMANTIC_REVIEW'
            else:disposition,reason='REVIEW','NO_INDEPENDENT_VISIBILITY_PERMISSION'
            segment_id='SP-'+canonical_sha256([RULE_VERSION,event['event_id'],sid,start,end,disposition])[:24]
            segments.append(dict(segment_id=segment_id,source_id=sid,disposition=disposition,
                resolved_span=dict(start=start,end=end,exact_quote=source['text'][start:end])))
            audit['segment_decisions'].append(dict(segment_id=segment_id,source_id=sid,start=start,end=end,
                disposition=disposition,reason=reason,hint_ids=[h['hint_id'] for h in covering]))
    proposal=dict(schema_version=CONTRACT_VERSION,proposal_id='SPL-'+canonical_sha256([manifest_id,target,RULE_VERSION])[:24],
        proposal_version=RULE_VERSION,manifest_id=manifest_id,target=target,
        source_bindings=[dict(source_id=s['source_id'],source_version=s['source_version'],sha256=s['sha256']) for s in manifest['sources']],segments=segments)
    compiled=compile_partition(manifest,proposal)
    counts=Counter(p['disposition'] for s in compiled['sources'] for p in s['segments'])
    char_counts=Counter()
    for source in compiled['sources']:
        for span in source['segments']:char_counts[span['disposition']]+=span['end']-span['start']
    audit.update(preparation_status='PREPARED_RESEARCHER_REVIEW_REQUIRED',target=target,
        split_proposal_sha256=canonical_sha256(proposal),compiled_sha256=compiled['compiled_sha256'],
        disposition_segment_counts=dict(counts),disposition_character_counts=dict(char_counts),
        eligible_keep_hint_count=len(eligible_keeps),located_mask_hint_count=len(masks),
        material_release_permitted=False,unresolved_review_segment_count=counts['REVIEW'],
        all_selected_sources_exactly_partitioned=True)
    audit['audit_sha256']=canonical_sha256(audit)
    return manifest,proposal,compiled,audit
