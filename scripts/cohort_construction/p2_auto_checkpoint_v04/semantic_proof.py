"""Source-bound two-channel prechoice proof; no clinical truth or clock inference.

The review LLM proposes semantic roles and citations. This module checks only the
mechanical proof contract: exact supplied text, separate action/fact/connection
roles, declared EvidenceObject identity, and per-current-fact coverage. It never
invents a release/reading timestamp or turns text similarity into adoption.
"""
from copy import deepcopy


POSITIVE_CHANNELS = {'report_time', 'semantic_prechoice'}


def _span(citation):
    start, end = citation.get('start'), citation.get('end')
    if type(start) is int and type(end) is int:
        return start, end
    return None


def _overlap(left, right):
    a, b = _span(left), _span(right)
    return bool(a and b and max(a[0], b[0]) < min(a[1], b[1]))


def _contained(inner, outer):
    a, b = _span(inner), _span(outer)
    return bool(a and b and inner['source_id'] == outer['source_id']
                and b[0] <= a[0] and a[1] <= b[1])


def _occurs_in(citation, container):
    """An exact narrative/report fact must occur in its proposed connection."""
    if citation.get('source_id') != container.get('source_id'):
        return False
    a, b = _span(citation), _span(container)
    if a and b and b[0] <= a[0] and a[1] <= b[1]:
        return True
    spans = list(citation.get('occurrences', [])) + list(citation.get('raw_occurrences', []))
    return any(_span(item) and b and b[0] <= _span(item)[0] and _span(item)[1] <= b[1]
               for item in spans)


def _citation_key(citation):
    submitted = citation.get('citation') or citation
    return submitted.get('source_id'), submitted.get('quote')


def _error(code, event, *, path, source_id=None, evidence_object_id=None,
           citation_index=None, citation=None, repairable=True, correction):
    span = _span(citation or {})
    return dict(code=code, event_id=event['event_id'],
        evidence_object_id=evidence_object_id, field_path=path, source_id=source_id,
        citation_index=citation_index, span=dict(start=span[0], end=span[1]) if span else None,
        rule='V04-TIME-008', repairable=repairable, correction=correction)


def _validated(entries, key):
    return next((item for item in entries if _citation_key(item) == key), None)


def _valid_action(event, citation):
    if not citation:
        return None
    if 'valid' in citation:
        citation = _single_valid(citation)
        if citation is None:
            return None
    target = _citation_key(citation)
    groups = event.get('choice_boundary_citations') or {}
    return next((item for item in groups.get('valid', []) if _citation_key(item) == target), None)


def _single_valid(proof):
    """An exact channel needs one unambiguous citation, not a repeated token."""
    if not proof or len(proof.get('valid', [])) != 1 \
            or proof.get('invalid') or proof.get('ambiguous'):
        return None
    return proof['valid'][0]


