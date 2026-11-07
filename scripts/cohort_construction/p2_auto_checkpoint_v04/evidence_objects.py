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

    def append_row(relation, oid, evidence, index, row_reasons, source_bound=False):
        identity = [event['event_id'], oid, index, relation.get('evidence_use'),
                    relation.get('evidence_object_ids', [])]
        row = {k: deepcopy(v) for k, v in relation.items() if k != 'validated_object_citations'}
        row.update(relation_id=('ER' + _digest(identity)[:20]
                                if oid else None),
            choice_event_id=event['event_id'], evidence_object_id=oid,
            relation_status='SOURCE_BOUND_PROPOSAL' if source_bound and not row_reasons else 'UNRESOLVED',
            validation_reasons=sorted(set(row_reasons)),
            publication_before_choice=relation.get('publication_before_choice', 'unknown'),
            reading_before_choice=relation.get('reading_before_choice', 'unknown'),
            novelty=relation.get('novelty', 'uncertain'),
            causal_role=relation.get('causal_role', 'not_established'),
            clinician_reading_established=False, clinical_causality_certified=False,
            publication_before_choice_established=False, reading_before_choice_established=False,
            novelty_established=False, causal_role_established=False,
            semantic_proposal_not_clinical_truth=True)
        if use == 'paraphrased' and evidence is not None and any(
                citation.get('evidence_object_id') == oid for citation in citations):
            block = next((citation for citation in citations
                          if citation.get('evidence_object_id') == oid), {})
            # Source-character binding and narrative/report chronology are
            # separate. Same-minute/day overlap is not silently ordered, and a
            # record-time proxy never proves clinician reading or actual use.
            publication = _clock(next((item.get('publication_time') for item in supplied_blocks
                if item.get('evidence_object_id') == oid), evidence.get('publication_time')))
            order = _compare_clocks(narrative_clock, publication)
            compatible = order in ('AFTER', 'SAME_TIME_UNORDERED',
                                   'SAME_MINUTE_UNORDERED', 'SAME_DAY_UNORDERED')
            if order == 'BEFORE':
                row_reasons.append('NARRATIVE_RECORD_TIME_BEFORE_EVIDENCE_PUBLICATION')
            elif not compatible:
                row_reasons.append('NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED')
            row.update(narrative_record_clock=narrative_clock,
                evidence_publication_clock=publication,
                narrative_record_to_publication_order=order,
                narrative_publication_time_compatible=compatible,
                narrative_record_time_order_not_reading_proof=True)
            row['relation_status'] = 'SOURCE_BOUND_PROPOSAL' if source_bound and not row_reasons else 'UNRESOLVED'
            row['validation_reasons'] = sorted(set(row_reasons))
        if evidence is not None:
            row.update(source_id=evidence['source_id'], source_sha256=evidence['source_sha256'])
        rows.append(row)
        reasons.extend(row_reasons)

    for index, raw in enumerate(validated_relations or []):
        relation = deepcopy(raw)
        # Source times are looked up in the exact submitted pool, not in a
        # possibly divergent mutable source object used only to rebuild EOs.
        narrative_quotes = [
            quote
            for citation in relation.get('validated_object_citations', [])
            for status in ('valid', 'invalid', 'ambiguous')
            for quote in ((citation.get('narrative_quote_validation') or {}).get(status, []))
        ]
        narrative_keys = {
            (quote.get('source_id'), quote.get('start'), quote.get('end'))
            for quote in narrative_quotes if quote.get('source_id')
        }
        narrative_blocks = [
            block for block in supplied_blocks
            if (block.get('source_id'), block.get('start'), block.get('end')) in narrative_keys
        ]
        if len(narrative_blocks) == len(narrative_keys) and narrative_keys:
            narrative_clocks = {
                (block.get('source_id'), block.get('start'), block.get('end')):
                    _clock(block.get('clock'))
                for block in narrative_blocks
            }
        else:
            narrative_clocks = {}
        if len(narrative_clocks) == 1:
            narrative_clock = deepcopy(next(iter(narrative_clocks.values())))
        elif narrative_keys:
            narrative_clock = _clock(None)
        else:
            narrative_clock = _clock(event.get('source_document_clock'))
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
    return dict(format='choice-evidence-relation-ledger-0.1.0', relations=rows,
        validation_reasons=sorted(set(reasons)), supplied_not_adopted=True,
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
