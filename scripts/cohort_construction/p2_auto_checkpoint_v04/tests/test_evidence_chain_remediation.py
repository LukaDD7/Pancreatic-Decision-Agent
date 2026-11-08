"""CE-REM-001/002/003 synthetic regressions; no clinical labels or remote calls."""
from copy import deepcopy

from scripts.cohort_construction.p2_auto_checkpoint import fhir_io
from scripts.cohort_construction.p2_auto_checkpoint_v04 import engine
from scripts.cohort_construction.p2_auto_checkpoint_v04.evidence import report_objects, lineage_metrics
from scripts.cohort_construction.p2_auto_checkpoint_v04.context import context_for_discovery
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_engine import FakeClient, integration_fixture, event, review, source, select
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_context import source as report_source
from scripts.cohort_construction.p2_auto_checkpoint_v05_refine import refine
from scripts.cohort_construction.p2_pathway_replay.core import project_sources, resource_selection, clock, \
    validate_assessment_round, exact_duplicate_groups


def imaging_row(**changes):
    row = dict(index_report_uid='SYNTHETIC-REPORT-1', exam_method='CT',
        original_findings='虚构：局部病灶。', original_impression='虚构：待进一步明确。',
        exam_datetime='2022-09-01 08:00', exam_no_raw='EXAM-1', raw_record_path='synthetic/report.jsonl')
    row.update(changes)
    return row


def projected(**changes):
    bundle = dict(imaging_all=[imaging_row(**changes)])
    sources, inventory = project_sources(bundle, 'SYNTHETIC-PATIENT')
    return sources[0], inventory


def test_CE_REM001_legacy_projection_identity_text_and_missingness_preserved():
    current, _ = projected()
    expected = '[original_findings]\n虚构：局部病灶。\n[original_impression]\n虚构：待进一步明确。\n'
    assert current['text'] == expected
    assert current['source_id'] == __import__('hashlib').sha256(
        ('SYNTHETIC-PATIENT|imaging_all|SYNTHETIC-REPORT-1|'
         + __import__('hashlib').sha256(expected.encode()).hexdigest()).encode()).hexdigest()[:24]
    assert current['clocks']['exam_time']['value'] == '2022-09-01 08:00:00'
    for role in ('report_time', 'issued', 'verified_time'):
        assert current['time_provenance'][role]['status'] == 'NOT_MAPPED_NO_NAMED_FIELD_IN_PROVIDED_ROW'
        assert current['time_provenance'][role]['missing_reason'] == 'UNAVAILABLE_NOT_PROVEN_ABSENT'
    assert current['clock_basis'] == 'exam_time_proxy_release_unknown'
    assert current['availability_certified'] is False
    assert current['clinician_reading_established'] is False


def test_CE_REM001_named_report_issue_verify_header_and_fhir_roundtrip():
    current, _ = projected(report_datetime='2022-09-01 10:00', issued='2022-09-01 10:00',
        verified_time='2022-09-01 10:30', original_report_header='虚构原始报告头：不得解析成临床所见。',
        report_time='', raw_record_path='synthetic/raw-path.jsonl')
    assert current['original_fields']['original_report_header'].startswith('虚构原始报告头')
    assert current['clocks']['report_time']['value'] == '2022-09-01 10:00:00'
    assert current['clocks']['issued']['value'] == '2022-09-01 10:00:00'
    assert current['clocks']['verified_time']['value'] == '2022-09-01 10:30:00'
    assert current['time_provenance']['report_time']['source_field'] == 'report_datetime'
    assert current['clock']['value'] == '2022-09-01 10:00:00'
    assert current['provenance']['raw_record_path'] == 'synthetic/raw-path.jsonl'
    bundle = fhir_io.build([current], 'SYNTHETIC', [], [])
    restored, _ = fhir_io.import_sources(bundle)
    assert restored[0]['text'] == current['text']
    assert restored[0]['source_id'] == current['source_id']
    assert restored[0]['clocks'] == current['clocks']
    assert restored[0]['time_provenance'] == current['time_provenance']
    assert restored[0]['original_fields'] == current['original_fields']