def compile_proof_channels(event, proposal, validation, *, path, evidence_object_id=None,
                           legacy=False):
    """Return separate temporal and semantic channels plus field-local errors.

    ``validation`` must contain exact source resolutions for action, connection,
    and (where applicable) narrative/basis citations. A report-time proposal is
    not accepted merely because an exam-time proxy exists. Semantic prechoice
    support requires an explicit before claim, a separate current action, and a
    connection that contains both the observation and the action.
    """
    errors = []
    action_key = _citation_key(proposal.get('action_quote') or proposal.get('current_action_quote') or {})
    action = _valid_action(event, validation.get('action'))
    connection = _single_valid(validation.get('connection'))
    narrative = _single_valid(validation.get('narrative'))
    basis = _single_valid(validation.get('basis'))
    role = proposal.get('proof_event_role') or event.get('proof_event_role')
    order = proposal.get('semantic_order', 'unknown')
    exact_action_bound = _valid_action(event, validation.get('action')) is not None or legacy
    semantic_claim = proposal.get('semantic_channel_status')
    temporal_claim = proposal.get('temporal_channel_status')
    requested = set(proposal.get('proof_channels') or [])
    if semantic_claim == 'explicit_before_choice':
        requested.add('semantic_prechoice')
    if temporal_claim == 'temporally_possibly_visible':
        requested.add('report_time')
    unknown_channels = requested - POSITIVE_CHANNELS
    if unknown_channels:
        errors.append(_error('SEMANTIC_PROOF_UNKNOWN_CHANNEL', event, path=path + '.proof_channels',
            evidence_object_id=evidence_object_id, correction='Use only the two declared proof channels.'))
    if order in ('after', 'same_time_unresolved') or semantic_claim == 'after_choice' \
            or proposal.get('availability') == 'after_choice' or role == 'postchoice_record':
        requested.discard('semantic_prechoice')
        if order == 'before' or semantic_claim == 'explicit_before_choice' \
                or proposal.get('availability') == 'explicitly_known_before':
            errors.append(_error('SEMANTIC_PROOF_POSTCHOICE_CONTRADICTION', event,
                path=path + '.semantic_order', evidence_object_id=evidence_object_id,
                repairable=False, correction='Preserve the postchoice record; do not reverse its order.'))
    if requested - POSITIVE_CHANNELS:
        requested = requested & POSITIVE_CHANNELS
    if requested:
        # A report-time channel has different source obligations from a
        # narrative semantic channel. Do not demand an action/connection quote
        # merely because a verified release clock already supports visibility.
        action_required = 'semantic_prechoice' in requested or not legacy
        if action_required and action is None:
            errors.append(_error('SEMANTIC_PROOF_ACTION_NOT_EXACT_CHOICE_BOUNDARY', event,
                path=path + ('.action_quote' if 'action_quote' in proposal else '.current_action_quote'),
                source_id=action_key[0], evidence_object_id=evidence_object_id,
                correction='Copy one exact current choice_boundary_quote; do not use it as a patient fact.'))
        if action_required and connection is None and 'semantic_prechoice' in requested:
            errors.append(_error('SEMANTIC_PROOF_CONNECTION_NOT_UNIQUELY_VERIFIED', event,
                path=path + '.connection_quote', source_id=(proposal.get('connection_quote') or {}).get('source_id'),
                evidence_object_id=evidence_object_id,
                correction='Provide one unambiguous source quotation connecting this evidence to the action.'))
        if 'semantic_prechoice' in requested and (
                order != 'before' or semantic_claim not in (None, 'explicit_before_choice')
                or proposal.get('availability') not in (None, 'explicitly_known_before')):
            errors.append(_error('SEMANTIC_PROOF_POSITIVE_CHANNEL_ORDER_MISMATCH', event,
                path=path + '.proof_channels', evidence_object_id=evidence_object_id,
                correction='Do not claim prechoice support when order or availability is not explicitly before.'))
        if narrative is None and 'semantic_prechoice' in requested:
            errors.append(_error('SEMANTIC_PROOF_FACT_NOT_UNIQUELY_VERIFIED', event,
                path=path + ('.narrative_quote' if 'narrative_quote' in proposal else '.observation'),
                evidence_object_id=evidence_object_id,
                correction='Use one exact, unambiguous patient-fact or narrative quotation.'))
        if basis is None and 'basis_quotes' in proposal and 'semantic_prechoice' in requested:
            errors.append(_error('SEMANTIC_PROOF_BASIS_NOT_UNIQUELY_VERIFIED', event,
                path=path + '.basis_quotes', evidence_object_id=evidence_object_id,
                correction='Provide one exact prechoice basis quotation or retain unresolved proof.'))
        if action is not None and narrative is not None and _overlap(action, narrative):
            errors.append(_error('SEMANTIC_PROOF_CURRENT_ACTION_CONTAMINATES_FACT', event,
                path=path + ('.narrative_quote' if 'narrative_quote' in proposal else '.observation'),
                source_id=narrative.get('source_id'), evidence_object_id=evidence_object_id,
                citation_index=narrative.get('citation_index'), citation=narrative,
                correction='Split the source-bound fact clause from the current action; retain both original quotes.'))
        if action is not None and connection is not None and not _occurs_in(action, connection):
            errors.append(_error('SEMANTIC_PROOF_CONNECTION_OMITS_CURRENT_ACTION', event,
                path=path + '.connection_quote', source_id=connection.get('source_id'),
                evidence_object_id=evidence_object_id, citation=connection,
                correction='The connection must contain the exact current action clause.'))
        if narrative is not None and connection is not None and not _occurs_in(narrative, connection):
            errors.append(_error('SEMANTIC_PROOF_CONNECTION_OMITS_DECLARED_FACT', event,
                path=path + '.connection_quote', source_id=connection.get('source_id'),
                evidence_object_id=evidence_object_id, citation=connection,
                correction='The connection must contain the declared fact, not merely similar words elsewhere.'))
        if basis is not None and narrative is not None and not _occurs_in(narrative, basis):
            errors.append(_error('SEMANTIC_PROOF_BASIS_OMITS_DECLARED_FACT', event,
                path=path + '.basis_quotes', source_id=basis.get('source_id'),
                evidence_object_id=evidence_object_id, citation=basis,
                correction='The prechoice basis must contain the declared patient fact.'))
        if connection is not None and basis is not None and not _occurs_in(connection, basis):
            errors.append(_error('SEMANTIC_PROOF_BASIS_OMITS_CONNECTION', event,
                path=path + '.basis_quotes', source_id=basis.get('source_id'),
                evidence_object_id=evidence_object_id, citation=basis,
                correction='The basis must preserve the source wording linking evidence to action.'))
    temporal_conflict_reasons = set(validation.get('temporal_reasons', [])) & {
        'NARRATIVE_RECORD_TIME_BEFORE_EVIDENCE_PUBLICATION',
        'EVIDENCE_NOT_PUBLISHED_BEFORE_CHOICE',
        'EVIDENCE_PUBLICATION_NOT_BEFORE_EXACT_CHOICE',
    }
    temporal_conflict = bool(temporal_conflict_reasons) \
        or temporal_claim == 'conflict' or proposal.get('publication_before_choice') == 'no'
    # Narrative record time is a document-time proxy, not the exact choice
    # instant. It may disprove that a paraphrase follows publication, but cannot
    # by itself disprove that the publication preceded the later clinical choice.
    temporal_supported = False
    if 'report_time' in requested:
        # Only the existing independently supplied publication clock can support
        # this branch. Semantic wording or an exam proxy cannot manufacture it.
        temporal_supported = bool(validation.get('publication_before_choice_verified')) \
            and exact_action_bound and not temporal_conflict
        if not temporal_supported:
            errors.append(_error('SEMANTIC_PROOF_REPORT_TIME_NOT_INDEPENDENTLY_VERIFIED', event,
                path=path + '.temporal_channel_status', evidence_object_id=evidence_object_id,
                repairable=False, correction='Keep report-time proof unresolved; never infer it from semantic text or exam time.'))
    if temporal_conflict:
        errors.append(_error('SEMANTIC_PROOF_TEMPORAL_CHANNEL_CONFLICT', event,
            path=path + '.temporal_channel_status', evidence_object_id=evidence_object_id,
            repairable=False, correction='Preserve the temporal conflict; semantic wording cannot erase it.'))
    technical_codes = {item['code'] for item in errors}
    # Supported means the complete source-bound proof, not merely a declared
    # channel plus one valid action. In particular, a valid action must not hide
    # an absent fact/connection and then fail again during current-fact coverage.
    semantic_required_codes = {
        'SEMANTIC_PROOF_ACTION_NOT_EXACT_CHOICE_BOUNDARY',
        'SEMANTIC_PROOF_CONNECTION_NOT_UNIQUELY_VERIFIED',
        'SEMANTIC_PROOF_FACT_NOT_UNIQUELY_VERIFIED',
        'SEMANTIC_PROOF_BASIS_NOT_UNIQUELY_VERIFIED',
        'SEMANTIC_PROOF_POSITIVE_CHANNEL_ORDER_MISMATCH',
        'SEMANTIC_PROOF_CURRENT_ACTION_CONTAMINATES_FACT',
        'SEMANTIC_PROOF_CONNECTION_OMITS_CURRENT_ACTION',
        'SEMANTIC_PROOF_CONNECTION_OMITS_DECLARED_FACT',
        'SEMANTIC_PROOF_BASIS_OMITS_DECLARED_FACT',
        'SEMANTIC_PROOF_BASIS_OMITS_CONNECTION',
        'SEMANTIC_PROOF_POSTCHOICE_CONTRADICTION',
    }
    if 'SEMANTIC_PROOF_ACTION_NOT_EXACT_CHOICE_BOUNDARY' in technical_codes:
        errors.append(_error('SEMANTIC_PROOF_PRECHOICE_REQUIRES_EXACT_ACTION_BOUNDARY', event,
            path=path + '.proof_channels', evidence_object_id=evidence_object_id,
            correction='A channel supports this specific choice only when its exact discovered action boundary is source-verified.'))
    semantic_supported = 'semantic_prechoice' in requested \
        and not (technical_codes & semantic_required_codes) \
        and exact_action_bound and not temporal_conflict and order == 'before'
    if legacy:
        # Migrated historical fixtures did not expose a separate action field.
        # They retain the original availability/basis contract, not a new EO or
        # report-time channel. New model outputs always use the strict branch.
        semantic_supported = bool(requested == {'semantic_prechoice'}
            and order == 'before' and basis is not None and narrative is not None
            and _occurs_in(narrative, basis) and not temporal_conflict)
    channels = dict(report_time=dict(
            status='TEMPORALLY_POSSIBLY_VISIBLE' if temporal_supported else
                'CONFLICT' if temporal_conflict else 'UNRESOLVED',
            supported=temporal_supported,
            established_reading=False, established_causal_adoption=False),
        semantic_prechoice=dict(
            status='SOURCE_BOUND_EXPLICIT_BEFORE_CHOICE' if semantic_supported else
                'CONFLICT' if temporal_conflict else
                'AFTER_CHOICE' if order == 'after' or semantic_claim == 'after_choice' else 'UNRESOLVED',
            supported=semantic_supported, narrative_prechoice_only=True,
            clinician_reading_established=False, clinical_causality_certified=False))
    return dict(format='semantic-proof-channels-0.1.0', proof_event_role=role,
        channels=channels, supported_channels=sorted(k for k,v in channels.items() if v['supported']),
        temporal_conflict=temporal_conflict, field_errors=errors,
        action_citation=deepcopy(action), connection_citation=deepcopy(connection),
        narrative_citation=deepcopy(narrative), basis_citation=deepcopy(basis),
        generated_clock_fields=[], clinical_reference=False)


