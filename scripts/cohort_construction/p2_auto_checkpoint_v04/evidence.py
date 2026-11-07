"""Evidence-bearing report objects; separate preservation from proof strength.

An execution anchor preserves the fact that a test occurred. An EvidenceObject
keeps its source text and time provenance for later management choices. Neither
object certifies a clinical choice time, report reading, or causal use.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json

from scripts.cohort_construction.p2_pathway_replay.core import (
    release_time_provenance, _structured_clock, _compare_clocks,
)
from .semantic_proof import compile_proof_channels


REPORT_KINDS = {'imaging', 'lab', 'pathology'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def _clock(value):
    # Execution and publication objects must retain the same precision as the
    # retrieval resolver; a normalized :00 is not automatically second precision.
    return _structured_clock(value)


def _publication_clock(clocks):
    # report_date is a distinct legacy alias; a missing release clock is not
    # replaced by a document/exam clock or by the later management choice.
    return _clock(next((clocks.get(role) for role in ('report_time', 'report_date', 'issued')
                       if _clock(clocks.get(role)).get('value')), None))


def _provided(event, context):
    """Bind exact supplied ranges and EO IDs from actual model context."""
    blocks = context.get('blocks', []) if context is not None else event.get('actual_model_evidence', [])
    return {(b.get('source_id'), b.get('start'), b.get('end'), b.get('evidence_object_id'))
            for b in blocks}


def _citation_block_bindings(citations, supplied_blocks):
    """Resolve each narrative citation to its actual supplied block and clock.

    A cited substring may be contained in a full supplied document block. An
    ambiguous quote must have a unique containing block; an unlocated quote
    never borrows a same-source sibling clock. The original quote, source hash
    and block text are retained in the binding for deterministic audit.
    """
    bindings = []
    for quote in citations:
        source_id = quote.get('source_id')
        submitted = quote.get('citation') or {}
        text = submitted.get('quote') if isinstance(submitted, dict) else None
        if not isinstance(text, str) or not isinstance(quote.get('quote'), str) \
                or text != quote['quote'] or not text:
            bindings.append(None)
            continue
        candidates = []
        for block in supplied_blocks:
            if block.get('source_id') != source_id:
                continue
            block_text = block.get('quote', block.get('text'))
            start, end = quote.get('start'), quote.get('end')
            if all(type(value) is int for value in (start, end, block.get('start'), block.get('end'))) \
                    and block['start'] <= start < end <= block['end'] \
                    and isinstance(block_text, str) and len(block_text) == block['end'] - block['start'] \
                    and block_text[start-block['start']:end-block['start']] == text:
                candidates.append(block)
            else:
                spans = list(quote.get('occurrences', [])) + list(quote.get('raw_occurrences', []))
                for occurrence in spans:
                    start, end = occurrence.get('start'), occurrence.get('end')
                    if all(type(value) is int for value in (start, end, block.get('start'), block.get('end'))) \
                            and block['start'] <= start < end <= block['end'] \
                            and isinstance(block_text, str) and len(block_text) == block['end'] - block['start'] \
                            and block_text[start-block['start']:end-block['start']] == text:
                        candidates.append(block)
            # A supplied block can use a nonzero source offset. Locate the exact
            # submitted text inside that block too; otherwise a second copy of
            # the same document would be invisible to the ambiguity check.
            if isinstance(block_text, str) and type(block.get('start')) is int \
                    and type(block.get('end')) is int \
                    and len(block_text) == block['end'] - block['start']:
                cursor = block_text.find(text)
                while cursor >= 0:
                    start, end = block['start'] + cursor, block['start'] + cursor + len(text)
                    if block['start'] <= start < end <= block['end']:
                        candidates.append(block)
                    cursor = block_text.find(text, cursor + 1)
        unique = {(block.get('source_id'), block.get('start'), block.get('end')): block
                  for block in candidates}
        bindings.append(next(iter(unique.values())) if len(unique) == 1 else None)
    return bindings


def _legacy_narrative_clock(event, validated, quote, bindings):
    """Legacy no-pool fallback: only the event's own narrative source is usable."""
    if any(binding is not None for binding in bindings):
        return None
    supplied_ids = {block.get('source_id') for block in event.get('actual_model_evidence', [])}
    citation = validated.get('narrative_quote_validation') or {}
    submitted = citation.get('valid', []) + citation.get('ambiguous', [])
    if len(supplied_ids) != 1 or quote.get('source_id') != event.get('source_id') \
            or quote.get('source_id') not in supplied_ids \
            or any(entry.get('source_id') != event.get('source_id') for entry in submitted):
        return None
    return deepcopy(event.get('source_document_clock'))