def test_CE_REM001_empty_named_field_not_unmapped_and_report_proxy_not_reading():
    current, _ = projected(report_time='')
    assert current['time_provenance']['report_time']['status'] == 'FIELD_PRESENT_BUT_EMPTY'
    current['clocks']['report_time'] = dict(value='2022-09-01 10:00:00', precision='minute', raw='2022-09-01 10:00')
    current['clocks']['exam_time'] = dict(value='2022-09-01 08:00:00', precision='minute', raw='2022-09-01 08:00')
    selected = resource_selection(current, '2022-09-01 09:00:00', '2022-01-01')
    assert selected['clock_basis'] == 'report_time'
    assert selected['disposition'] == 'EXCLUDE'
    assert current['clinician_reading_established'] is False


def test_CE_REM001_report_object_separates_execution_evidence_and_reading():
    current, _ = projected(report_time='2022-09-01 10:00')
    anchor, evidence = report_objects(current)
    assert anchor['choice_time'] is None and anchor['time_basis'] == 'historical_unknown'
    assert anchor['execution_time']['value'] == '2022-09-01 08:00:00'
    assert evidence['publication_time']['value'] == '2022-09-01 10:00:00'
    assert evidence['reading_status'] == 'UNKNOWN'
    assert not anchor['clinician_reading_established']
    assert not evidence['clinician_reading_established']
    assert evidence['execution_anchor_id'] == anchor['anchor_id']
    assert 'NOT_SELECTION_OF_ITSELF' in evidence['permitted_use']


def choice_fixture(kind='new_management_choice', current_fact=False):
    text = '拟行超声内镜引导下细针穿刺。'
    current = source('EUS', text, '2022-09-02 09:00')
    e = event('EUS-CHOICE', current, action=text, time_basis='record_time_proxy', phase='术前',
        prechoice_citations={'valid': [], 'invalid': [], 'ambiguous': []},
        choice_boundary_citations=engine.resolve_model_quotes([dict(source_id='EUS', quote=text)], [current], []))
    quote = dict(source_id='EUS', quote=text)
    r = review('EUS-CHOICE', checkpoint_kind=kind, previous_event_id=None, same_process_previous_id=None,
        evidence_evolution='none', process_link='uncertain', evidence_connection='uncertain',
        evidence_comparison=dict(previous_known_quotes=[], current_information_quotes=[quote] if current_fact else [],
            change_kind='none', reinterpretation_quotes=[], explanation='虚构当前管理安排，不是选择前事实。'),
        evidence_relations=[], proof_event_role='new_management_choice',
        temporal_channel_status='not_applicable',semantic_channel_status='not_applicable',
        key_management_predecessor_rationale='')
    ctx = engine.build_evidence_context(e, [current], [e], preserve_current_context=True)
    return e, r, [current], ctx


def test_CE_REM002_new_EUS_choice_does_not_have_to_predate_itself():
    e, r, sources, ctx = choice_fixture()
    comparison, _, reasons = engine.validate_semantic_contract(e, r, ctx, sources, [e], [])
    e['semantic_contract_validation'] = reasons
    status, reasons = select(e=e, r=r, observations={'valid': [], 'invalid': [], 'ambiguous': []},
        connections={'valid': [], 'invalid': [], 'ambiguous': []}, earlier_ids=set())
    assert status == 'CANDIDATE_NEW_MANAGEMENT_CHOICE'
    assert reasons == []
    assert comparison['current_information']['valid'] == []