def compile_availability(event, item, observation, basis, action, connection,
                         action_validated=None, legacy=False):
    action = action_validated if action_validated is not None else _valid_action(event, action)
    narrative = _validated(observation.get('valid', []) + observation.get('ambiguous', []) + observation.get('invalid', []),
                           _citation_key(item.get('observation', {})))
    proof = compile_proof_channels(event, item, dict(
        narrative=observation, basis=basis, action=action, connection=connection),
        path='observation_availability', legacy=legacy)
    proof['narrative_citation'] = deepcopy(narrative)
    return proof


def _covered(target, supports):
    end = target.get('start')
    if type(end) is not int:
        return False
    for low, high in sorted((item['start'], item['end']) for item in supports
                            if item.get('source_id') == target.get('source_id') and _span(item)):
        if low > end:
            break
        end = max(end, high)
        if end >= target['end']:
            return True
    return False


def relation_supports_fact(relation, target):
    """Bind one EO relation to the same submitted current-fact span.

    Matching EO/novelty alone is insufficient. The exact narrative quote must
    or when the current fact is itself the same report text. Equal wording alone
    never certifies a cross-document paraphrase or clinical adoption.
    """
    if not relation.get('prechoice_proof_supported'):
        return False
    narrative = relation.get('validated_narrative_quote') or {}
    if _span(narrative) and _covered(target, [narrative]):
        return True
    direct = relation.get('validated_direct_quote') or {}
    # Raw validated citations keep their original offsets; wrapper proposals
    # use ``citation`` only to recover the exact submitted object identity.
    submitted = target if target.get('quote') else target.get('citation') or {}
    return bool(_span(direct) and direct.get('source_id') == target.get('source_id')
                and direct.get('quote') == submitted.get('quote'))


