"""Document semantic reconstruction; no reference labels are pipeline inputs."""
import copy
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from scripts.cohort_construction.p2_auto_checkpoint.extract import digest, clock, write
from scripts.cohort_construction.p2_auto_checkpoint.semantic import mask_text, patient_names
from .client import Client
from .context import build_units, context_for_discovery, build_evidence_context, time_order, unit_comparison_boundary, source_clock
from .source_time import source_record_time
from .prompts import DISCOVERY_INSTRUCTIONS, DISCOVERY_SCHEMA, REVIEW_INSTRUCTIONS, REVIEW_SCHEMA
from .validation import resolve_citations


def redact(value, names):
    if isinstance(value,str):return mask_text(value,names)
    if isinstance(value,list):return [redact(x,names) for x in value]
    if isinstance(value,dict):
        exact_keys={'source_id','source_ids','source_sha256','unit_id','event_id','candidate_id','local_id',
            'same_test_source_ids','block_id','block_ids','previous_event_id','same_process_previous_id',
            'same_event_with','discovery_request_sha256','source_id_index_path'}
        return {k:(v if k in exact_keys else redact(v,names)) for k,v in value.items()}
    return value


def resolve_model_quotes(quotes, sources, names, allowed=None):
    """Length-preserving display offsets are verified before mapping to raw text."""
    masked=[dict(source_id=s['source_id'],text=mask_text(s['text'],names)) for s in sources]
    result=resolve_citations(quotes,masked,allowed)
    indexed={s['source_id']:s for s in sources}
    # CE-AUD-008: even a rejected display lookup refers to an immutable raw
    # source.  Previously its source_sha256 silently denoted masked text while
    # valid/ambiguous entries denoted raw text.  Retain both scopes explicitly;
    # rejection never becomes evidence support or a verified raw quotation.
    for a in result['invalid']:
        source=indexed.get(a.get('source_id'))
        if source is None:continue
        a['lookup_display_source_sha256']=a.get('source_sha256')
        a['source_sha256']=source['source_sha256']
        a['submitted_masked_quote']=a.get('quote') or (a.get('citation') or {}).get('quote')
        a['display_quote_verified']=a.get('raw_quote_verified',False)
        a['raw_quote_verified']=False
        a['quote_text_scope']='length_preserving_masked_display'
    for kind in ['valid','ambiguous']:
        for a in result[kind]:
            s=indexed[a['source_id']];a['source_sha256']=s['source_sha256']
            a['submitted_masked_quote']=a.get('quote') or a['citation']['quote']
            if kind=='valid':a['quote']=s['text'][a['start']:a['end']]
            else:
                for occurrence in a['occurrences']:
                    occurrence['quote']=s['text'][occurrence['start']:occurrence['end']]
                # Different hidden identities must not be resolved by selecting
                # the first occurrence. Preserve source-level ambiguity.
                raw_quotes={x['quote'] for x in a['occurrences']}
                a['quote']=next(iter(raw_quotes)) if len(raw_quotes)==1 else None
            a['mapped_from_length_preserving_display']=True
    return result


def supported(citations):
    return bool(citations['valid'] or any(x.get('eligible_as_source_support') for x in citations['ambiguous']))


def discover_unit(unit,sources,config,client,names):
    context=context_for_discovery(unit,sources,config['discovery_context_characters'])
    payload=redact(dict(instruction=DISCOVERY_INSTRUCTIONS,unit_id=unit['unit_id'],**context),names)
    result,receipt=client.ask('document_event_discovery',payload,DISCOVERY_SCHEMA)
    def compile_response(response, response_receipt):
        compiled=compile_discovery_response(unit,sources,response,names,response_receipt)
        compiled['input_context_summary']={k:v for k,v in context.items() if k not in [
            'current_unit','prior_clinical_documents','background_clinical_documents',
            'prior_source_catalog','uncertain_time_source_catalog']}
        return compiled
    compiled=compile_response(result,receipt)
    if config.get('discovery_feedback_max_rounds',0):
        from .discovery_feedback import run_discovery_feedback
        compiled,audit=run_discovery_feedback(compiled,result,payload,compile_response,client,config)
        compiled['discovery_feedback_audit']=audit
    return compiled


