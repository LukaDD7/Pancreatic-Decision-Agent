"""Synthetic source-first review tests; no actual people or clinical conclusions."""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from scripts.cohort_construction.p2_auto_checkpoint.extract import clock
from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import build
from scripts.cohort_construction.p2_auto_checkpoint_v04.context import build_units
from scripts.cohort_construction.p2_auto_checkpoint_v06.independent_validation import (
    ValidationError, compare, prepare, reveal, submit, summarize,
)
from scripts.cohort_construction.p2_auto_checkpoint_v06.independent_validation.adapter import (
    load_case, load_release, population, scope_sources,
)
from scripts.cohort_construction.p2_auto_checkpoint_v06.independent_validation.core import (
    DIMENSIONS, bound, digest, read, sha_bytes, sha_text, validate_citations, write,
)


def replace(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def fixture_release(tmp_path, *, old_inventory=False, support=True):
    release = tmp_path / 'frozen_release'
    case = release / 'SCALE-TEST'
    case.mkdir(parents=True)
    sources = []
    for sid in ('A', 'B', 'C', 'Z', 'F', 'LATE', 'UNKNOWN', 'SUPPORT'):
        if sid == 'SUPPORT' and not support:
            continue
        at = None if sid == 'UNKNOWN' else ('2026-10-20 10:00' if sid == 'LATE' else '2026-10-01 10:00')
        text = '某😀患者原始记录。团队暂缓并建议完善证据。' + sid
        source = dict(source_id=sid, patient_uid='fixture', kind='document', family='fixture',
            title='护理记录' if sid == 'SUPPORT' else '查房记录', text=text, source_sha256=sha_text(text),
            clocks={'body_record_time': clock(at)}, clock=clock(at), clock_basis='body_record_time',
            provenance={}, original_fields={}, flags=[])
        sources.append(source)
    units = build_units(sources, {'include_support_documents': False})
    unit_by_source = {unit['source_ids'][0]: unit for unit in units}
    results = []
    for sid in ('A', 'B', 'C', 'Z', 'LATE', 'UNKNOWN'):
        unit = unit_by_source[sid]
        events = [dict(event_id='E-' + sid, source_id=sid, source_ids=[sid], unit_id=unit['unit_id'],
                       record_function='model_judgment', checkpoint_status='PROGRAM_CANDIDATE',
                       semantic_review={'rationale': 'MODEL_ONLY_SECRET_' + sid})] if sid in ('A', 'B', 'C') else []
        rejected = [dict(reason='MODEL_REJECTED_SECRET', quotes=[])] if sid in ('B', 'C') else []
        results.append(dict(unit_id=unit['unit_id'], events=events, rejected_mentions=rejected,
                            receipt={'request_sha256': 'synthetic-request'}, input_context_summary={}))
    events = [deepcopy(event) for item in results for event in item['events']]
    values = {
        'source_fhir_r4_bundle.json': build(sources, 'fixture', [], []),
        'unit_inventory.json': {'version': 'fixture', 'units': units},
        'summary.json': {'case_id': case.name, 'candidate_checkpoints': len(events)},
        'discovery.json': dict(units=results, events=events,
                               failures=[dict(unit_id=unit_by_source['F']['unit_id'], stage='discovery', error='FixtureFailure')]),
        'decision_events.json': events, 'episodes.json': [], 'relations.json': [],
    }
    if old_inventory:
        original = tmp_path / 'original_run' / case.name
        original.mkdir(parents=True)
        replace(original / 'unit_inventory.json', values.pop('unit_inventory.json'))
        values['summary.json']['original_input_directory'] = str(original)
    for name, value in values.items():
        replace(case / name, value)
    return release


def batch_fixture(tmp_path, **options):
    release = fixture_release(tmp_path, **options)
    batch = tmp_path / 'private' / 'batch'
    result = prepare(release, batch, per_stratum=1)
    assert result['task_count'] == 4
    return batch


def task_files(batch, task_id='IV-0001'):
    folder = batch / 'phase_a' / task_id
    return read(folder / 'task.json'), read(folder / 'sources.json'), read(folder / 'source_review_template.json')


def completed_source_review(batch, tmp_path, *, task_id='IV-0001', reviewer='person-A'):
    task, bundle, review = task_files(batch, task_id)
    source = bundle['documents'][0]
    quote = dict(source_id=source['source_id'], source_sha256=source['source_sha256'],
                 start=1, end=4, exact_quote=source['text'][1:4])
    review.update(origin='independent_human', status='INDEPENDENT_COMPLETE', reviewer_id=reviewer,
                  independence_attested=True, complete_within_declared_scope=True,
                  read_source_ids=[source['source_id'] for source in bundle['documents']],
                  no_target_management_judgment_found=False,
                  findings=[dict(finding_id='F1', kind='management_judgment',
                                 description='Synthetic reviewer interpretation, not clinical truth.', citations=[quote])])
    path = tmp_path / (task_id + '-' + reviewer + '-source.json')
    replace(path, review)
    return path, review


def comparison_fixture(batch, tmp_path, *, reviewer='person-A'):
    source_path, source_review = completed_source_review(batch, tmp_path, reviewer=reviewer)
    submit(batch, source_path)
    reveal(batch, 'IV-0001', reviewer)
    template = batch / 'researcher_only' / 'revealed' / 'IV-0001' / reviewer / 'comparison_template.json'
    comparison = read(template)
    comparison.update(origin='independent_human', status='INDEPENDENT_COMPLETE', independence_attested=True)
    for dimension in comparison['dimensions']:
        dimension.update(assessment='UNRESOLVED', rationale='Synthetic scope lacks the necessary comparison evidence.')
    path = tmp_path / (reviewer + '-comparison.json')
    replace(path, comparison)
    return path, comparison


def test_CE_IV001_phase_a_contains_only_original_scope_and_pending_preparation(tmp_path):
    batch = batch_fixture(tmp_path)
    material = '\n'.join(path.read_text() for path in (batch / 'phase_a').rglob('*') if path.is_file())
    assert 'MODEL_ONLY_SECRET' not in material and 'MODEL_REJECTED_SECRET' not in material
    for term in ('selected_stratum', 'eligible_strata', 'checkpoint_status', 'semantic_review', 'proposals_sha256'):
        assert term not in material
    _, bundle, template = task_files(batch)
    assert len(bundle['documents']) == 1
    assert template['origin'] == 'ai_preparation' and template['status'] == 'PENDING'
    assert not template['independence_attested']


def test_CE_IV001_reveal_rejects_absent_source_review(tmp_path):
    batch = batch_fixture(tmp_path)
    with pytest.raises(ValidationError, match='INDEPENDENT_SOURCE_REVIEW_REQUIRED'):
        reveal(batch, 'IV-0001', 'person-A')
    assert not (batch / 'researcher_only' / 'revealed').exists()


@pytest.mark.parametrize('origin', ['ai_preparation', 'synthetic_test'])
def test_CE_IV001_nonhuman_cannot_assert_independent_completion(tmp_path, origin):
    batch = batch_fixture(tmp_path)
    path, review = completed_source_review(batch, tmp_path)
    review['origin'] = origin
    replace(path, review)
    with pytest.raises(ValidationError, match='AI_OR_SYNTHETIC_CANNOT_COMPLETE'):
        submit(batch, path)


def test_CE_IV001_ai_pending_draft_never_reveals_or_enters_completed_denominator(tmp_path):
    batch = batch_fixture(tmp_path)
    _, _, draft = task_files(batch)
    draft['reviewer_id'] = 'AI-preparer'
    path = tmp_path / 'draft.json'
    replace(path, draft)
    assert submit(batch, path)['independently_complete'] is False
    with pytest.raises(ValidationError, match='INDEPENDENT_SOURCE_REVIEW_REQUIRED'):
        reveal(batch, 'IV-0001', 'AI-preparer')
    result = summarize(batch)
    assert result['draft_submissions'] == 1
    assert result['independent_source_review_submissions'] == 0
    assert result['independent_comparison_submissions'] == 0


def test_CE_IV001_complete_human_review_reveals_a_separate_researcher_file(tmp_path):
    batch = batch_fixture(tmp_path)
    before = {path: path.read_bytes() for path in (batch / 'phase_a').rglob('*') if path.is_file()}
    path, _ = completed_source_review(batch, tmp_path)
    receipt = submit(batch, path)
    revealed = reveal(batch, 'IV-0001', 'person-A')
    assert receipt['independently_complete'] is True
    assert not revealed['doctor_packet']
    assert all(path.read_bytes() == data for path, data in before.items())
    material = read(batch / 'researcher_only' / 'revealed' / 'IV-0001' / 'person-A' / 'comparison_material.json')
    assert 'proposals' in material and material['warning'].startswith('Researcher comparison')
    assert material['source_review_sha256'] == receipt['submission_sha256']


@pytest.mark.parametrize('reason_kind', ['uncertain_finding', 'source_gap'])
def test_CE_IV001_complete_reading_with_unresolved_target_can_reveal_without_forcing_label(tmp_path, reason_kind):
    batch = batch_fixture(tmp_path)
    path, review = completed_source_review(batch, tmp_path)
    review['no_target_management_judgment_found'] = None
    if reason_kind == 'uncertain_finding':
        review['findings'][0]['kind'] = 'uncertain'
    else:
        review['findings'] = []
        review['unresolved_issues'] = [dict(issue_id='Q1', description='Missing earlier complete source; target identity remains unresolved.',
                                          requested_source_ids=['not-delivered-reference'])]
    replace(path, review)
    assert submit(batch, path)['independently_complete']
    assert reveal(batch, 'IV-0001', 'person-A')['status'] == 'RESEARCHER_COMPARISON_REVEALED'
    result = summarize(batch)
    assert result['source_target_assessment_counts']['UNRESOLVED'] == 1
    assert result['source_target_assessment_counts']['NO_TARGET_FOUND_IN_SCOPE'] == 0


def test_CE_IV001_blank_unresolved_target_cannot_claim_completed_reading(tmp_path):
    batch = batch_fixture(tmp_path)
    path, review = completed_source_review(batch, tmp_path)
    review.update(no_target_management_judgment_found=None, findings=[], unresolved_issues=[])
    replace(path, review)
    with pytest.raises(ValidationError, match='UNRESOLVED_TARGET_REQUIRES_EXPLICIT_REASON'): submit(batch, path)


def test_CE_IV001_registered_same_patient_reveal_blocks_later_source_completion(tmp_path):
    batch = batch_fixture(tmp_path)
    path, _ = completed_source_review(batch, tmp_path)
    submit(batch, path)
    reveal(batch, 'IV-0001', 'person-A')
    later, _ = completed_source_review(batch, tmp_path, task_id='IV-0002')
    with pytest.raises(ValidationError, match='PRIOR_LOCAL_MODEL_EXPOSURE_BLOCKS'):
        submit(batch, later)
    assert not (batch / 'source_reviews' / 'IV-0002' / 'person-A').exists()


def test_CE_IV001_same_patient_source_reviews_frozen_before_reveal_remain_valid(tmp_path):
    batch = batch_fixture(tmp_path)
    first, _ = completed_source_review(batch, tmp_path)
    second, _ = completed_source_review(batch, tmp_path, task_id='IV-0002')
    submit(batch, first); submit(batch, second)
    reveal(batch, 'IV-0001', 'person-A')
    reveal(batch, 'IV-0002', 'person-A')
    assert summarize(batch)['independent_source_review_submissions'] == 2


@pytest.mark.parametrize('change,code', [
    ('quote', 'VERBATIM_SOURCE_BINDING_MISMATCH'),
    ('hash', 'VERBATIM_SOURCE_BINDING_MISMATCH'),
    ('offset', 'INVALID_SOURCE_CODEPOINT_SPAN'),
    ('scope', 'CITATION_OUTSIDE_DECLARED_SCOPE'),
    ('boolean_offset', 'INVALID_SOURCE_CODEPOINT_SPAN'),
])
def test_CE_IV002_exact_original_spans_hashes_and_unicode_are_required(tmp_path, change, code):
    batch = batch_fixture(tmp_path)
    path, review = completed_source_review(batch, tmp_path)
    citation = review['findings'][0]['citations'][0]
    if change == 'quote': citation['exact_quote'] = 'invented'
    elif change == 'hash': citation['source_sha256'] = 'wrong'
    elif change == 'offset': citation['end'] = 999999
    elif change == 'scope': citation['source_id'] = 'OUTSIDE'
    else: citation['start'] = True
    replace(path, review)
    with pytest.raises(ValidationError, match=code): submit(batch, path)


def test_CE_IV002_unicode_codepoint_positive_and_complete_no_target_are_valid(tmp_path):
    batch = batch_fixture(tmp_path)
    path, review = completed_source_review(batch, tmp_path)
    assert '😀' in review['findings'][0]['citations'][0]['exact_quote']
    assert submit(batch, path)['independently_complete']
    path, review = completed_source_review(batch, tmp_path, reviewer='person-B')
    review.update(no_target_management_judgment_found=True, findings=[])
    replace(path, review)
    assert submit(batch, path)['independently_complete']


def test_CE_IV002_source_scope_completeness_is_explicit_and_not_inferred(tmp_path):
    batch = batch_fixture(tmp_path)
    path, review = completed_source_review(batch, tmp_path)
    review['read_source_ids'] = []
    replace(path, review)
    with pytest.raises(ValidationError, match='ALL_DECLARED_SOURCES'): submit(batch, path)
    review['read_source_ids'] = [s['source_id'] for s in task_files(batch)[1]['documents']]
    review['complete_within_declared_scope'] = False
    replace(path, review)
    with pytest.raises(ValidationError, match='ALL_DECLARED_SOURCES'): submit(batch, path)


def test_CE_IV002_stale_task_and_source_artifact_hashes_are_rejected(tmp_path):
    batch = batch_fixture(tmp_path)
    path, review = completed_source_review(batch, tmp_path)
    review['task_sha256'] = 'stale'
    replace(path, review)
    with pytest.raises(ValidationError, match='SUBMISSION_TASK_BINDING'): submit(batch, path)
    source_path = batch / 'phase_a' / 'IV-0001' / 'sources.json'
    source = read(source_path); source['documents'][0]['text'] += 'tamper'
    replace(source_path, source)
    with pytest.raises(ValidationError, match='ARTIFACT_HASH_MISMATCH'): submit(batch, path)


def test_CE_IV003_deterministic_sampling_covers_four_layers_without_duplicate_tasks(tmp_path):
    release = fixture_release(tmp_path)
    first, second = tmp_path / 'private' / 'first', tmp_path / 'private' / 'second'
    prepare(release, first, seed=123, per_stratum=1)
    prepare(release, second, seed=123, per_stratum=1)
    a = read(first / 'researcher_only' / 'manifest.json')
    b = read(second / 'researcher_only' / 'manifest.json')
    assert a['tasks'] == b['tasks']
    assert all(row['selected'] == 1 for row in a['sampling'])
    keys = [(row['case_id'], tuple(row['source_ids'])) for row in a['tasks']]
    assert len(keys) == len(set(keys)) == 4
    assert any(len(row['eligible_strata']) > 1 for row in a['tasks'])


def test_CE_IV003_blind_task_numbers_follow_identity_hash_not_stratum_blocks(tmp_path):
    release = fixture_release(tmp_path)
    batch = tmp_path / 'private' / 'batch'
    prepare(release, batch, seed=123, per_stratum=1)
    manifest = read(batch / 'researcher_only' / 'manifest.json')
    keys = [digest([123, 'blind-task-order', row['case_id'], sorted(row['source_ids'])])
            for row in manifest['tasks']]
    assert keys == sorted(keys)
    assert [row['selected_stratum'] for row in manifest['tasks']] != manifest['sampling_order']
    assert [row['task_id'] for row in manifest['tasks']] == [f'IV-{i:04d}' for i in range(1, 5)]


def test_CE_IV003_request_failure_is_not_zero_and_empty_layer_is_reported(tmp_path):
    release = fixture_release(tmp_path, support=False)
    cases = load_release(release)
    strata, _ = population(cases)
    assert not any('F' in row['source_ids'] for row in strata['zero_discovery_units'])
    result = prepare(release, tmp_path / 'private' / 'batch', per_stratum=1)
    support = next(row for row in result['sampling'] if row['stratum'] == 'support_sources_not_discovered')
    assert support['population_size'] == 0 and support['selected'] == 0 and support['gap']


def test_CE_IV003_old_inventory_lookup_and_reconstructed_routing_are_explicit(tmp_path):
    release = fixture_release(tmp_path, old_inventory=True)
    case = load_release(release)[0]
    assert case['inventory_origin'] == 'summary_original_input_directory'
    assert case['routing_provenance']['kind'] == 'reconstructed_routing'
    assert case['routing_provenance']['original_run_receipt'] is False
    assert case['routing_provenance']['inventory_unit_membership_verified'] is True
    assert case['routing_provenance']['matching_include_support_documents'] == [False]


def test_CE_IV003_router_mismatch_preserves_gap_instead_of_inventing_support_exclusion(tmp_path):
    release = fixture_release(tmp_path)
    path = release / 'SCALE-TEST' / 'unit_inventory.json'
    inventory = read(path)
    removed = next(unit for unit in inventory['units'] if unit['source_ids'] == ['F'])
    inventory['units'].remove(removed)
    replace(path, inventory)
    case = load_release(release)[0]
    assert case['routing'] == []
    assert case['routing_provenance']['gap'] == 'FROZEN_UNIT_MEMBERSHIP_NOT_REPRODUCED_BY_CURRENT_ROUTER'
    assert population([case])[0]['support_sources_not_discovered'] == []


def test_CE_IV003_inventory_text_version_mismatch_is_rejected(tmp_path):
    release = fixture_release(tmp_path)
    path = release / 'SCALE-TEST' / 'unit_inventory.json'
    inventory = read(path)
    inventory['units'][0]['original_texts'][0]['text'] = 'wrong original version'
    replace(path, inventory)
    with pytest.raises(ValidationError, match='INVENTORY_SOURCE_VERSION'): load_release(release)


def test_CE_IV003_discovery_duplicate_object_content_and_unit_sources_are_checked(tmp_path):
    release = fixture_release(tmp_path)
    case = release / 'SCALE-TEST'
    path = case / 'discovery.json'
    discovery = read(path)
    original = deepcopy(discovery)
    discovery['events'][0]['record_function'] = 'contradictory copy'
    replace(path, discovery)
    with pytest.raises(ValidationError, match='DISCOVERY_EVENT_CONTENT'): load_release(release)
    replace(path, original)
    final = read(case / 'decision_events.json')
    final[0]['source_ids'] = ['Z']  # Known patient source, wrong current unit.
    replace(case / 'decision_events.json', final)
    with pytest.raises(ValidationError, match='EVENT_SOURCE_UNIT'): load_release(release)


def test_CE_IV004_default_scope_is_current_unit_and_expansion_is_explicit_proxy_only(tmp_path):
    release = fixture_release(tmp_path)
    case = load_release(release)[0]
    item = dict(source_ids=['A'])
    docs, scope = scope_sources(case, item, 0)
    assert [doc['source_id'] for doc in docs] == ['A']
    expanded, scope = scope_sources(case, item, 3)
    assert {'A', 'UNKNOWN', 'SUPPORT'} <= {doc['source_id'] for doc in expanded}
    assert 'LATE' not in {doc['source_id'] for doc in expanded}
    assert scope['proxy_clock_only'] and scope['context_days'] == 3
    assert 'not an Episode' in scope['limitation']
    unknown, scope = scope_sources(case, dict(source_ids=['UNKNOWN']), 3)
    assert len(unknown) == 1 and scope['anchor_time_unresolved']


def test_CE_IV004_date_only_source_time_is_unresolved_not_midnight(tmp_path):
    release = fixture_release(tmp_path)
    case = load_release(release)[0]
    source = next(source for source in case['sources'] if source['source_id'] == 'A')
    source['clock'] = {'value': '2026-10-01', 'precision': 'date'}
    docs, scope = scope_sources(case, dict(source_ids=['A']), 3)
    assert len(docs) == 1 and scope['anchor_time_unresolved']
    assert docs[0]['source_clock']['value'] == '2026-10-01'


def test_CE_IV004_comparison_preserves_unresolved_and_reports_human_denominators(tmp_path):
    batch = batch_fixture(tmp_path)
    path, _ = comparison_fixture(batch, tmp_path)
    result = compare(batch, path)
    assert result['independently_complete']
    summary = summarize(batch)
    assert summary['prepared_tasks'] == 4
    assert summary['independent_comparison_submissions'] == 1
    assert summary['tasks_pending_comparison'] == 3
    assert summary['accuracy'] == summary['population_recall'] == 'not_measured'
    assert summary['adjudication_status'] == 'NOT_PERFORMED'
    for dimension in DIMENSIONS:
        result = summary['dimensional_reviewer_assessments'][dimension]
        assert result['denominator'] == 1 and result['counts']['UNRESOLVED'] == 1
        assert result['counts']['REVISE'] == result['counts']['SUPPORTED'] == 0


@pytest.mark.parametrize('change,code', [
    ('source_review_sha256', 'COMPARISON_PHASE_BINDING'),
    ('reveal_sha256', 'COMPARISON_PHASE_BINDING'),
    ('missing_dimension', 'ALL_DIMENSIONS_REQUIRE'),
    ('duplicate_dimension', 'UNKNOWN_OR_DUPLICATE_DIMENSION'),
    ('foreign_event', 'COMPARISON_REFERENCE_OUTSIDE_TASK'),
    ('revision_without_citation', 'REVISION_REQUIRES_ORIGINAL_SOURCE_CITATION'),
])
def test_CE_IV004_comparison_binding_and_explicit_dimensions_fail_closed(tmp_path, change, code):
    batch = batch_fixture(tmp_path)
    path, comparison = comparison_fixture(batch, tmp_path)
    if change in ('source_review_sha256', 'reveal_sha256'): comparison[change] = 'wrong'
    elif change == 'missing_dimension': comparison['dimensions'].pop()
    elif change == 'duplicate_dimension': comparison['dimensions'].append(deepcopy(comparison['dimensions'][0]))
    elif change == 'foreign_event': comparison['dimensions'][0]['event_ids'] = ['FOREIGN']
    else: comparison['dimensions'][0]['assessment'] = 'REVISE'
    replace(path, comparison)
    with pytest.raises(ValidationError, match=code): compare(batch, path)


def test_CE_IV004_revision_with_exact_original_citation_is_a_report_not_adjudication(tmp_path):
    batch = batch_fixture(tmp_path)
    path, comparison = comparison_fixture(batch, tmp_path)
    frozen = read(batch / 'source_reviews' / 'IV-0001' / 'person-A' / 'frozen.json')
    comparison['dimensions'][0].update(assessment='REVISE',
        citations=frozen['submitted']['findings'][0]['citations'], finding_ids=['F1'])
    replace(path, comparison)
    compare(batch, path)
    summary = summarize(batch)
    assert summary['dimensional_reviewer_assessments']['event_detection']['counts']['REVISE'] == 1
    assert summary['adjudication_status'] == 'NOT_PERFORMED' and not summary['clinical_reference_created']


def test_CE_IV004_supported_assessment_requires_citation_or_frozen_original_finding(tmp_path):
    batch = batch_fixture(tmp_path)
    path, comparison = comparison_fixture(batch, tmp_path)
    comparison['dimensions'][0]['assessment'] = 'SUPPORTED'
    replace(path, comparison)
    with pytest.raises(ValidationError, match='SUPPORTED_ASSESSMENT_REQUIRES_SOURCE_LINK'):
        compare(batch, path)
    comparison['dimensions'][0]['finding_ids'] = ['F1']
    replace(path, comparison)
    compare(batch, path)
    result = summarize(batch)['dimensional_reviewer_assessments']['event_detection']
    assert result['counts']['SUPPORTED'] == 1 and result['denominator'] == 1


def test_CE_IV005_all_artifacts_are_exclusive_and_old_input_bytes_unchanged(tmp_path):
    release = fixture_release(tmp_path, old_inventory=True)
    original = {path: path.read_bytes() for path in tmp_path.rglob('*.json')}
    batch = tmp_path / 'private' / 'batch'
    prepare(release, batch, per_stratum=1)
    assert all(path.read_bytes() == data for path, data in original.items())
    manifest = read(batch / 'researcher_only' / 'manifest.json')
    for record in manifest['source_snapshots']:
        assert sha_bytes((batch / record['snapshot_path']).read_bytes()) == record['sha256']
    with pytest.raises(ValidationError, match='OUTPUT_DIRECTORY_ALREADY_EXISTS'): prepare(release, batch)
    path, _ = completed_source_review(batch, tmp_path)
    submit(batch, path)
    with pytest.raises(ValidationError, match='SUBMISSION_ALREADY_FROZEN'): submit(batch, path)
    reveal(batch, 'IV-0001', 'person-A')
    with pytest.raises(ValidationError, match='REVEAL_ALREADY_EXISTS'): reveal(batch, 'IV-0001', 'person-A')


def test_CE_IV005_real_adapter_rejects_nonprivate_and_symlink_escape_outputs(tmp_path):
    release = fixture_release(tmp_path)
    with pytest.raises(ValidationError, match='REQUIRES_PRIVATE_OUTPUT'):
        prepare(release, tmp_path / 'public' / 'batch')
    private = tmp_path / 'private'
    public = tmp_path / 'public'
    private.mkdir(); public.mkdir()
    (private / 'escape').symlink_to(public, target_is_directory=True)
    with pytest.raises(ValidationError, match='REQUIRES_PRIVATE_OUTPUT'):
        prepare(release, private / 'escape' / 'batch')
    assert not (public / 'batch').exists()


def test_CE_IV005_frozen_source_review_bytes_tamper_blocks_reveal(tmp_path):
    batch = batch_fixture(tmp_path)
    path, _ = completed_source_review(batch, tmp_path)
    submit(batch, path)
    submitted = batch / 'source_reviews' / 'IV-0001' / 'person-A' / 'submitted_bytes.json'
    submitted.write_bytes(submitted.read_bytes() + b' ')
    with pytest.raises(ValidationError, match='FROZEN_SOURCE_REVIEW_BYTES_CHANGED'):
        reveal(batch, 'IV-0001', 'person-A')


def test_CE_IV005_duplicate_json_keys_path_traversal_and_extra_answers_are_rejected(tmp_path):
    batch = batch_fixture(tmp_path)
    path = tmp_path / 'duplicate.json'
    path.write_text('{"task_id":"IV-0001","task_id":"IV-0002"}')
    with pytest.raises(ValidationError, match='DUPLICATE_JSON_KEY'): submit(batch, path)
    with pytest.raises(ValidationError, match='INVALID_PSEUDONYMOUS_IDENTIFIER'):
        reveal(batch, '../outside', 'person-A')
    path, review = completed_source_review(batch, tmp_path)
    review['clinical_mode'] = 'not allowed'
    replace(path, review)
    with pytest.raises(ValidationError, match='UNKNOWN_FIELD'): submit(batch, path)


def test_CE_IV005_preparation_and_recovery_create_no_network_connections(tmp_path, monkeypatch):
    release = fixture_release(tmp_path)
    def blocked(*args, **kwargs):
        raise AssertionError('Network connection attempted')
    monkeypatch.setattr(socket, 'socket', blocked)
    batch = tmp_path / 'private' / 'batch'
    prepare(release, batch, per_stratum=1)
    path, _ = comparison_fixture(batch, tmp_path)
    compare(batch, path)
    assert summarize(batch)['model_api_calls'] == 0


def test_CE_IV005_summary_rechecks_comparison_bytes_and_revealed_material(tmp_path):
    batch = batch_fixture(tmp_path)
    path, _ = comparison_fixture(batch, tmp_path)
    compare(batch, path)
    frozen_bytes = batch / 'comparisons' / 'IV-0001' / 'person-A' / 'submitted_bytes.json'
    frozen_bytes.write_bytes(frozen_bytes.read_bytes() + b' ')
    with pytest.raises(ValidationError, match='FROZEN_COMPARISON_BYTES_CHANGED'): summarize(batch)