def compile_evidence_relations(event, validated_relations, sources, context=None):
    """Bind model-proposed evidence use to exact, actually supplied source text.

    Errors are local to their evidence object; a valid sibling cannot erase an
    unknown object proposed in the same relation. Direct/paraphrased source
    support does not certify publication, reading, adoption, novelty or causal
    use. Model assertions remain visibly proposals rather than established facts.
    """
    objects = evidence_object_index(sources)
    supplied = _provided(event, context)
    supplied_blocks = context.get('blocks', []) if context is not None else event.get('actual_model_evidence', [])
    by_object = {obj['evidence_object_id']: obj for obj in objects.values()}
    rows, reasons = [], []
    temporal_only_reasons = {'NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED'}
    temporal_conflict_reasons = {'NARRATIVE_RECORD_TIME_BEFORE_EVIDENCE_PUBLICATION',
                                 'EVIDENCE_NOT_PUBLISHED_BEFORE_CHOICE',
                                 'EVIDENCE_PUBLICATION_NOT_BEFORE_EXACT_CHOICE'}
    proof_reason_prefixes = ('SEMANTIC_PROOF_', 'EVIDENCE_OBJECT_CHOICE_TIME_')

    def finalize_row(row, source_bound, proof, modern):
        """Keep clocks, source binding, and usable prechoice proof independent."""
        row_reasons = set(row['validation_reasons'])
        row_reasons.update(item['code'] for item in (proof or {}).get('field_errors', []))
        conflicts = row_reasons & temporal_conflict_reasons or bool((proof or {}).get('temporal_conflict'))
        source_blocking = row_reasons - temporal_only_reasons - temporal_conflict_reasons \
            - {code for code in row_reasons if code.startswith(proof_reason_prefixes)}
        # A declared semantic label never erases a clock conflict. Conversely,
        # publication unknown does not reject an otherwise exact semantic quote.
        source_status = 'SOURCE_BOUND' if source_bound and not source_blocking else 'UNRESOLVED'
        relation_bound = source_status == 'SOURCE_BOUND' and not conflicts
        if modern:
            relation_bound = relation_bound and bool((proof or {}).get('supported_channels'))
        row.update(
            relation_status='SOURCE_BOUND_PROPOSAL' if relation_bound else 'UNRESOLVED',
            source_binding_status=source_status,
            validation_reasons=sorted(row_reasons),
            semantic_proof_validation=deepcopy(proof),
            supported_proof_channels=deepcopy((proof or {}).get('supported_channels', [])),
            prechoice_proof_supported=bool((proof or {}).get('supported_channels')) and not conflicts,
            temporal_conflict_present=bool(conflicts),
            temporal_only_reasons=sorted(row_reasons & temporal_only_reasons),
            temporal_conflict_reasons=sorted(row_reasons & temporal_conflict_reasons),
            source_blocking_reasons=sorted(source_blocking),
            publication_before_choice_established=bool(
                (proof or {}).get('channels', {}).get('report_time', {}).get('supported')),
        )
        return row

    def append_row(relation, oid, evidence, index, row_reasons, source_bound=False):
        modern = 'proof_event_role' in relation
        citation = next((item for item in citations if item.get('evidence_object_id') == oid), {})
        supplied_block = next((item for item in supplied_blocks if item.get('evidence_object_id') == oid), {})
        choice_clock = _clock(event.get('choice_time') or event.get('decision_clock'))
        publication = _clock(supplied_block.get('publication_time') or (evidence or {}).get('publication_time'))
        publication_order = _compare_clocks(publication, choice_clock)
        exact_choice = event.get('time_basis') == 'explicit_documented_choice' and bool(choice_clock['value'])
        row = {k: deepcopy(v) for k, v in relation.items()
               if k not in ('validated_object_citations', 'semantic_proof_proposals')}
        row.update(relation_id=('ER' + _digest([
                event['event_id'], oid, index, relation.get('evidence_use'),
                relation.get('evidence_object_ids', [])])[:20] if oid else None),
            choice_event_id=event['event_id'], evidence_object_id=oid,
            validation_reasons=sorted(set(row_reasons)),
            publication_before_choice=relation.get('publication_before_choice', 'unknown'),
            reading_before_choice=relation.get('reading_before_choice', 'unknown'),
            novelty=relation.get('novelty', 'uncertain'),
            causal_role=relation.get('causal_role', 'not_established'),
            clinician_reading_established=False, clinical_causality_certified=False,
            reading_before_choice_established=False,
            novelty_established=False, causal_role_established=False,
            semantic_proposal_not_clinical_truth=True,
            validated_narrative_quote=deepcopy(citation.get('validated_narrative_quote')),
            validated_direct_quote=deepcopy(citation.get('validated_direct_quote')),
            publication_before_choice_order=publication_order,
            publication_before_choice_verified=publication_order == 'BEFORE' and exact_choice)
        if 'report_time' in (relation.get('proof_channels') or []) \
                and not row['publication_before_choice_verified'] and publication.get('value') \
                and choice_clock['value'] and publication_order != 'BEFORE':
            row_reasons.append('EVIDENCE_PUBLICATION_NOT_BEFORE_EXACT_CHOICE')
        if use == 'paraphrased' and evidence is not None and citation:
            narrative_quotes = [quote for status in ('valid', 'invalid', 'ambiguous')
                for quote in ((citation.get('narrative_quote_validation') or {}).get(status, []))]
            bindings = _citation_block_bindings(narrative_quotes, supplied_blocks)
            # Source-character binding and narrative/report chronology are
            # separate. Same-minute/day overlap is not silently ordered, and a
            # record-time proxy never proves clinician reading or actual use.
            if not narrative_quotes:
                order = 'UNRESOLVED_NARRATIVE_QUOTE'
                narrative_clock = _clock(None)
                binding_receipt = dict(status='NO_SUBMITTED_NARRATIVE_CITATION')
                row_reasons.append('NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED')
            elif len(narrative_quotes) != 1:
                order = 'UNRESOLVED_MULTIPLE_NARRATIVE_CITATIONS'
                narrative_clock = _clock(None)
                binding_receipt = dict(status='MULTIPLE_NARRATIVE_CITATIONS_NOT_POOL_SHARED')
                row_reasons.append('NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED')
            else:
                quote, binding = narrative_quotes[0], bindings[0]
                narrative_clock = _clock(binding.get('clock')) if binding is not None else _clock(None)
                if binding is None and context is None:
                    narrative_clock = _clock(_legacy_narrative_clock(event, citation, quote, bindings))
                if narrative_clock.get('value'):
                    order = _compare_clocks(narrative_clock, publication)
                    binding_receipt = dict(status='EXACT_OR_CONTAINED_SUPPLIED_BLOCK' if binding is not None
                        else 'LEGACY_SINGLE_EVENT_NARRATIVE_CLOCK',
                        source_id=quote.get('source_id'), quote=quote.get('quote'),
                        block_start=binding.get('start') if binding else None,
                        block_end=binding.get('end') if binding else None,
                        citation_start=quote.get('start'), citation_end=quote.get('end'))
                else:
                    order = 'UNRESOLVED_NARRATIVE_SOURCE_OR_CLOCK'
                    binding_receipt = dict(status='NARRATIVE_BLOCK_NOT_UNIQUE_OR_CLOCK_UNRESOLVED',
                        source_id=quote.get('source_id'), quote=quote.get('quote'))
                    row_reasons.append('NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED')
            compatible = order in ('AFTER', 'SAME_TIME_UNORDERED',
                                   'SAME_MINUTE_UNORDERED', 'SAME_DAY_UNORDERED')
            if order == 'BEFORE':
                row_reasons.append('NARRATIVE_RECORD_TIME_BEFORE_EVIDENCE_PUBLICATION')
            elif not compatible:
                # The semantic channel can be independently source-bound, but
                # publication/narrative order remains explicitly unresolved.
                row_reasons.append('NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED')
            row.update(narrative_record_clock=narrative_clock,
                evidence_publication_clock=publication,
                narrative_clock_binding=binding_receipt,
                narrative_record_to_publication_order=order,
                narrative_publication_time_compatible=compatible,
                narrative_record_time_order_not_reading_proof=True)
        # Publication/narrative ordering is resolved after the row skeleton is
        # created. Refresh the persisted reason snapshot before compiling and
        # finalizing so concrete clock reasons cannot disappear behind the
        # derived SEMANTIC_PROOF_* error codes.
        row['validation_reasons'] = sorted(set(row_reasons))
        proof = None
        if modern:
            if relation.get('temporal_channel_status') == 'temporally_possibly_visible' \
                    and not exact_choice and 'report_time' in (relation.get('proof_channels') or []):
                row_reasons.append('EVIDENCE_OBJECT_CHOICE_TIME_NOT_EXACT')
            proof = compile_proof_channels(event, relation, dict(
                narrative=citation.get('narrative_quote_validation'),
                action=relation.get('validated_action_citation'),
                connection=relation.get('validated_connection_citation'),
                publication_before_choice_verified=row['publication_before_choice_verified'],
                narrative_record_to_publication_order=row.get(
                    'narrative_record_to_publication_order', publication_order),
                temporal_reasons=row_reasons),
                path='evidence_relations', evidence_object_id=oid)
        finalize_row(row, source_bound, proof, modern)
        if evidence is not None:
            row.update(source_id=evidence['source_id'], source_sha256=evidence['source_sha256'])
        rows.append(row)
        reasons.extend(row['validation_reasons'])

    for index, raw in enumerate(validated_relations or []):
        relation = deepcopy(raw)
        # Source times are looked up in the exact submitted pool, not in a
        # possibly divergent mutable source object used only to rebuild EOs.
        use = relation.get('evidence_use')
        proposed_ids = relation.get('evidence_object_ids', [])
        unique_proposed_ids = list(dict.fromkeys(proposed_ids))
        citations = relation.get('validated_object_citations', [])
        counts = Counter(unique_proposed_ids)
        shared_reasons = []
        if not unique_proposed_ids:
            shared_reasons.append('EVIDENCE_OBJECT_ID_REQUIRED')
        if len(unique_proposed_ids) != len(proposed_ids):
            shared_reasons.append('DUPLICATE_EVIDENCE_OBJECT_ID_IN_RELATION')
        if use == 'none' and (relation.get('direct_quote') or relation.get('narrative_quote')):
            shared_reasons.append('NONE_RELATION_CANNOT_HAVE_EVIDENCE_QUOTE')
        if use not in ('direct', 'paraphrased'):
            shared_reasons.append('EVIDENCE_USE_NOT_SOURCE_BOUND:' + str(use))
        if relation.get('publication_before_choice') == 'no' and use in ('direct', 'paraphrased'):
            shared_reasons.append('EVIDENCE_NOT_PUBLISHED_BEFORE_CHOICE')
        if relation.get('reading_before_choice') == 'yes' and relation.get('publication_before_choice') != 'yes':
            shared_reasons.append('READING_BEFORE_CHOICE_REQUIRES_PUBLICATION_BEFORE_CHOICE')
        unknown_proposed = set(unique_proposed_ids) - set(by_object)
        if unknown_proposed:
            shared_reasons.append('RELATION_CONTAINS_UNKNOWN_OR_UNPROVIDED_OBJECT')
        cited_ids = []
        seen_citations = set()
        for citation in citations:
            oid = citation.get('evidence_object_id')
            evidence = by_object.get(oid)
            row_reasons = list(shared_reasons)
            if oid not in unique_proposed_ids or evidence is None:
                row_reasons.append('UNKNOWN_OR_UNPROVIDED_EVIDENCE_OBJECT')
                append_row(relation, oid, None, index, row_reasons)
                continue
            cited_ids.append(oid)
            citation_identity = (oid, evidence['source_id'], citation.get('start'), citation.get('end'),
                                 citation.get('direct_quote_valid'), citation.get('paraphrase_quote_valid'))
            if citation_identity in seen_citations:
                continue
            seen_citations.add(citation_identity)
            span = (evidence['source_id'], citation.get('start'), citation.get('end'), oid)
            if span not in supplied:
                row_reasons.append('EVIDENCE_OBJECT_NOT_IN_SUBMITTED_CONTEXT')
            if use == 'direct' and not citation.get('direct_quote_valid', False):
                row_reasons.append('DIRECT_EVIDENCE_USE_QUOTE_NOT_VERIFIED')
            if use == 'paraphrased' and not (
                    citation.get('direct_quote_valid', False)
                    and citation.get('paraphrase_quote_valid', False)):
                row_reasons.append('PARAPHRASE_EVIDENCE_USE_QUOTE_NOT_VERIFIED')
            append_row(relation, oid, evidence, index, row_reasons, source_bound=True)
        for oid in sorted(set(unique_proposed_ids) - set(cited_ids)):
            row_reasons = list(shared_reasons)
            row_reasons.append('UNKNOWN_OR_UNPROVIDED_EVIDENCE_OBJECT' if oid in unknown_proposed
                               else 'EVIDENCE_OBJECT_RELATION_NOT_SOURCE_BOUND')
            append_row(relation, oid, by_object.get(oid), index, row_reasons)
        if not citations and not unique_proposed_ids:
            append_row(relation, None, None, index, shared_reasons)
    return dict(format='choice-evidence-relation-ledger-0.2.0', relations=rows,
        validation_reasons=sorted(set(reasons)),
        field_errors=[deepcopy(error) for row in rows
                      for error in (row.get('semantic_proof_validation') or {}).get('field_errors', [])],
        temporal_validation_reasons=sorted(set(reasons) & (temporal_only_reasons | temporal_conflict_reasons)),
        source_identity_or_citation_reasons=sorted(set(reasons) - temporal_only_reasons - temporal_conflict_reasons),
        temporal_conflict_present=bool(set(reasons) & temporal_conflict_reasons),
        supplied_not_adopted=True,
        direct_citation_not_reading_proof=True, clinical_reference=False)