def compile_discovery_response(unit,sources,result,names,receipt):
    """Recompile all event anchors before constructing any review evidence pool.

    A revised discovery response is a new proposal.  Its IDs, clinical-decision
    quotes and time evidence are all rebuilt; no original review context is
    carried into it.  The caller preserves both responses and request hashes.
    """
    events=[];used=set();byid={s['source_id']:s for s in sources}
    for raw in result['events']:
        c=copy.deepcopy(raw);actions=resolve_model_quotes(c.pop('action_quotes'),sources,names,
            {sid:[(0,len(byid[sid]['text']))] for sid in unit['source_ids']})
        pre=resolve_model_quotes(c.pop('prechoice_facts'),sources,names)
        post=resolve_model_quotes(c.pop('postchoice_facts'),sources,names)
        timing=resolve_model_quotes(c.pop('time_quotes',[]),sources,names)
        boundaries=resolve_model_quotes(c.pop('choice_boundary_quotes',[]),sources,names,
            {sid:[(0,len(byid[sid]['text']))] for sid in unit['source_ids']})
        sid=next((q['source_id'] for q in actions['valid']),None)
        sid=sid or next((q['source_id'] for q in actions['ambiguous']),unit['source_ids'][0])
        identity=digest([unit['unit_id'],raw['action_quotes']])[:20]
        eid='DE'+identity
        if eid in used:
            eid='DE'+digest([identity,raw['local_id'],len(events)])[:20]
            c['uncertainty'].append('DUPLICATE_ACTION_ANCHOR_REQUIRES_SEMANTIC_IDENTITY_REVIEW')
        used.add(eid)
        c.update(event_id=eid,candidate_id=eid,source_id=sid,source_ids=unit['source_ids'],unit_id=unit['unit_id'],
            kind=unit['kind'],document_profile=unit['document_profile'],type_prior=unit['type_prior'],
            source_document_clock=source_clock(byid[sid]),source_document_clock_raw=byid[sid]['clock'],
            source_record_time_provenance=source_record_time(byid[sid]),
            action_citations=actions,choice_boundary_citations=boundaries,prechoice_citations=pre,
            postchoice_citations=post,time_citations=timing,discovery_request_sha256=receipt['request_sha256'],
            discovery_source_supported=supported(actions),unit_hints=unit.get('hints',[]))
        # A model can decide event identity, but it cannot turn a report's time
        # into an absent choice timestamp or an operation start into a minute.
        c['model_time_proposal']=dict(choice_time=c['choice_time'],time_basis=c['time_basis'])
        if unit['kind']=='performed_test_report':
            c.update(choice_time=None,time_basis='historical_unknown',same_test_source_ids=unit['source_ids'],
                time_upper_bound=unit['clock'],selection_basis='performed_test_report')
        from .temporal import verify_choice_time
        checked=verify_choice_time(c,sources,timing)
        c['time_validation']=checked
        c['choice_time']=checked['choice_time'];c['time_basis']=checked['time_basis']
        c['uncertainty'].extend(checked['uncertainty'])
        c['decision_clock']=clock(c['choice_time'])
        if actions['valid']:c['anchor']=actions['valid'][0]
        if not supported(actions):c['uncertainty'].append('CURRENT_ACTION_SOURCE_UNVERIFIED')
        events.append(c)
    rejected=[dict(r,validated_quotes=resolve_model_quotes(r['quotes'],sources,names)) for r in result['rejected_mentions']]
    return dict(unit_id=unit['unit_id'],events=events,rejected_mentions=rejected,receipt=receipt)


def chronology(event, bys):
    if event.get('choice_time'):return clock(event['choice_time'])
    if event.get('time_upper_bound'):return event['time_upper_bound']
    if event.get('time_basis')=='operative_phase':
        text=bys[event['source_id']]['text'];m=re.search(r'手术时间[：:]\s*(20\d\d-\d\d-\d\d \d\d:\d\d)',text)
        return clock(m[1] if m else None)
    return event['source_document_clock']