def combine_current_fact_coverage(event, current_information, availability,
                                  relation_ledger=None, legacy=False):
    """Return per-fact proof coverage; one good EO cannot certify every fact."""
    known = []
    for item in availability:
        if not item.get('eligible_as_known_prechoice'):
            continue
        known.extend((item.get('validated_observation') or {}).get('valid', []))
    relations = (relation_ledger or {}).get('relations', [])
    rows, errors = [], []
    for index, target in enumerate(current_information.get('valid', [])):
        narrative = _covered(target, known)
        supporting = [row.get('relation_id') for row in relations
                      if relation_supports_fact(row, target)]
        supported = narrative or bool(supporting)
        rows.append(dict(citation_index=target.get('citation_index', index),
            source_id=target.get('source_id'), start=target.get('start'), end=target.get('end'),
            observation_channel_supported=narrative, evidence_object_relation_ids=supporting,
            prechoice_supported=supported))
        if not supported:
            errors.append(_error('SEMANTIC_PROOF_CURRENT_FACT_NOT_COVERED', event,
                path='evidence_comparison.current_information_quotes',
                source_id=target.get('source_id'), citation_index=target.get('citation_index'),
                citation=target, correction='Provide separate source-bound prechoice proof for this exact current fact.'))
    for status in ('invalid', 'ambiguous'):
        for target in current_information.get(status, []):
            errors.append(_error('SEMANTIC_PROOF_CURRENT_FACT_NOT_UNIQUELY_VERIFIED', event,
                path='evidence_comparison.current_information_quotes', source_id=target.get('source_id'),
                citation_index=target.get('citation_index'), citation=target,
                correction='Resolve this exact source quotation; do not discard it from the proposed comparison.'))
    return dict(per_current_fact=rows,
        known_current_information_supported=bool(rows) and all(row['prechoice_supported'] for row in rows)
            and not current_information.get('invalid') and not current_information.get('ambiguous'),
        field_errors=errors)


def proof_role_matches(review):
    role = review.get('proof_event_role')
    if role is None:
        return True  # Archived/legacy proposals have no such declared field.
    checkpoint, kind = review.get('checkpoint'), review.get('checkpoint_kind')
    if checkpoint == 'new_evidence' and kind == 'evidence_informed_choice':
        return role == 'evidence_informed_management_choice'
    if checkpoint == 'new_evidence' and kind == 'new_management_choice':
        return role in ('new_management_choice', 'first_visible_management_choice')
    if checkpoint == 'first_anchor':
        return role in ('first_visible_management_choice', 'test_execution_anchor', 'new_management_choice')
    if checkpoint == 'repeat':
        return role in ('repeat_or_execution', 'postchoice_record')
    if checkpoint == 'uncertain':
        return True
    return role == 'uncertain'