def report_objects(source):
    """Compile lossless report roles without inventing event identities.

    The existing discovery event remains an optional historical execution anchor;
    these objects add a distinct, reusable evidence identity rather than silently
    changing the historical event's proof obligations.
    """
    if source.get('kind') not in REPORT_KINDS:
        raise ValueError('EVIDENCE_OBJECT_REQUIRES_REPORT_SOURCE')
    if not isinstance(source.get('text'), str) or not source['text']:
        raise ValueError('EVIDENCE_OBJECT_FULL_SOURCE_TEXT_REQUIRED')
    clocks = deepcopy(source.get('clocks') or {})
    identity = {
        'source_id': source['source_id'],
        'source_sha256': source.get('source_sha256') or hashlib.sha256(source['text'].encode()).hexdigest(),
    }
    common = dict(identity,
        source_version=source.get('source_version') or 'content-sha256:' + identity['source_sha256'],
        original_text=source['text'], source_clocks=clocks,
        time_provenance=deepcopy(source.get('time_provenance', {})),
        source_provenance=deepcopy(source.get('provenance', {})),
        choice_time_established=False, clinician_reading_established=False,
        clinical_reference=False)
    execution_clock = next((clocks.get(role) for role in
        ('observation_effective_time', 'sample_time', 'specimen_time', 'exam_time')
        if _clock(clocks.get(role)).get('value')), None)
    anchor = dict(format='test-execution-anchor-0.1.0',
        anchor_id='TA' + _digest(['test-execution-anchor', identity])[:20], **deepcopy(common),
        execution_status='REPORT_OR_RECORD_PRESENT_NOT_CLINICALLY_CERTIFIED',
        execution_time=_clock(execution_clock),
        execution_time_basis='NAMED_EXECUTION_CLOCK' if execution_clock else 'UNKNOWN_NOT_PUBLICATION_TIME',
        choice_time=None, time_basis='historical_unknown',
        evidence_object_id='EO' + _digest(['evidence-object', identity])[:20])
    publication_errors = release_time_provenance(source)
    publication = _publication_clock(clocks) if not publication_errors else _clock(None)
    evidence = dict(format='evidence-object-0.1.0',
        evidence_object_id=anchor['evidence_object_id'], **deepcopy(common),
        kind=source['kind'], title=source.get('title'),
        execution_anchor_id=anchor['anchor_id'],
        publication_time=publication,
        publication_status='PUBLICATION_TIME_PRESENT_NOT_READING_CERTIFIED'
            if publication.get('value') else
            'PUBLICATION_TIME_UNRESOLVED_REVIEW_REQUIRED' if publication_errors else
            'PUBLICATION_TIME_UNKNOWN_NOT_PROVEN_ABSENT',
        publication_time_provenance_errors=publication_errors,
        reading_status='UNKNOWN', causal_role='not_established',
        permitted_use='EVIDENCE_FOR_LATER_MANAGEMENT_CHOICE_NOT_SELECTION_OF_ITSELF')
    return anchor, evidence