def select_status(event, review, observations, connections, earlier_ids, identity_ids=None):
    """Enforce source/contract boundaries without replacing semantic decisions."""
    reasons=[]
    if not event['discovery_source_supported']:reasons.append('CURRENT_ACTION_SOURCE_UNVERIFIED')
    if review.get('previous_event_id') and review['previous_event_id'] not in earlier_ids:
        reasons.append('UNPROVIDED_OR_NONPRIOR_PREVIOUS_EVENT_ID')
    if review.get('same_process_previous_id') and review['same_process_previous_id'] not in earlier_ids:
        reasons.append('UNPROVIDED_OR_NONPRIOR_SAME_PROCESS_PREVIOUS_ID')
    if any(e not in (identity_ids or earlier_ids) and e!=event['event_id'] for e in review['same_event_with']):
        reasons.append('UNPROVIDED_SAME_EVENT_ID')
    if review.get('scope')!='yes':
        return ('NOT_RELATED' if review.get('scope')=='no' else 'UNCERTAIN_SCOPE'),reasons
    proposed=review['checkpoint']
    if proposed=='repeat':return 'REPEATED_OR_EXECUTED_MENTION',reasons
    if proposed=='not_related':return 'NOT_RELATED',reasons
    if proposed=='uncertain':return 'UNCERTAIN_SEMANTIC_JUDGMENT',reasons
    test_anchor=proposed=='first_anchor' and event.get('kind')=='performed_test_report'
    if review.get('record_function') in ['repeat','execution','generic','uncertain'] and not test_anchor:
        reasons.append('MODEL_EVENT_FUNCTION_CHECKPOINT_CONFLICT')
    if proposed=='new_evidence':
        if review['evidence_evolution']!='new_relevant':reasons.append('MODEL_NEWNESS_CONFLICT')
        if not supported(observations):reasons.append('NO_VALID_PRIOR_OBSERVATION')
        if not review.get('previous_event_id'):reasons.append('PRIOR_ROUND_UNRESOLVED')
        if review.get('evidence_connection',review['process_link'])!='supported':reasons.append('EVIDENCE_CONNECTION_UNCERTAIN')
        if not supported(connections):reasons.append('NO_VERIFIED_CONNECTION_QUOTE')
        if 'evidence_comparison' in review:
            if review['evidence_comparison']['change_kind'] not in ['new_measurement','changed_state','explicit_reinterpretation']:
                reasons.append('COMPARISON_DOES_NOT_ESTABLISH_NEW_INFORMATION')
            reasons.extend(event.get('semantic_contract_validation',[]))
    if proposed=='first_anchor' and review.get('previous_event_id'):
        reasons.append('FIRST_ANCHOR_HAS_PRIOR_ROUND')
    if reasons:return 'UNCERTAIN_CONTRACT_OR_SOURCE',reasons
    if proposed=='first_anchor':
        return ('CANDIDATE_TEST_ANCHOR_TIME_UNCERTAIN' if event['kind']=='performed_test_report' else 'CANDIDATE_FIRST_ANCHOR'),reasons
    return 'CANDIDATE_NEW_EVIDENCE',reasons


def _allowed_connection_ranges(event, context, sources):
    allowed=copy.deepcopy(context['allowed_spans']);byid={s['source_id']:s for s in sources}
    for sid in event['source_ids']:
        ends=[q['end'] for q in event['action_citations']['valid'] if q['source_id']==sid]
        if ends:
            end=max(ends);text=byid[sid]['text']
            # A source quote can omit the terminal punctuation while a valid
            # connection quote includes it. Expand only across whitespace and
            # closing/sentence punctuation, never into later clinical words.
            while end<len(text) and (text[end].isspace() or text[end] in '。！？!?；;，,：:、.）)]】》”’\"\''):
                end+=1
            regions=[(0,end)]
            # Explicitly separated downstream facts can precede the narrative
            # choice in a retrospective note. Prefix order does not make those
            # declared postoperative results eligible connection evidence.
            for q in event.get('postchoice_citations',{}).get('valid',[]):
                if q['source_id']!=sid:continue
                next_regions=[]
                for low,high in regions:
                    if q['end']<=low or q['start']>=high:next_regions.append((low,high))
                    else:
                        if low<q['start']:next_regions.append((low,q['start']))
                        if q['end']<high:next_regions.append((q['end'],high))
                regions=next_regions
            allowed.setdefault(sid,[]).extend(dict(start=low,end=high) for low,high in regions)
    return allowed


def earlier_ledger(unit,events,bys):
    # Source-chronology offers alternatives, not a claimed true clinical order.
    ledger=[]
    boundary,_=unit_comparison_boundary(unit)
    for e in events:
        sibling=e['unit_id']==unit['unit_id']
        if sibling and not (e.get('choice_time') and e.get('time_basis')=='explicit_documented_choice'):continue
        t=chronology(e,bys)
        if not sibling and time_order(t,boundary)!='BEFORE':continue
        ledger.append(dict(event_id=e['event_id'],source_id=e['source_id'],source_document_clock=e['source_document_clock'],
            choice_time=e['choice_time'],time_basis=e['time_basis'],phase=e['phase'],scope=e['scope'],
            record_function=e['record_function'],management_question=e['management_question'],action=e['action'],
            action_quotes=e['action_citations']['valid'],comparison_order_certified=False,same_current_unit=sibling,
            historical_or_test_time_unknown=e['time_basis'] in ['historical_unknown','unknown']))
    return sorted(ledger,key=lambda e:(e['source_document_clock'].get('value') or '9999',e['event_id']))


