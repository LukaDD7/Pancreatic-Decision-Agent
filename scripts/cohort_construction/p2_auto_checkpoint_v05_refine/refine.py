"""Repair a model's proof citations once using its exact original evidence pool.

No reference labels, metrics, later cases, or additional source material are
loaded. A model may correct a quote or classification, or remain uncertain.
Every returned proposal goes through the same frozen validation as the initial
review. A failed request or an omitted/duplicated target never creates a positive.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import shutil
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from scripts.cohort_construction.p2_auto_checkpoint.extract import digest, read, write
from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources, build, validate
from scripts.cohort_construction.p2_auto_checkpoint.semantic import patient_names, mask_text
from scripts.cohort_construction.p2_auto_checkpoint_v04.engine import (
    resolve_model_quotes, _allowed_connection_ranges, validate_semantic_contract,
    select_status, dimension_audit, assemble_processes, supported, redact,
)
from scripts.cohort_construction.p2_auto_checkpoint_v04.prompts import REVIEW_SCHEMA


REPAIR_INSTRUCTIONS = """
本轮是唯一一次有条件的来源/引用/证明修复，不是要求改成阳性。
只为本次events中的event_id各返回一条items，不新增事件、不改ID、不使用外部或后续信息。
原始全文、证据池、block_ids和前轮允许集合保持原样；validation_feedback指出上一提案哪些引用或证明未通过。
重新核对原文与允许范围，逐字给出完整但最短的合适引用；禁止把实际动作或选择后信息当作选择前观察。
节点action证明可以包含实际选择，不能因此整体复制为observation；若允许池中有独立的观察文字，可重新逐字引用它。
不要机械裁剪引文，也不要为通过校验编造依据。若原文/允许池仍不足，保留uncertain并说明缺口。
可以修正scope、record_function、checkpoint、前轮、连接和不确定性分类；不要求保持原提案的阳性判断。
previous_event_id和same_process_previous_id仅取各event.evidence_context.eligible_previous_event_ids。
同一完整review schema继续适用；返回JSON，不评价治疗正确性。
"""

_REPAIRABLE_REASONS = {
    'NO_VALID_PRIOR_OBSERVATION', 'NO_VERIFIED_CONNECTION_QUOTE',
    'PREVIOUS_KNOWLEDGE_PROOF_MISSING_OR_OUTSIDE_PRIOR_ROUND',
    'CURRENT_COMPARISON_PROOF_MISSING_OR_OUTSIDE_EVIDENCE',
    'EXPLICIT_REINTERPRETATION_HAS_NO_SOURCE_PROOF',
    'UNPROVIDED_OR_NONPRIOR_PREVIOUS_EVENT_ID',
    'UNPROVIDED_OR_NONPRIOR_SAME_PROCESS_PREVIOUS_EVENT_ID',
    'UNPROVIDED_OR_NONPRIOR_SAME_PROCESS_PREVIOUS_ID',
    'MODEL_EVENT_FUNCTION_CHECKPOINT_CONFLICT', 'FIRST_ANCHOR_HAS_PRIOR_ROUND',
}


def repair_triggers(event):
    """Select proof/contract failures, not pure clinical semantic uncertainty."""
    if event.get('review_refinement', {}).get('attempted'):
        return []
    if not event.get('semantic_review') or not event.get('review_request_sha256'):
        return []
    review = event['semantic_review']
    if (event.get('checkpoint_status') != 'UNCERTAIN_CONTRACT_OR_SOURCE'
            or review.get('checkpoint') not in ('first_anchor', 'new_evidence')
            or not event.get('discovery_source_supported')):
        return []
    reasons = set(event.get('validation_reasons', []))
    result = sorted(reasons & _REPAIRABLE_REASONS)
    if review['checkpoint'] == 'first_anchor':
        return sorted(set(result) & {'MODEL_EVENT_FUNCTION_CHECKPOINT_CONFLICT',
            'FIRST_ANCHOR_HAS_PRIOR_ROUND', 'UNPROVIDED_OR_NONPRIOR_PREVIOUS_EVENT_ID',
            'UNPROVIDED_OR_NONPRIOR_SAME_PROCESS_PREVIOUS_ID'})
    for field in ('validated_observation_citations', 'validated_connection_citations'):
        blocking = ('NO_VALID_PRIOR_OBSERVATION' in reasons or 'NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED' in reasons
            if field == 'validated_observation_citations' else 'NO_VERIFIED_CONNECTION_QUOTE' in reasons)
        if blocking and event.get(field, {}).get('invalid'):
            result.append(field + ':INVALID_CITATION')
    comparison_blockers = {
        'previous_known': {'PREVIOUS_KNOWLEDGE_PROOF_MISSING_OR_OUTSIDE_PRIOR_ROUND'},
        'current_information': {'CURRENT_COMPARISON_PROOF_MISSING_OR_OUTSIDE_EVIDENCE',
                                'NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED'},
        'reinterpretation': {'EXPLICIT_REINTERPRETATION_HAS_NO_SOURCE_PROOF'},
    }
    for key, proof in event.get('comparison_validation', {}).items():
        if reasons & comparison_blockers.get(key, set()) and isinstance(proof, dict) and proof.get('invalid'):
            result.append('comparison_validation.' + key + ':INVALID_CITATION')
    availability = event.get('availability_validation', [])
    if 'NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED' in reasons:
        # A well-formed explicit statement that availability is unknown is not
        # a technical error. Missing/mismatched/invalid proof can be repaired.
        if not availability or any(
            not a.get('is_declared_observation', True)
            or a.get('validated_observation', {}).get('invalid')
            or a.get('validated_basis', {}).get('invalid')
            or (a.get('availability') == 'explicitly_known_before' and not a.get('eligible_as_known_prechoice'))
            for a in availability
        ):
            result.append('AVAILABILITY_PROOF_MISSING_OR_MALFORMED')
        declared = review.get('observation_quotes', [])
        if any(not any(a.get('observation') == q for a in availability) for q in declared):
            result.append('OBSERVATION_AVAILABILITY_RECORD_MISSING')
        if availability and all(a.get('availability') == 'explicitly_known_before' for a in availability):
            result.append('EXPLICIT_KNOWN_COMPARISON_COVERAGE_MISMATCH')
    return sorted(set(result))


def feedback(event):
    return dict(event_id=event['event_id'], triggers=repair_triggers(event),
                checkpoint_status=event.get('checkpoint_status'),
                validation_reasons=event.get('validation_reasons', []),
                observation_validation=event.get('validated_observation_citations', {}),
                connection_validation=event.get('validated_connection_citations', {}),
                comparison_validation=event.get('comparison_validation', {}),
                availability_validation=event.get('availability_validation', []),
                allowed_observation_spans=event.get('evidence_context', {}).get('allowed_spans', {}))


def request_endpoint(client):
    """Return the exact nonsecret endpoint used in cache keys, or fail closed."""
    endpoint = client.settings.chat_completions_url
    parsed = urlsplit(endpoint)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.query or parsed.fragment
            or parsed.username is not None or parsed.password is not None):
        raise ValueError('UNSAFE_ENDPOINT_CANNOT_BE_SAVED_IN_PROVENANCE')
    return endpoint


def original_payload(cache, request_hash, model, endpoint):
    saved = read(Path(cache) / (request_hash + '.json'))
    if digest([endpoint, saved['request']]) != request_hash:
        raise ValueError('ORIGINAL_REQUEST_BODY_HASH_MISMATCH')
    if saved['receipt']['request_sha256'] != request_hash:
        raise ValueError('ORIGINAL_REQUEST_HASH_RECEIPT_MISMATCH')
    if digest(saved['result']) != saved['result_sha256']:
        raise ValueError('ORIGINAL_RESPONSE_HASH_MISMATCH')
    if saved['request']['model'] != model:
        raise ValueError('REPAIR_MUST_USE_SAME_MODEL')
    user = json.loads(saved['request']['messages'][1]['content'])
    if user['task'] != 'document_checkpoint_review':
        raise ValueError('ORIGINAL_REQUEST_NOT_CHECKPOINT_REVIEW')
    return deepcopy(user['input'])


def prepare_payload(payload, targets, names):
    result = deepcopy(payload)
    ids = {e['event_id'] for e in targets}
    result['events'] = [e for e in result['events'] if e['event']['event_id'] in ids]
    if {e['event']['event_id'] for e in result['events']} != ids:
        raise ValueError('TARGET_NOT_IN_ORIGINAL_REQUEST')
    pool = {b['block_id']: b for b in result['evidence_source_pool']}
    byid = {e['event_id']: e for e in targets}
    for item in result['events']:
        event = byid[item['event']['event_id']]
        provided = set(item['evidence_context']['block_ids'])
        actual = {f"{b['source_id']}:{b['start']}:{b['end']}": b for b in event['actual_model_evidence']}
        if provided != set(actual) or not provided <= set(pool):
            raise ValueError('ORIGINAL_EVIDENCE_CONTEXT_MISMATCH')
        actual_ranges = {(b['source_id'], b['start'], b['end']) for b in actual.values()}
        allowed_ranges = {(sid, span['start'], span['end'])
            for sid, spans in event['evidence_context']['allowed_spans'].items() for span in spans}
        if actual_ranges != allowed_ranges:
            raise ValueError('ORIGINAL_ALLOWED_SPANS_MISMATCH')
        for key, block in actual.items():
            given = pool[key]
            if any(given.get(field) != block[field] for field in ('source_id', 'start', 'end')):
                raise ValueError('ORIGINAL_EVIDENCE_COORDINATES_MISMATCH')
            raw = block.get('quote', block.get('text'))
            if not isinstance(raw, str) or given.get('text') != mask_text(raw, names):
                raise ValueError('ORIGINAL_EVIDENCE_TEXT_MISMATCH')
    result['instruction'] += '\n' + REPAIR_INSTRUCTIONS
    result['validation_feedback'] = redact([feedback(e) for e in targets], names)
    result['original_proposals'] = redact([e['semantic_review'] for e in targets], names)
    result['repair_contract'] = dict(maximum_semantic_repair_rounds=1,
        original_evidence_pool_sha256=digest(payload['evidence_source_pool']),
        source_context_added=False, positive_result_required=False)
    assert result['evidence_source_pool'] == payload['evidence_source_pool']
    assert result['earlier_event_ledger'] == payload['earlier_event_ledger']
    return result


def revalidate(event, proposal, sources, all_events, names, receipt):
    e = deepcopy(event)
    ctx = e['evidence_context']
    observations = resolve_model_quotes(proposal['observation_quotes'], sources, names, ctx['allowed_spans'])
    connections = resolve_model_quotes(proposal['connection_quotes'], sources, names,
                                       _allowed_connection_ranges(e, ctx, sources))
    comparison, availability, contract_reasons = validate_semantic_contract(e, proposal, ctx, sources, all_events, names)
    e['semantic_contract_validation'] = contract_reasons
    status, reasons = select_status(e, proposal, observations, connections,
        set(e.get('provided_prior_event_ids', [])), set(e.get('provided_identity_event_ids', [])))
    raw, dimensions, citations = dimension_audit(e, proposal, sources, names, reasons)
    e.update(semantic_review_before_refinement=deepcopy(event['semantic_review']),
        checkpoint_status_before_refinement=event.get('checkpoint_status'),
        validation_reasons_before_refinement=deepcopy(event.get('validation_reasons', [])),
        scope=proposal['scope'], record_function=proposal['record_function'], semantic_review=deepcopy(proposal),
        checkpoint_status=status, validation_reasons=reasons,
        validated_observation_citations=observations, validated_connection_citations=connections,
        comparison_validation=comparison, availability_validation=availability,
        previous_event_id=proposal['previous_event_id'], same_process_previous_id=proposal['same_process_previous_id'],
        semantic_uncertainty_dimensions=raw, uncertainty_dimensions=dimensions, uncertainty_dimension_citations=citations,
        review_request_sha256=receipt['request_sha256'], blind_replay_ready=False, expert_reference=False,
        review_refinement=dict(attempted=True, rounds=1, status='MODEL_REVISION_VALIDATED',
            original_review_request_sha256=event['review_request_sha256'],
            repair_request_sha256=receipt['request_sha256'], triggers=repair_triggers(event),
            evidence_context_unchanged=True))
    e['citation_warnings'] = dict(action_invalid=len(e['action_citations'].get('invalid', [])),
        action_ambiguous=len(e['action_citations'].get('ambiguous', [])),
        observation_invalid=len(observations['invalid']), observation_ambiguous=len(observations['ambiguous']),
        connection_invalid=len(connections['invalid']), connection_ambiguous=len(connections['ambiguous']))
    if 'legacy_uncertainty_fields' in e:
        e['legacy_uncertainty_fields'].update(event_identity=proposal['record_function'],
            evidence_evolution=proposal['evidence_evolution'], connection=proposal['process_link'], technical_validation=reasons)
    assert e['actual_model_evidence'] == event['actual_model_evidence']
    return e


def repair_group(targets, all_events, sources, cache, config, client):
    """One semantic call per original review, with no positive fallback."""
    original_hash = targets[0]['review_request_sha256']
    names = patient_names(sources)
    audit = dict(original_review_request_sha256=original_hash, target_event_ids=[e['event_id'] for e in targets],
                 semantic_rounds=1, result=None, receipt=None, status='PREPARING')
    receipt_start = len(getattr(client, 'receipts', []))
    try:
        payload = prepare_payload(original_payload(cache, original_hash, config['semantic_model'], request_endpoint(client)), targets, names)
        audit['repair_payload_sha256'] = digest(payload)
        result, receipt = client.ask('document_checkpoint_review_repair', payload, REVIEW_SCHEMA)
        audit.update(result=deepcopy(result), receipt=deepcopy(receipt))
        Draft202012Validator(REVIEW_SCHEMA).validate(result)
    except Exception as exc:
        new_receipts = getattr(client, 'receipts', [])[receipt_start:]
        if new_receipts:
            audit['receipt'] = deepcopy(new_receipts[-1])
        audit.update(status='REPAIR_NOT_APPLIED', error=type(exc).__name__)
        return [dict(deepcopy(e), review_refinement=dict(attempted=True, rounds=1,
            status='REPAIR_NOT_APPLIED', original_review_request_sha256=original_hash,
            error=type(exc).__name__, triggers=repair_triggers(e))) for e in targets], audit
    ids = set(audit['target_event_ids'])
    counts = Counter(x['event_id'] for x in result['items'])
    proposals = {r['event_id']: r for r in result['items'] if r['event_id'] in ids and counts[r['event_id']] == 1}
    audit['unexpected_event_ids'] = sorted(set(counts) - ids)
    output = []
    for e in targets:
        r = proposals.get(e['event_id'])
        if r is None:
            output.append(dict(deepcopy(e), review_refinement=dict(attempted=True, rounds=1,
                status='TARGET_MISSING_OR_DUPLICATED', original_review_request_sha256=original_hash,
                repair_request_sha256=receipt['request_sha256'], triggers=repair_triggers(e))))
            continue
        try:
            output.append(revalidate(e, r, sources, all_events, names, receipt))
        except Exception as exc:
            output.append(dict(deepcopy(e), review_refinement=dict(attempted=True, rounds=1,
                status='REPAIR_REVALIDATION_FAILED', original_review_request_sha256=original_hash,
                repair_request_sha256=receipt['request_sha256'], error=type(exc).__name__, triggers=repair_triggers(e))))
    audit['status'] = 'COMPLETE'
    return output, audit


def refine_case(input_dir, output_dir, cache, config, client, schema):
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    if output_dir.exists() or output_dir.resolve().is_relative_to(input_dir.resolve()):
        raise ValueError('REFINEMENT_REQUIRES_NEW_OUTPUT_DIRECTORY')
    endpoint = request_endpoint(client)
    required = ('decision_events.json', 'source_fhir_r4_bundle.json', 'model_receipts.json',
                'discovery.json', 'unit_inventory.json', 'source_import_receipt.json')
    hashes = {name: hashlib.sha256((input_dir / name).read_bytes()).hexdigest() for name in required}
    original = read(input_dir / 'decision_events.json')
    discovery_events = read(input_dir / 'discovery.json')['events']
    sources, source_receipt = import_sources(read(input_dir / 'source_fhir_r4_bundle.json'))
    patient = sources[0]['patient_uid']
    output_dir.mkdir(parents=True)
    shutil.copyfile(input_dir / 'decision_events.json', output_dir / 'original_decision_events.json')
    preserved = {}
    for name in ('discovery.json', 'unit_inventory.json', 'source_import_receipt.json'):
        shutil.copyfile(input_dir / name, output_dir / name)
        preserved[name] = hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
        if preserved[name] != hashes[name]:
            raise ValueError('PRESERVED_PRIMARY_ARTIFACT_HASH_MISMATCH')
    groups = defaultdict(list)
    for e in original:
        if repair_triggers(e):
            groups[e['review_request_sha256']].append(e)
    replacements, audits = {}, []
    for request_hash, targets in sorted(groups.items()):
        revised, audit = repair_group(targets, discovery_events, sources, cache, config, client)
        replacements.update({e['event_id']: e for e in revised})
        audits.append(audit)
        write(output_dir / 'repair_responses' / (request_hash + '.json'), audit)
    events = [replacements.get(e['event_id'], deepcopy(e)) for e in original]
    # Identity grouping is derived afresh; preserve its initial status in the
    # original snapshot and restore the model-validated pre-group status here.
    for e in events:
        if e.get('checkpoint_status') == 'GROUPED_SAME_EVENT_MENTION' and e.get('status_before_semantic_identity_grouping'):
            e['checkpoint_status'] = e['status_before_semantic_identity_grouping']
        for key in ('canonical_event_id', 'episode_id', 'status_before_semantic_identity_grouping'):
            e.pop(key, None)
    episodes, relations = assemble_processes(events)
    annotations = []
    for e in events:
        small = {k: v for k, v in e.items() if k not in ('evidence_context', 'actual_model_evidence')}
        small['evidence_context_audit_file'] = str(output_dir / 'decision_events.json')
        small['semantic'] = dict(supports=e['action_citations']['valid'])
        small['evolution'] = dict(supports=e.get('validated_observation_citations', {}).get('valid', []),
            connection_supports=e.get('validated_connection_citations', {}).get('valid', []))
        annotations.append(small)
    source_bundle = build(sources, patient, [], [])
    validate(source_bundle, schema)
    research_bundle = build(sources, patient, annotations, episodes)
    fhir_receipt = validate(research_bundle, schema)
    old_receipts = read(input_dir / 'model_receipts.json')
    current_receipts = [a['receipt'] for a in audits if a.get('receipt')]
    original_counts = dict(Counter(e['checkpoint_status'] for e in original))
    final_counts = dict(Counter(e['checkpoint_status'] for e in events))
    summary = dict(case_id=patient, version='p2-auto-checkpoint-0.5-one-pass-proof-refinement',
        original_input_directory=str(input_dir.resolve()), same_sources=True, source_import=source_receipt,
        original_event_count=len(original), final_event_count=len(events),
        repair_target_events=len(replacements), repair_request_groups=len(audits),
        repair_statuses=dict(Counter(e.get('review_refinement', {}).get('status', 'NOT_TARGETED') for e in events)),
        original_checkpoint_statuses=original_counts, checkpoint_statuses=final_counts,
        candidate_checkpoints=sum(e['checkpoint_status'].startswith('CANDIDATE_') for e in events),
        candidate_episodes=len(episodes), process_links=len(relations), fhir_validation=fhir_receipt,
        clinical_validation=False, annotation_references_read=False, new_source_context_added=False,
        maximum_semantic_repair_rounds=1, blind_packets_created=False)
    for name, value in [('decision_events', events), ('episodes', episodes), ('relations', relations),
                        ('source_fhir_r4_bundle', source_bundle), ('research_fhir_r4_bundle', research_bundle),
                        ('model_receipts', old_receipts + current_receipts), ('summary', summary)]:
        write(output_dir / (name + '.json'), value)
    if any(hashlib.sha256((input_dir / name).read_bytes()).hexdigest() != value for name, value in hashes.items()):
        raise ValueError('COMPLETED_INPUT_ARTIFACT_CHANGED_DURING_REFINEMENT')
    manifest = dict(schema_version='v05-refinement-provenance-1',
        chat_completions_endpoint=endpoint, model=config['semantic_model'],
        original_input_directory=str(input_dir.resolve()),
        original_decision_events_sha256=hashes['decision_events.json'],
        original_source_fhir_sha256=hashes['source_fhir_r4_bundle.json'],
        source_fhir_sha256=hashlib.sha256((output_dir / 'source_fhir_r4_bundle.json').read_bytes()).hexdigest(),
        original_discovery_sha256=hashes['discovery.json'], original_file_sha256=hashes,
        preserved_file_sha256=preserved, revalidation_uses_original_discovery_events=True,
        source_context_added=False, maximum_semantic_repair_rounds=1)
    write(output_dir / 'refinement_manifest.json', manifest)
    return summary