def test_CE_REM002_current_action_is_field_error_event_remains_available():
    e, r, sources, ctx = choice_fixture(current_fact=True)
    comparison, _, contract = engine.validate_semantic_contract(e, r, ctx, sources, [e], [])
    e['semantic_contract_validation'] = contract
    assert comparison['field_errors'][0]['code'] == 'CURRENT_ACTION_USED_AS_NEW_PATIENT_FACT'
    assert comparison['field_errors'][0]['repairable'] is True
    status, reasons = select(e=e, r=r, observations={'valid': [], 'invalid': [], 'ambiguous': []},
        connections={'valid': [], 'invalid': [], 'ambiguous': []}, earlier_ids=set())
    assert status == 'UNCERTAIN_CONTRACT_OR_SOURCE'
    assert reasons == ['CURRENT_ACTION_USED_AS_NEW_PATIENT_FACT']
    r['checkpoint_kind'] = 'evidence_informed_choice'
    comparison, _, contract = engine.validate_semantic_contract(e, r, ctx, sources, [e], [])
    e['semantic_contract_validation'] = contract
    status, reasons = select(e=e, r=r, observations={'valid': [], 'invalid': [], 'ambiguous': []},
        connections={'valid': [], 'invalid': [], 'ambiguous': []}, earlier_ids=set())
    assert status == 'UNCERTAIN_CONTRACT_OR_SOURCE'
    assert 'CURRENT_ACTION_USED_AS_NEW_PATIENT_FACT' in reasons
    assert e['event_id'] == 'EUS-CHOICE'


def test_CE_REM002_management_kind_cannot_bypass_evidence_informed_proof():
    e, r, sources, ctx = choice_fixture()
    r['checkpoint_kind'] = 'evidence_informed_choice'
    r['evidence_comparison']['change_kind'] = 'changed_state'
    _, _, contract = engine.validate_semantic_contract(e, r, ctx, sources, [e], [])
    e['semantic_contract_validation'] = contract
    status, reasons = select(e=e, r=r, observations={'valid': [], 'invalid': [], 'ambiguous': []},
        connections={'valid': [], 'invalid': [], 'ambiguous': []}, earlier_ids=set())
    assert status == 'UNCERTAIN_CONTRACT_OR_SOURCE'
    assert {'PRIOR_ROUND_UNRESOLVED', 'NO_VALID_PRIOR_OBSERVATION', 'NO_VERIFIED_CONNECTION_QUOTE'} <= set(reasons)
    assert 'MODEL_NEWNESS_CONFLICT' in reasons


def test_CE_REM002_report_cannot_claim_new_management_choice_of_itself():
    e, r, _, _ = choice_fixture()
    e['kind'] = 'performed_test_report'
    status, reasons = select(e=e, r=r, observations={'valid': [], 'invalid': [], 'ambiguous': []},
        connections={'valid': [], 'invalid': [], 'ambiguous': []}, earlier_ids=set())
    assert status == 'UNCERTAIN_CONTRACT_OR_SOURCE'
    assert 'REPORT_RESULT_CANNOT_PROVE_SELECTION_OF_ITSELF' in reasons


def relation_fixture():
    imaging = report_source('CT', '静脉受侵。', when='2022-09-01 08:00', title='CT', kind='imaging')
    imaging['clocks']['report_time'] = dict(value='2022-09-01 10:00:00', precision='minute', raw='2022-09-01 10:00')
    current = source('C', 'CT提示静脉受侵。决定补充病理取材。', '2022-09-02 10:00')
    e = event('LATER', current, action='决定补充病理取材。', choice_time='2022-09-02 10:00',
        time_basis='explicit_documented_choice', phase='术前', prechoice_citations={'valid': [], 'invalid': [], 'ambiguous': []},
        choice_boundary_citations=engine.resolve_model_quotes([dict(source_id='C',quote='决定补充病理取材。')], [current], []))
    ctx = engine.build_evidence_context(e, [imaging, current], [e], preserve_current_context=True)
    return e, [imaging, current], ctx