def eligible_prior_ids(event, ledger, all_events, sources, context):
    """A unit's record-time ledger is only a superset, never a time license."""
    byevent={e['event_id']:e for e in all_events};bys={s['source_id']:s for s in sources}
    eligible=set()
    for item in ledger:
        previous=byevent.get(item['event_id'])
        if not previous or previous['event_id']==event['event_id']:continue
        if previous['unit_id']==event['unit_id'] and not (
            event.get('time_basis')==previous.get('time_basis')=='explicit_documented_choice'
            and event.get('choice_time') and previous.get('choice_time')):
            continue
        if time_order(chronology(previous,bys),context.get('cutoff'))=='BEFORE':
            eligible.add(previous['event_id'])
    return eligible


def validate_semantic_contract(event, review, context, sources, all_events, names):
    """Check semantic proof provenance, never infer novelty from word overlap.

    Whether facts are new, relevant or explicitly connected remains the model's
    judgment. This rejects unsupported claims of the required proof structure.
    """
    if 'evidence_comparison' not in review:
        return {}, [], []  # Legacy synthetic fixtures / archived records.
    byevent={e['event_id']:e for e in all_events}
    previous=byevent.get(review.get('previous_event_id'))
    prior_allowed={}
    if previous:
        old_context=build_evidence_context(previous,sources,all_events,budget=150000,
            preserve_current_context=True)
        prior_allowed=_allowed_connection_ranges(previous,old_context,sources)
    current_allowed=context['allowed_spans']
    connection_allowed=_allowed_connection_ranges(event,context,sources)
    comp=review['evidence_comparison']
    validated={
        'previous_known':resolve_model_quotes(comp['previous_known_quotes'],sources,names,prior_allowed),
        'current_information':resolve_model_quotes(comp['current_information_quotes'],sources,names,current_allowed),
        'reinterpretation':resolve_model_quotes(comp['reinterpretation_quotes'],sources,names,connection_allowed),
    }
    availability=[];known_current=[]
    def exact_entries(c):
        return c.get('valid',[])
    for item in review.get('observation_availability',[]):
        observation=resolve_model_quotes([item['observation']],sources,names,current_allowed)
        basis=resolve_model_quotes(item['basis_quotes'],sources,names,connection_allowed)
        declared_observation=item['observation'] in review.get('observation_quotes',[])
        explicit=(item['availability']=='explicitly_known_before' and supported(observation) and supported(basis)
                  and declared_observation)
        availability.append(dict(**item,validated_observation=observation,validated_basis=basis,
            is_declared_observation=declared_observation,eligible_as_known_prechoice=explicit,
            semantic_claim_not_independently_validated=True))
        if explicit:known_current.extend(exact_entries(observation))
    # A known old symptom cannot stand in for the alleged new information.
    def covered(a):
        end=a['start']
        for low,high in sorted((b['start'],b['end']) for b in known_current if a['source_id']==b['source_id']):
            if low>end:break
            end=max(end,high)
            if end>=a['end']:return True
        return False
    current_entries=exact_entries(validated['current_information'])
    known_new=bool(current_entries) and all(covered(a) for a in current_entries)
    reasons=[]
    if review['checkpoint']=='new_evidence':
        if not supported(validated['previous_known']):reasons.append('PREVIOUS_KNOWLEDGE_PROOF_MISSING_OR_OUTSIDE_PRIOR_ROUND')
        if not supported(validated['current_information']):reasons.append('CURRENT_COMPARISON_PROOF_MISSING_OR_OUTSIDE_EVIDENCE')
        if comp['change_kind']=='explicit_reinterpretation' and not supported(validated['reinterpretation']):
            reasons.append('EXPLICIT_REINTERPRETATION_HAS_NO_SOURCE_PROOF')
        if not known_new:reasons.append('NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED')
    validated['known_current_information_supported']=known_new
    return validated,availability,reasons


def dimension_audit(event, review, sources, names, reasons):
    """Keep model dimensions and mechanical overrides separately auditable."""
    raw=copy.deepcopy(review.get('uncertainty_dimensions',{}));final=copy.deepcopy(raw)
    citations={key:resolve_model_quotes(value.get('source_quotes',[]),sources,names)
               for key,value in raw.items()}
    if raw and event.get('time_basis')!='explicit_documented_choice':
        old=final['choice_time']
        if old.get('status')!='uncertain':
            final['choice_time']=dict(status='uncertain',reason_category='model_conflict',
                explanation='模型把实际选择时间标成确定，但其实际时刻未通过核验；不能据此断言原文确实未提供。原模型判断另存。',
                source_quotes=old.get('source_quotes',[]))
    if raw and 'NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED' in reasons:
        old=final['source_availability']
        if old.get('status')!='uncertain':
            final['source_availability']=dict(status='uncertain',reason_category='model_conflict',
                explanation='新增比较事实未全部获得选择前已知依据，模型确定声明与引用核验不一致；不据此归咎源资料缺失。',
                source_quotes=old.get('source_quotes',[]))
    return raw,final,citations