def evidence_object_index(sources):
    objects = {}
    for source in sources:
        if source.get('kind') not in REPORT_KINDS:
            continue
        obj = report_objects(source)[1]
        if obj['source_id'] in objects and objects[obj['source_id']] != obj:
            raise ValueError('DUPLICATE_SOURCE_ID_WITH_DIFFERENT_EVIDENCE_OBJECT')
        objects[obj['source_id']] = obj
    return objects


def lineage_metrics(events):
    """Keep supplied/cited/proposed/visibility/scope layers as distinct counts."""
    result = dict(events=len(events), source_supplied=0, directly_cited=0,
        paraphrase_linked=0, publication_before_choice_proposed=0,
        reading_before_choice_proposed=0, evidence_predecessor_proposed=0,
        publication_before_choice_established=0, reading_before_choice_established=0,
        evidence_predecessor_selected=0,
        episode_linked=0, materialized=0, development_scope_only=0)
    for event in events:
        result['source_supplied'] += bool(event.get('actual_model_evidence'))
        result['directly_cited'] += any(
            item.get('kind') in REPORT_KINDS for item in event.get('actual_model_evidence', [])) and any(
            cite.get('source_id') in {item.get('source_id') for item in event.get('actual_model_evidence', [])}
            for cite in (event.get('comparison_validation') or {}).get('current_information', {}).get('valid', []))
        relations = (event.get('evidence_relation_ledger') or {}).get('relations', [])
        bound = [row for row in relations if row.get('relation_status') == 'SOURCE_BOUND_PROPOSAL']
        result['paraphrase_linked'] += any(row.get('evidence_use') == 'paraphrased' for row in bound)
        result['publication_before_choice_proposed'] += any(row.get('publication_before_choice') == 'yes' for row in bound)
        result['reading_before_choice_proposed'] += any(row.get('reading_before_choice') == 'yes' for row in bound)
        result['evidence_predecessor_proposed'] += any(row.get('causal_role') in ('trigger', 'baseline') for row in bound)
        result['episode_linked'] += bool(event.get('episode_memberships'))
        result['materialized'] += bool(event.get('materialized'))
        result['development_scope_only'] += bool(event.get('development_review_scope'))
    result['counts_are_not_clinical_recall_or_correctness'] = True
    result['direct_citation_is_not_reading_or_adoption'] = True
    result['model_temporal_and_causal_assertions_are_not_established_by_this_ledger'] = True
    return result
