"""Fictional researcher task for UI/receiver tests; never clinical evidence."""
from scripts.cohort_construction.p2_checkpoint_materials.prepare import prepare_material_review
from scripts.cohort_construction.p2_checkpoint_materials.review import create_review_draft
from scripts.cohort_construction.p2_checkpoint_packet.compiler import text_sha256
from .build import build_assignment


def synthetic_inputs(*, literal_markup=False):
    text = '虚构观察甲。随后决定切除。虚构术后稳定。🧬\r\n'
    if literal_markup:
        text += '</script><script>window.XSS_EXECUTED=true</script><img src="https://xss.invalid/leak" onerror="window.XSS_EXECUTED=true">'
    source = dict(source_id='S-fictional-included', text=text, source_sha256=text_sha256(text))
    outside_text = '虚构范围外原文。此来源尚未进行覆盖审核。🧪\r\n'
    outside = dict(source_id='S-fictional-outside', text=outside_text, source_sha256=text_sha256(outside_text))
    quote = '随后决定切除。';start = text.index(quote)
    citation = dict(source_id=source['source_id'], source_sha256=source['source_sha256'],
                    quote=quote, start=start, end=start+len(quote), status='VALID',
                    raw_quote_verified=True, span_verified=True, eligible_as_source_support=True)
    event = dict(event_id='SYNTHETIC-NOT-A-CLINICAL-DECISION', source_id=source['source_id'],
                 source_ids=[source['source_id']], actual_model_evidence=[],
                 choice_boundary_citations={'valid':[citation],'invalid':[],'ambiguous':[]},
                 postchoice_citations={'valid':[],'invalid':[],'ambiguous':[]},
                 validated_observation_citations={'valid':[]}, availability_validation=[],
                 numeric_lexeme_test=[1.0, 1e-7])
    manifest, proposal, compiled, preparation = prepare_material_review(
        event, [source, outside], decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-ONLY')
    draft = create_review_draft(compiled, manifest, preparation, reviewer_id='AI-DRAFT-NOT-A-HUMAN')
    return dict(compiled=compiled, manifest=manifest, draft=draft,
                preparation_audit=preparation, selected_event=event), [source, outside], proposal


def synthetic_assignment(*, literal_markup=False, load_excluded=True):
    documents, sources, _ = synthetic_inputs(literal_markup=literal_markup)
    return build_assignment(**documents, assignment_id='SYNTHETIC-RESEARCHER-ASSIGNMENT',
                             coverage_sources=sources if load_excluded else ())