def review_unit(unit,unit_events,all_events,sources,config,client,names):
    bys={s['source_id']:s for s in sources}
    ledger=earlier_ledger(unit,all_events,bys)
    sibling_ids={e['event_id'] for e in unit_events}
    contexts={e['event_id']:build_evidence_context(e,sources,all_events,
        budget=config['review_context_characters'],
        preserve_current_context=config.get('preserve_current_prechoice_context',False)) for e in unit_events}
    event_prior_ids={e['event_id']:eligible_prior_ids(e,ledger,all_events,sources,contexts[e['event_id']]) for e in unit_events}
    # Each event keeps a distinct allowlist even though common original source
    # ranges are transferred once. No selected source is silently re-filtered.
    pool={};event_inputs=[]
    for e in unit_events:
        ctx=contexts[e['event_id']];keys=[]
        for b in ctx['blocks']:
            key=b['source_id']+':'+str(b['start'])+':'+str(b['end']);keys.append(key)
            pool[key]=dict(block_id=key,source_id=b['source_id'],start=b['start'],end=b['end'],
                text=b['quote'],kind=b['kind'],source_title=b['title'],clock=b['clock'],
                interval=b['interval'],availability_certified=False,priority=b['priority'],
                availability_status=b.get('availability_status','PRIOR_SOURCE_PROXY_UNCERTIFIED'),
                admissibility_role=b.get('admissibility_role','source_context_requires_semantic_time_review'))
        event_inputs.append(dict(event={k:e[k] for k in ['event_id','source_id','scope','record_function',
                'management_question','action','choice_time','time_basis','phase','uncertainty','action_citations']},
            evidence_context=dict(block_ids=keys,cutoff=ctx['cutoff'],cutoff_basis=ctx['cutoff_basis'],
                eligible_previous_event_ids=sorted(event_prior_ids[e['event_id']]),
                uncertainty={k:v for k,v in ctx['uncertainty'].items() if k!='current_prechoice_context_preserved'},
                budget=ctx['budget'])))
    payload=dict(instruction=REVIEW_INSTRUCTIONS,unit_id=unit['unit_id'],document_profile=unit['document_profile'],
        type_prior=unit['type_prior'],current_full_document=unit['original_texts'],
        reconstruction_only_actual_choices_disclosed=True,earlier_event_ledger=ledger,
        events=event_inputs,evidence_source_pool=list(pool.values()))
    # Apply the provider request budget to serialized text, not just raw source
    # characters. Remove whole source ranges with per-event receipts, never
    # silently truncate a document or claim omitted evidence was absent.
    limit=config.get('max_review_payload_characters',240000)
    size=len(json.dumps(payload,ensure_ascii=False));removed=[]
    for block in sorted(pool.values(),key=lambda b:(b['priority'],b['clock'].get('value') or ''),reverse=True):
        if size<=limit-2000:break
        key=block['block_id'];del pool[key];removed.append(key)
        for inp in event_inputs:
            if key in inp['evidence_context']['block_ids']:inp['evidence_context']['block_ids'].remove(key)
        payload['evidence_source_pool']=list(pool.values())
        size=len(json.dumps(payload,ensure_ascii=False))
    if size>limit:raise ValueError('FULL_DOCUMENT_AND_EVENT_LEDGER_EXCEED_REVIEW_BUDGET')
    for e in unit_events:
        ctx=contexts[e['event_id']]
        kept=[];allowed={}
        for b in ctx['blocks']:
            key=b['source_id']+':'+str(b['start'])+':'+str(b['end'])
            if key not in pool:
                ctx['ledger'].append(dict(source_id=b['source_id'],start=b['start'],end=b['end'],
                    disposition='omitted_provider_payload_budget',whole_source_not_truncated=True))
                ctx['uncertainty']['source_coverage_complete']=False
                ctx['uncertainty']['omitted_budget_count']+=1
            else:
                kept.append(b);allowed.setdefault(b['source_id'],[]).append(dict(start=b['start'],end=b['end']))
        ctx['blocks']=kept;ctx['allowed_spans']=allowed
        ctx['actual_payload_characters']=size;ctx['provider_budget_omitted_blocks']=len(removed)
        inp=next(i for i in event_inputs if i['event']['event_id']==e['event_id'])
        inp['evidence_context']['uncertainty']={k:v for k,v in ctx['uncertainty'].items() if k!='current_prechoice_context_preserved'}
    final_size=len(json.dumps(payload,ensure_ascii=False))
    if final_size>limit:raise ValueError('SERIALIZED_REVIEW_PAYLOAD_EXCEEDS_BUDGET')
    for ctx in contexts.values():ctx['actual_payload_characters']=final_size
    result,receipt=client.ask('document_checkpoint_review',redact(payload,names),REVIEW_SCHEMA)
    counts={eid:sum(r['event_id']==eid for r in result['items']) for eid in sibling_ids}
    reviewed={r['event_id']:r for r in result['items'] if counts.get(r['event_id'])==1};out=[]
    for event in unit_events:
        e=copy.deepcopy(event);ctx=contexts[e['event_id']];r=reviewed.get(e['event_id'])
        earlier_ids=event_prior_ids[e['event_id']]
        if r is None:
            id_error='MODEL_DUPLICATED_EVENT_ID' if counts[e['event_id']]>1 else 'MODEL_OMITTED_EVENT_ID'
            r=dict(event_id=e['event_id'],scope=e['scope'],record_function='uncertain',checkpoint='uncertain',
                previous_event_id=None,same_event_with=[],same_process_previous_id=None,process_link='uncertain',
                evidence_evolution='uncertain',observation_quotes=[],connection_quotes=[],
                rationale=id_error,uncertainty=[id_error])
        observations=resolve_model_quotes(r['observation_quotes'],sources,names,ctx['allowed_spans'])
        connections=resolve_model_quotes(r['connection_quotes'],sources,names,
            _allowed_connection_ranges(e,ctx,sources))
        comparison,availability,contract_reasons=validate_semantic_contract(e,r,ctx,sources,all_events,names)
        e['semantic_contract_validation']=contract_reasons
        status,reasons=select_status(e,r,observations,connections,earlier_ids,earlier_ids|sibling_ids)
        raw_dimensions,dimensions,dimension_citations=dimension_audit(e,r,sources,names,reasons)
        # The final semantic classification is the review's; never silently
        # retain the lexical/discovery classification over its correction.
        e.update(scope=r['scope'],record_function=r['record_function'],semantic_review=r,
            checkpoint_status=status,validation_reasons=reasons,
            validated_observation_citations=observations,validated_connection_citations=connections,
            previous_event_id=r['previous_event_id'],same_process_previous_id=r['same_process_previous_id'],
            provided_prior_event_ids=sorted(earlier_ids),provided_identity_event_ids=sorted(earlier_ids|sibling_ids),
            evidence_context=ctx,actual_model_evidence=ctx['blocks'],review_request_sha256=receipt['request_sha256'],
            legacy_uncertainty_fields=dict(source_support=bool(supported(e['action_citations'])),
                event_identity=r['record_function'],time_basis=e['time_basis'],
                source_availability='proxy_not_certified',evidence_evolution=r['evidence_evolution'],
                connection=r['process_link'],technical_validation=reasons,
                source_context_omissions=ctx['uncertainty']['omitted_budget_count']),
            semantic_uncertainty_dimensions=raw_dimensions,uncertainty_dimensions=dimensions,
            uncertainty_dimension_citations=dimension_citations,
            comparison_validation=comparison,availability_validation=availability,
            blind_replay_ready=False,expert_reference=False)
        # A source-level duplicate citation is preserved but not used as an
        # invented exact cut point. Errors are local, not whole-call failures.
        e['citation_warnings']=dict(action_invalid=len(e['action_citations']['invalid']),
            action_ambiguous=len(e['action_citations']['ambiguous']),
            observation_invalid=len(observations['invalid']),observation_ambiguous=len(observations['ambiguous']),
            connection_invalid=len(connections['invalid']),connection_ambiguous=len(connections['ambiguous']))
        out.append(e)
    id_audit=dict(missing_event_ids=sorted(eid for eid,n in counts.items() if n==0),
        duplicated_event_ids=sorted(eid for eid,n in counts.items() if n>1),
        unexpected_event_ids=sorted({r['event_id'] for r in result['items']}-sibling_ids))
    output=dict(events=out,receipt=receipt,unexpected_event_ids=id_audit['unexpected_event_ids'],
        initial_review_id_audit=id_audit)
    if config.get('review_feedback_max_rounds',0):
        from .review_feedback import run_review_feedback
        revised,audit=run_review_feedback(out,redact(payload,names),all_events,sources,names,client,config)
        output.update(events=revised,original_events=out,review_feedback_audit=audit)
    return output