def proposal(use, narrative):
    return dict(event_id='LATER', evidence_relations=[dict(evidence_object_ids=[], evidence_use=use,
        direct_quote=dict(source_id='MODEL-CANNOT-REASSIGN-EO', quote='静脉受侵。'),
        narrative_quote=narrative, publication_before_choice='yes', reading_before_choice='unknown',
        novelty='new', causal_role='trigger', explanation='虚构证据关系提案，不是临床采用证明。',
        proof_event_role='evidence_informed_management_choice',
        temporal_channel_status='temporally_possibly_visible' if use=='direct' else 'unresolved',
        proof_channels=['report_time'] if use=='direct' else [],
        semantic_channel_status='not_applicable' if use=='direct' else 'explicit_before_choice',
        semantic_order='unknown' if use=='direct' else 'before',
        current_action_quote=dict(source_id='C',quote='决定补充病理取材。'),
        connection_quote=dict(source_id='C',quote='CT提示静脉受侵。决定补充病理取材。') if narrative else None,
        proof_explanation='纯虚构：时间链与语义链分开，不是临床Reference。')])


def test_CE_REM003_report_direct_evidence_can_bind_later_management_choice():
    e, sources, ctx = relation_fixture()
    oid = next(b['evidence_object_id'] for b in ctx['blocks'] if b['source_id'] == 'CT')
    proposed = proposal('direct', None)
    proposed['evidence_relations'][0]['evidence_object_ids'] = [oid]
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    ledger = engine.compile_evidence_relations(e, rows, sources, context=ctx)
    assert ledger['validation_reasons'] == []
    row = ledger['relations'][0]
    assert row['source_id'] == 'CT' and row['relation_status'] == 'SOURCE_BOUND_PROPOSAL'
    assert row['reading_before_choice'] == 'unknown'
    assert row['clinician_reading_established'] is False
    assert row['clinical_causality_certified'] is False
    assert ctx['same_test_source_ids'] == []


def test_CE_REM003_paraphrase_requires_both_original_and_separate_narrative():
    e, sources, ctx = relation_fixture()
    oid = next(b['evidence_object_id'] for b in ctx['blocks'] if b['source_id'] == 'CT')
    proposed = proposal('paraphrased', dict(source_id='C', quote='CT提示静脉受侵。'))
    proposed['evidence_relations'][0]['evidence_object_ids'] = [oid]
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    assert engine.compile_evidence_relations(e, rows, sources, context=ctx)['validation_reasons'] == []
    proposed['evidence_relations'][0]['narrative_quote'] = dict(source_id='CT', quote='静脉受侵。')
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    assert not rows[0]['validated_object_citations'][0]['paraphrase_quote_valid']
    assert 'PARAPHRASE_EVIDENCE_USE_QUOTE_NOT_VERIFIED' in engine.compile_evidence_relations(
        e, rows, sources, context=ctx)['validation_reasons']


def test_CE_REM003_unknown_object_or_other_report_cannot_be_substituted():
    e, sources, ctx = relation_fixture()
    proposed = proposal('direct', None)
    proposed['evidence_relations'][0]['evidence_object_ids'] = ['EO-NOT-SUPPLIED']
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    ledger = engine.compile_evidence_relations(e, rows, sources, context=ctx)
    assert ledger['relations'][0]['relation_status'] == 'UNRESOLVED'
    assert 'UNKNOWN_OR_UNPROVIDED_EVIDENCE_OBJECT' in ledger['validation_reasons']


def test_CE_REM003_own_test_remains_excluded_from_its_own_selection_context():
    imaging = report_source('CT', '静脉受侵。', when='2022-09-01 08:00', title='CT', kind='imaging')
    e = event('OWN', imaging, action='静脉受侵。', kind='performed_test_report', choice_time=None,
        time_basis='historical_unknown', same_test_source_ids=['CT'], source_ids=['CT'])
    ctx = engine.build_evidence_context(e, [imaging], [e])
    assert ctx['blocks'] == []
    assert ctx['same_test_source_ids'] == ['CT']
    assert any(row.get('disposition') == 'EXCLUDE_OWN_TEST_RESULT' for row in ctx['ledger'])