def assemble_processes(events,final_graph_validation=False):
    """Same management process is separate from identity/same specific task."""
    byid={e['event_id']:e for e in events};parent={eid:eid for eid in byid};links=[]
    if final_graph_validation:
        from .final_graph import relation_guard
    identity_parent={eid:eid for eid in byid}
    def identity_root(eid):
        while identity_parent[eid]!=eid:
            identity_parent[eid]=identity_parent[identity_parent[eid]];eid=identity_parent[eid]
        return eid
    def root(eid):
        while parent[eid]!=eid:
            parent[eid]=parent[parent[eid]];eid=parent[eid]
        return eid
    for e in events:
        r=e.get('semantic_review',{});p=r.get('same_process_previous_id')
        if p and p in byid and p!=e['event_id']:
            support=(supported(e['validated_connection_citations']) and
                e.get('discovery_source_supported',True) and byid[p].get('discovery_source_supported',True) and
                p in e.get('provided_prior_event_ids',[p])) and not any(
                x.startswith('UNPROVIDED_OR_NONPRIOR') or x=='CURRENT_ACTION_SOURCE_UNVERIFIED'
                for x in e.get('validation_reasons',[]))
            linked=r['process_link']=='supported' and support
            guard=relation_guard(e,p,'same_management_process',byid) if final_graph_validation else None
            if guard is not None:linked=linked and guard['allowed']
            link=dict(source_event_id=p,target_event_id=e['event_id'],relation='same_management_process',
                status='supported' if linked else 'uncertain',connection_citations=e['validated_connection_citations'])
            if guard is not None:link['final_graph_validation']=guard
            links.append(link)
            if linked:parent[root(e['event_id'])]=root(p)
        for p in r.get('same_event_with',[]):
            if p in byid and p!=e['event_id']:
                valid_identity=(supported(e['validated_connection_citations']) and
                    e.get('discovery_source_supported',True) and byid[p].get('discovery_source_supported',True) and
                    p in e.get('provided_identity_event_ids',[p]) and
                    'UNPROVIDED_SAME_EVENT_ID' not in e.get('validation_reasons',[]))
                guard=relation_guard(e,p,'same_event',byid) if final_graph_validation else None
                if guard is not None:valid_identity=valid_identity and guard['allowed']
                link=dict(source_event_id=p,target_event_id=e['event_id'],relation='same_event',
                    status='supported' if valid_identity else 'uncertain',
                    connection_citations=e['validated_connection_citations'])
                if guard is not None:link['final_graph_validation']=guard
                links.append(link)
                if valid_identity:
                    parent[root(e['event_id'])]=root(p)
                    identity_parent[identity_root(e['event_id'])]=identity_root(p)
    identities={}
    for e in events:identities.setdefault(identity_root(e['event_id']),[]).append(e)
    for members in identities.values():
        candidates=[e for e in members if e['checkpoint_status'].startswith('CANDIDATE_')]
        if not candidates:continue
        canonical=min(candidates,key=lambda e:((e.get('source_document_clock') or {}).get('value') or '9999',e['event_id']))
        for e in members:
            e['canonical_event_id']=canonical['event_id']
            if e is not canonical and e['checkpoint_status'].startswith('CANDIDATE_'):
                e['status_before_semantic_identity_grouping']=e['checkpoint_status']
                e['checkpoint_status']='GROUPED_SAME_EVENT_MENTION'
    groups={}
    for e in events:
        if e['scope']=='no' or e['record_function']=='generic':continue
        groups.setdefault(root(e['event_id']),[]).append(e)
    episodes=[]
    for members in groups.values():
        selected=[e for e in members if e['checkpoint_status'].startswith('CANDIDATE_')]
        if not selected:continue
        eid='EP'+digest(sorted(e['event_id'] for e in members))[:20]
        ids=[e['event_id'] for e in members]
        episodes.append(dict(episode_id=eid,status='candidate_management_process_not_clinical_reference',
            candidate_ids=ids,checkpoint_ids=[e['event_id'] for e in selected],
            management_questions=sorted({e['management_question'] for e in members}),
            uncertain_links=[l for l in links if l['status']=='uncertain' and (l['source_event_id'] in ids or l['target_event_id'] in ids)]))
        for e in members:e['episode_id']=eid
    return episodes,links


def process_sources(case_id,sources,config,out,cache,workers=4):
    # A misspelled or excessive repair limit must fail before any model request.
    from .review_feedback import validate_feedback_config
    validate_feedback_config(config)
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    routing=[]
    units=build_units(sources,config,routing_audit=routing) if config.get('include_support_documents',False) else build_units(sources,config)
    inventory=dict(version=config['version'],units=units)
    if config.get('include_support_documents',False):inventory['source_routing_audit']=routing
    write(out/'unit_inventory.json',inventory)
    names=patient_names(sources);client=Client(cache,config);discovered=[];failures=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(discover_unit,u,sources,config,client,names):u for u in units}
        for f in as_completed(futures):
            u=futures[f]
            try:discovered.append(f.result())
            except Exception as exc:failures.append(dict(unit_id=u['unit_id'],stage='discovery',error=type(exc).__name__,detail=str(exc)[:150]))
            write(out/'progress.json',dict(stage='discovery',completed=len(discovered)+len(failures),total=len(units),failures=len(failures)))
            write(out/'discovery_partial.json',dict(units=discovered,failures=failures))
    discovered.sort(key=lambda d:d['unit_id'])
    all_events=sorted([e for d in discovered for e in d['events']],key=lambda e:e['event_id'])
    write(out/'discovery.json',dict(events=all_events,units=discovered,failures=failures))
    byunit={u['unit_id']:u for u in units};groups={}
    for e in all_events:groups.setdefault(e['unit_id'],[]).append(e)
    reviewed=[];review_audits=[];original_reviews=[];review_id_audits=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(review_unit,byunit[uid],items,all_events,sources,config,client,names):uid for uid,items in groups.items()}
        for f in as_completed(futures):
            uid=futures[f]
            try:
                result=f.result();reviewed.extend(result['events'])
                if result.get('initial_review_id_audit'):
                    review_id_audits.append(dict(unit_id=uid,**result['initial_review_id_audit']))
                if 'review_feedback_audit' in result:
                    review_audits.append(dict(unit_id=uid,**result['review_feedback_audit']))
                    original_reviews.extend(result['original_events'])
            except Exception as exc:
                failures.append(dict(unit_id=uid,stage='review',error=type(exc).__name__,detail=str(exc)[:150]))
                for e in groups[uid]:
                    reviewed.append(dict(e,checkpoint_status='UNCERTAIN_REVIEW_TRANSPORT_OR_SCHEMA',
                        semantic_review={},validated_observation_citations=dict(valid=[],ambiguous=[],invalid=[]),
                        validated_connection_citations=dict(valid=[],ambiguous=[],invalid=[]),blind_replay_ready=False))
            write(out/'progress.json',dict(stage='review',completed_units=sum(1 for x in futures if x.done()),total=len(groups),failures=len(failures)))
            write(out/'decision_events_partial.json',reviewed)
    reviewed.sort(key=lambda e:(chronology(e,{s['source_id']:s for s in sources})['value'] or '9999',e['event_id']))
    if config.get('final_graph_validation',False):
        from .final_graph import audit_final_graph
        reviewed,graph_audit=audit_final_graph(reviewed)
        write(out/'final_graph_audit.json',graph_audit)
    episodes,links=assemble_processes(reviewed,final_graph_validation=config.get('final_graph_validation',False))
    write(out/'initial_review_id_audits.json',review_id_audits)
    if config.get('review_feedback_max_rounds',0):
        write(out/'review_feedback_audits.json',review_audits)
        write(out/'reviews_before_feedback.json',original_reviews)
    write(out/'decision_events.json',reviewed);write(out/'episodes.json',episodes);write(out/'relations.json',links)
    write(out/'failures.json',failures);write(out/'model_receipts.json',client.receipts)
    from collections import Counter
    summary=dict(case_id=case_id,version=config['version'],whole_document_or_test_units=len(units),
        discovered_events=len(all_events),reviewed_events=len(reviewed),
        checkpoint_statuses=dict(Counter(e['checkpoint_status'] for e in reviewed)),
        candidate_checkpoints=sum(e['checkpoint_status'].startswith('CANDIDATE_') for e in reviewed),
        candidate_episodes=len(episodes),process_links=len(links),model_calls=len(client.receipts),
        cached_calls=sum(r['cached'] for r in client.receipts),failures=len(failures),
        clinical_accuracy_measured=False,blind_packets_created=False,reference_read_by_pipeline=False)
    if config.get('final_graph_validation',False):
        summary['final_graph_audit']={k:graph_audit[k] for k in (
            'version','event_count','demoted_checkpoint_count','blocked_relation_count')}
    if config.get('review_feedback_max_rounds',0):
        summary['review_feedback_groups_attempted']=sum(a.get('attempted',False) for a in review_audits)
        summary['review_feedback_statuses']=dict(Counter(a['status'] for a in review_audits))
        summary['review_feedback_event_results']=dict(Counter(
            e['review_refinement']['status'] for e in reviewed if e.get('review_refinement')))
    if config.get('discovery_feedback_max_rounds',0):
        summary['discovery_feedback_units_attempted']=sum(bool(d.get('discovery_feedback_audit',{}).get('rounds_attempted',0)) for d in discovered)
        summary['discovery_feedback_statuses']=dict(Counter(
            d['discovery_feedback_audit']['status'] for d in discovered if d.get('discovery_feedback_audit')))
    write(out/'summary.json',summary);write(out/'progress.json',dict(stage='complete',**summary))
    return reviewed,episodes,summary
