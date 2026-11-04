"""Source-first research review with append-only, hash-bound local artifacts.

No network or model client is used. Identity and independence are declarations
by actual people; hashes cannot authenticate either. This is not a doctor packet.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import fcntl
from functools import wraps
from pathlib import Path

from .adapter import load_release, population, scope_sources
from .core import (
    ASSESSMENTS, DIMENSIONS, VERSION, ValidationError, bound, digest, fields,
    identifier, now, origin_status, private_output, read, require, sha_bytes, snapshot, string,
    validate_citations, validate_sources, verify_bound, write,
)


def _serialized(function):
    """Serialize this batch's source-freeze and reveal boundary on local POSIX."""
    @wraps(function)
    def run(batch, *args, **kwargs):
        batch = Path(batch).resolve()
        require(private_output(batch), 'RESEARCH_MATERIAL_REQUIRES_PRIVATE_OUTPUT')
        require((batch / 'researcher_only' / 'manifest.json').is_file(), 'PREPARED_BATCH_REQUIRED')
        with (batch / '.operation.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return function(batch, *args, **kwargs)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    return run


def _input_hashes(cases):
    return {str(path): sha_bytes(path.read_bytes())
            for case in cases for path in case['paths'].values()}


def prepare(release, output, *, seed=20261029, per_stratum=2, context_days=0):
    """Prepare an explicitly developmental batch from immutable frozen outputs."""
    output, release = Path(output).resolve(), Path(release).resolve()
    require(private_output(output), 'RESEARCH_MATERIAL_REQUIRES_PRIVATE_OUTPUT')
    require(not output.exists(), 'OUTPUT_DIRECTORY_ALREADY_EXISTS')
    require(not output.is_relative_to(release), 'OUTPUT_MUST_NOT_BE_WITHIN_FROZEN_RELEASE')
    require(type(seed) is int and type(per_stratum) is int and per_stratum > 0, 'INVALID_SAMPLING_SETTINGS')
    require(type(context_days) is int and 0 <= context_days <= 365, 'INVALID_CONTEXT_DAYS')
    cases = load_release(release)
    before = _input_hashes(cases)
    strata, by_case = population(cases)
    selected, seen, sampling = [], set(), []
    for stratum, items in strata.items():
        items = sorted(items, key=lambda item: digest([seed, stratum, item]))
        eligible = []
        for item in items:
            key = (item['case_id'], tuple(sorted(item['source_ids'])))
            if key not in seen:
                eligible.append((key, item))
        chosen = eligible[:per_stratum]
        for key, item in chosen:
            seen.add(key)
            selected.append((stratum, item))
        sampling.append(dict(stratum=stratum, population_size=len(items),
                             eligible_after_earlier_stratum_deduplication=len(eligible),
                             requested=per_stratum, selected=len(chosen),
                             gap=None if len(chosen) == per_stratum else 'INSUFFICIENT_DISTINCT_ELIGIBLE_UNITS'))
    require(selected, 'NO_ELIGIBLE_RESEARCH_TASKS')
    # Task numbers cannot encode the coordinator's stratum order. This second
    # ordering deliberately uses only the seed and unit/source identity.
    selected.sort(key=lambda pair: digest([seed, 'blind-task-order', pair[1]['case_id'],
                                           sorted(pair[1]['source_ids'])]))
    # All source and model files are snapshotted byte-for-byte before tasks are
    # created. Those coordinator files must never accompany Phase A delivery.
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    snapshots = []
    for case in cases:
        for name, path in sorted(case['paths'].items()):
            target = output / 'researcher_only' / 'input_snapshots' / case['case_id'] / name
            record = snapshot(path, target)
            require(record['sha256'] == before[str(path)], 'INPUT_CHANGED_DURING_PREPARATION')
            snapshots.append(dict(case_id=case['case_id'], input_name=name,
                                  original_path=str(path), snapshot_path=str(target.relative_to(output)), **record))
    records = []
    for index, (stratum, item) in enumerate(selected, 1):
        case, task_id = by_case[item['case_id']], f'IV-{index:04d}'
        documents, scope = scope_sources(case, item, context_days)
        source_bundle = bound(dict(version=VERSION, task_id=task_id, scope=scope,
            anchor_source_ids=sorted(item['source_ids']), documents=documents), 'source_bundle_sha256')
        task = bound(dict(version=VERSION, task_id=task_id, task_version='1',
            source_bundle_sha256=source_bundle['source_bundle_sha256'], scope=scope,
            purpose='Independent source reconstruction within the declared scope; research validation only.',
            instructions=[
                'Read every supplied complete source independently before consulting any model output.',
                'Cite exact source text with Unicode codepoint offsets and its source hash; preserve uncertainty.',
                'Record patient-specific clinical-team management judgments, event grouping, prior comparison evidence, information evolution and time evidence as supported by the text.',
                'The supplied source range may include later documents; never use later outcomes to rewrite earlier information or claim earlier clinical availability.',
                'Request more sources when needed. Scope completion is not full-patient recall or a Clinical Reference.',
            ]), 'task_sha256')
        source_template = dict(version=VERSION, task_id=task_id, task_sha256=task['task_sha256'],
            source_bundle_sha256=source_bundle['source_bundle_sha256'], origin='ai_preparation',
            status='PENDING', reviewer_id='', independence_attested=False,
            complete_within_declared_scope=False, read_source_ids=[],
            no_target_management_judgment_found=None, findings=[], unresolved_issues=[])
        dest = output / 'phase_a' / task_id
        write(dest / 'task.json', task)
        write(dest / 'sources.json', source_bundle)
        write(dest / 'source_review_template.json', source_template)
        markdown = ['# 独立原文重建任务 ' + task_id, '',
                    '本材料仅用于指定原文范围的研究验证，可以含后续文书；不能用于临床动作盲评。',
                    '先独立阅读全文并封存原文重建，再由协调者另行开放模型提案。', '',
                    scope['description'], scope['limitation'], '']
        for source in documents:
            markdown += ['## ' + source['source_id'],
                         '原文 SHA256: ' + source['source_sha256'],
                         '以下正文保持原样；Unicode codepoint 从 0 开始、右端不含。', '', source['text'], '']
        markdown_path = dest / '原文材料.md'
        with markdown_path.open('x', encoding='utf-8') as stream:
            stream.write('\n'.join(markdown))
        markdown_path.chmod(0o600)
        results = case['discoveries'].get(item['unit_id']) if item['unit_id'] else None
        event_ids = {event['event_id'] for event in case['events'] if event['unit_id'] == item['unit_id']}
        # Keep each producer's statement separate. Do not turn the LLM's
        # classification or the program's status into a confirmed clinical fact.
        proposals = bound(dict(version=VERSION, task_id=task_id,
            task_sha256=task['task_sha256'], source_bundle_sha256=source_bundle['source_bundle_sha256'],
            discovery_stage_llm_output=deepcopy(results),
            reviewed_events_with_llm_and_program_fields=[deepcopy(event) for event in case['events'] if event['event_id'] in event_ids],
            program_relations=[deepcopy(relation) for relation in case['relations'] if
                relation.get('source_event_id') in event_ids or relation.get('target_event_id') in event_ids],
            program_candidate_episodes=[deepcopy(episode) for episode in case['episodes'] if
                event_ids.intersection(episode.get('candidate_ids', []))],
            proposal_absence_meaning='No recorded discovery proposal for this source means unmeasured, not a negative clinical judgment.',
            independent_review_scope_source_ids=scope['source_ids'],
            limitation='Model context and references may extend beyond the independent source scope; outside-scope assertions require UNRESOLVED or OUT_OF_SCOPE.'), 'proposals_sha256')
        write(output / 'researcher_only' / 'proposals' / (task_id + '.json'), proposals)
        memberships = [label for label, items in strata.items() if any(
            candidate['case_id'] == item['case_id'] and sorted(candidate['source_ids']) == sorted(item['source_ids'])
            for candidate in items)]
        records.append(dict(task_id=task_id, task_sha256=task['task_sha256'],
            source_bundle_sha256=source_bundle['source_bundle_sha256'], proposals_sha256=proposals['proposals_sha256'],
            selected_stratum=stratum, eligible_strata=memberships, **deepcopy(item),
            source_document_count=len(documents), source_characters=sum(len(d['text']) for d in documents)))
    code = Path(__file__).parent
    code_snapshot = {}
    for path in sorted(code.glob('*.py')):
        target = output / 'researcher_only' / 'code_snapshot' / path.name
        code_snapshot[path.name] = snapshot(path, target)['sha256']
    after = _input_hashes(cases)
    require(before == after, 'FROZEN_INPUT_CHANGED_DURING_PREPARATION')
    manifest = bound(dict(version=VERSION, created_at_utc=now(), release_directory=str(release),
        development_only=True, independent_human_reviews_completed=0, clinical_reference_created=False,
        doctor_packets_created=False, model_api_calls=0, sampling_seed=seed, per_stratum=per_stratum,
        context_days=context_days, sampling_order=list(strata), sampling=sampling,
        sampling_design='Deterministic hash-ranked units within declared development/challenge strata; overlap removed in declared stratum order; no population accuracy estimate.',
        tasks=records, source_snapshots=snapshots, original_input_sha256=before,
        input_hashes_unchanged=True, code_snapshot_sha256=code_snapshot,
        case_routing=[dict(case_id=case['case_id'], inventory_origin=case['inventory_origin'],
                          **case['routing_provenance']) for case in cases],
        unmeasured_request_failures=[dict(case_id=case['case_id'], failures=case['failures']) for case in cases if case['failures']],
        independence_limitation='File layout and hashes bind declarations; they do not authenticate reviewer identity, prove actual blinding, or prevent a coordinator from opening other local files.'), 'manifest_sha256')
    write(output / 'researcher_only' / 'manifest.json', manifest)
    write(output / 'operations' / 'prepare.json', bound(dict(operation='prepare', at_utc=now(),
        manifest_sha256=manifest['manifest_sha256'], input_hashes_unchanged=True,
        model_api_calls=0, task_count=len(records)), 'operation_sha256'))
    return dict(status='PREPARED_AWAITING_INDEPENDENT_HUMANS', task_count=len(records),
                sampling=sampling, independent_human_reviews_completed=0,
                clinical_accuracy='not_measured', model_api_calls=0,
                manifest_sha256=manifest['manifest_sha256'])


def _load_task(batch, task_id):
    batch, task_id = Path(batch).resolve(), identifier(task_id)
    require(private_output(batch), 'RESEARCH_MATERIAL_REQUIRES_PRIVATE_OUTPUT')
    manifest = verify_bound(read(batch / 'researcher_only' / 'manifest.json'), 'manifest_sha256')
    matches = [record for record in manifest['tasks'] if record['task_id'] == task_id]
    require(len(matches) == 1, 'TASK_NOT_IN_BATCH')
    record = matches[0]
    task = verify_bound(read(batch / 'phase_a' / task_id / 'task.json'), 'task_sha256')
    bundle = read(batch / 'phase_a' / task_id / 'sources.json')
    sources = validate_sources(bundle)
    proposals = verify_bound(read(batch / 'researcher_only' / 'proposals' / (task_id + '.json')), 'proposals_sha256')
    require(task['task_sha256'] == record['task_sha256'] and
            task['source_bundle_sha256'] == bundle['source_bundle_sha256'] == record['source_bundle_sha256'] and
            proposals['proposals_sha256'] == record['proposals_sha256'] and
            proposals['task_sha256'] == task['task_sha256'] and
            proposals['source_bundle_sha256'] == bundle['source_bundle_sha256'], 'TASK_CONTENT_BINDING_MISMATCH')
    return batch, task, bundle, sources, proposals


def _review_binding(review, task, bundle):
    require(review.get('version') == VERSION and review.get('task_id') == task['task_id'] and
            review.get('task_sha256') == task['task_sha256'] and
            review.get('source_bundle_sha256') == bundle['source_bundle_sha256'], 'SUBMISSION_TASK_BINDING_MISMATCH')
    origin_status(review)


def _validate_source_review(review, task, bundle, sources):
    fields(review, {'version', 'task_id', 'task_sha256', 'source_bundle_sha256', 'origin', 'status',
                   'reviewer_id', 'independence_attested', 'complete_within_declared_scope', 'read_source_ids',
                   'no_target_management_judgment_found', 'findings', 'unresolved_issues'},
           {'version', 'task_id', 'task_sha256', 'source_bundle_sha256', 'origin', 'status', 'reviewer_id',
            'independence_attested', 'complete_within_declared_scope', 'read_source_ids',
            'no_target_management_judgment_found', 'findings', 'unresolved_issues'})
    _review_binding(review, task, bundle)
    require(type(review['complete_within_declared_scope']) is bool, 'SCOPE_COMPLETENESS_BOOLEAN_REQUIRED')
    require(isinstance(review['read_source_ids'], list) and all(isinstance(x, str) for x in review['read_source_ids']) and
            len(review['read_source_ids']) == len(set(review['read_source_ids'])) and
            not set(review['read_source_ids']) - sources.keys(), 'READ_SOURCE_SCOPE_MISMATCH')
    require(isinstance(review['findings'], list) and isinstance(review['unresolved_issues'], list), 'REVIEW_ARRAYS_REQUIRED')
    seen = set()
    for finding in review['findings']:
        fields(finding, {'finding_id', 'kind', 'description', 'citations', 'event_group_id'},
               {'finding_id', 'kind', 'description', 'citations'})
        identifier(finding['finding_id'])
        require(finding['finding_id'] not in seen, 'DUPLICATE_FINDING_ID')
        seen.add(finding['finding_id'])
        require(finding['kind'] in {'management_judgment', 'evidence', 'temporal_boundary', 'uncertain'}, 'INVALID_FINDING_KIND')
        string(finding['description'])
        require(finding['citations'], 'FINDING_REQUIRES_ORIGINAL_SOURCE_CITATION')
        validate_citations(finding['citations'], sources)
        if 'event_group_id' in finding:
            identifier(finding['event_group_id'])
    for issue in review['unresolved_issues']:
        fields(issue, {'issue_id', 'description', 'requested_source_ids'}, {'issue_id', 'description', 'requested_source_ids'})
        identifier(issue['issue_id'])
        string(issue['description'])
        require(isinstance(issue['requested_source_ids'], list) and all(isinstance(s, str) and s for s in issue['requested_source_ids']),
                'SOURCE_REQUEST_ARRAY_REQUIRED')
    no_target = review['no_target_management_judgment_found']
    require(no_target is None or type(no_target) is bool, 'NO_TARGET_BOOLEAN_REQUIRED')
    if no_target is True:
        require(not any(item['kind'] == 'management_judgment' for item in review['findings']), 'NO_TARGET_CONFLICTS_WITH_MANAGEMENT_FINDING')
    if review['status'] == 'INDEPENDENT_COMPLETE':
        require(review['complete_within_declared_scope'] and set(review['read_source_ids']) == sources.keys(),
                'COMPLETE_REVIEW_REQUIRES_ALL_DECLARED_SOURCES')
        if no_target is None:
            require(any(item['kind'] == 'uncertain' for item in review['findings']) or review['unresolved_issues'],
                    'UNRESOLVED_TARGET_REQUIRES_EXPLICIT_REASON')
        elif no_target is False:
            require(any(item['kind'] == 'management_judgment' for item in review['findings']),
                    'TARGET_FOUND_REQUIRES_ORIGINAL_SOURCE_FINDING')


def _save_submission(batch, review, source_path, kind):
    complete = review['status'] == 'INDEPENDENT_COMPLETE'
    folder = kind if complete else 'drafts/' + kind
    dest = batch / folder / review['task_id'] / review['reviewer_id']
    require(not dest.exists(), 'SUBMISSION_ALREADY_FROZEN')
    dest.mkdir(parents=True, exist_ok=False, mode=0o700)
    raw = snapshot(source_path, dest / 'submitted_bytes.json')
    # Parse exact snapshotted bytes, closing the check/copy race for submissions.
    require(read(dest / 'submitted_bytes.json') == review, 'SUBMISSION_CHANGED_DURING_FREEZE')
    frozen = bound(dict(version=VERSION, kind=kind, submitted=review,
        original_submission_sha256=raw['sha256'], submitted_at_utc=now(),
        declared_origin=review['origin'], independently_complete=complete,
        clinical_reference=False, identity_authenticated=False), 'submission_sha256')
    write(dest / 'frozen.json', frozen)
    write(dest / 'operation.json', bound(dict(operation=kind, at_utc=now(),
        submission_sha256=frozen['submission_sha256'], model_api_calls=0), 'operation_sha256'))
    return dict(status=review['status'], submission_sha256=frozen['submission_sha256'],
                independently_complete=complete, clinical_reference=False)


def _check_prior_local_exposure(batch, task_id, reviewer_id):
    manifest = verify_bound(read(batch / 'researcher_only' / 'manifest.json'), 'manifest_sha256')
    cases = {record['task_id']: record['case_id'] for record in manifest['tasks']}
    for path in (batch / 'researcher_only' / 'revealed').glob('*/' + reviewer_id + '/comparison_material.json'):
        revealed = verify_bound(read(path), 'reveal_sha256')
        require(revealed['task_id'] in cases and revealed['reviewer_id'] == reviewer_id,
                'LOCAL_EXPOSURE_RECORD_BINDING_MISMATCH')
        require(cases[revealed['task_id']] != cases[task_id],
                'PRIOR_LOCAL_MODEL_EXPOSURE_BLOCKS_INDEPENDENT_SOURCE_REVIEW')


@_serialized
def submit(batch, source_review):
    """Verify and freeze a person's source-first reconstruction, or a pending draft."""
    review = read(source_review)
    require(isinstance(review, dict), 'OBJECT_REQUIRED')
    batch, task, bundle, sources, _ = _load_task(batch, review.get('task_id'))
    _validate_source_review(review, task, bundle, sources)
    if review['status'] == 'INDEPENDENT_COMPLETE':
        # Local known exposure only. This cannot authenticate access outside
        # this batch or prove a person did not open coordinator files directly.
        _check_prior_local_exposure(batch, task['task_id'], review['reviewer_id'])
    return _save_submission(batch, review, source_review, 'source_reviews')


def _frozen_source_review(batch, task, bundle, sources, reviewer_id):
    reviewer_id = identifier(reviewer_id)
    directory = batch / 'source_reviews' / task['task_id'] / reviewer_id
    require((directory / 'frozen.json').is_file(), 'INDEPENDENT_SOURCE_REVIEW_REQUIRED_BEFORE_REVEAL')
    frozen = verify_bound(read(directory / 'frozen.json'), 'submission_sha256')
    review = frozen['submitted']
    _validate_source_review(review, task, bundle, sources)
    require(review['status'] == 'INDEPENDENT_COMPLETE' and review['origin'] == 'independent_human' and
            frozen['independently_complete'] is True and review['reviewer_id'] == reviewer_id,
            'INDEPENDENT_SOURCE_REVIEW_REQUIRED_BEFORE_REVEAL')
    require(sha_bytes((directory / 'submitted_bytes.json').read_bytes()) == frozen['original_submission_sha256'] and
            read(directory / 'submitted_bytes.json') == review, 'FROZEN_SOURCE_REVIEW_BYTES_CHANGED')
    return frozen


@_serialized
def reveal(batch, task_id, reviewer_id):
    """Create a separate researcher comparison only after the source review freeze."""
    batch, task, bundle, sources, proposals = _load_task(batch, task_id)
    frozen = _frozen_source_review(batch, task, bundle, sources, reviewer_id)
    dest = batch / 'researcher_only' / 'revealed' / task_id / reviewer_id
    require(not dest.exists(), 'REVEAL_ALREADY_EXISTS')
    result = bound(dict(version=VERSION, task_id=task_id, task_sha256=task['task_sha256'],
        source_bundle_sha256=bundle['source_bundle_sha256'], source_review_sha256=frozen['submission_sha256'],
        reviewer_id=reviewer_id, revealed_at_utc=now(), proposals=proposals,
        warning='Researcher comparison with model proposals and possibly later/out-of-scope context. Never a doctor blind-review packet or Clinical Reference.'), 'reveal_sha256')
    template = dict(version=VERSION, task_id=task_id, task_sha256=task['task_sha256'],
        source_bundle_sha256=bundle['source_bundle_sha256'], source_review_sha256=frozen['submission_sha256'],
        reveal_sha256=result['reveal_sha256'], reviewer_id=reviewer_id, origin='ai_preparation',
        status='PENDING', independence_attested=False,
        dimensions=[dict(dimension=dimension, assessment=None, rationale='', citations=[],
                         event_ids=[], finding_ids=[]) for dimension in DIMENSIONS],
        unresolved_issues=[])
    write(dest / 'comparison_material.json', result)
    write(dest / 'comparison_template.json', template)
    write(dest / 'operation.json', bound(dict(operation='reveal', at_utc=now(),
        source_review_sha256=frozen['submission_sha256'], reveal_sha256=result['reveal_sha256'],
        model_api_calls=0), 'operation_sha256'))
    return dict(status='RESEARCHER_COMPARISON_REVEALED', reveal_sha256=result['reveal_sha256'],
                clinical_reference=False, doctor_packet=False)


def _validate_comparison(review, task, bundle, sources, proposals, frozen, revealed):
    fields(review, {'version', 'task_id', 'task_sha256', 'source_bundle_sha256', 'origin', 'status',
                   'reviewer_id', 'independence_attested', 'source_review_sha256', 'reveal_sha256',
                   'dimensions', 'unresolved_issues'},
           {'version', 'task_id', 'task_sha256', 'source_bundle_sha256', 'origin', 'status', 'reviewer_id',
            'independence_attested', 'source_review_sha256', 'reveal_sha256', 'dimensions', 'unresolved_issues'})
    _review_binding(review, task, bundle)
    require(review['source_review_sha256'] == frozen['submission_sha256'] and
            review['reveal_sha256'] == revealed['reveal_sha256'] and
            revealed['reviewer_id'] == review['reviewer_id'] and
            revealed['proposals']['proposals_sha256'] == proposals['proposals_sha256'], 'COMPARISON_PHASE_BINDING_MISMATCH')
    require(isinstance(review['dimensions'], list), 'DIMENSION_ARRAY_REQUIRED')
    seen = set()
    event_ids = {event['event_id'] for event in proposals['reviewed_events_with_llm_and_program_fields']}
    discovery = proposals['discovery_stage_llm_output'] or {}
    event_ids.update(event['event_id'] for event in discovery.get('events', []))
    findings = {item['finding_id'] for item in frozen['submitted']['findings']}
    for dimension in review['dimensions']:
        fields(dimension, {'dimension', 'assessment', 'rationale', 'citations', 'event_ids', 'finding_ids'},
               {'dimension', 'assessment', 'rationale', 'citations', 'event_ids', 'finding_ids'})
        require(dimension['dimension'] in DIMENSIONS and dimension['dimension'] not in seen, 'UNKNOWN_OR_DUPLICATE_DIMENSION')
        seen.add(dimension['dimension'])
        require(dimension['assessment'] in ASSESSMENTS, 'EXPLICIT_DIMENSION_ASSESSMENT_REQUIRED')
        string(dimension['rationale'])
        validate_citations(dimension['citations'], sources)
        for name, allowed in [('event_ids', event_ids), ('finding_ids', findings)]:
            values = dimension[name]
            require(isinstance(values, list) and all(isinstance(value, str) for value in values) and
                    len(values) == len(set(values)) and not set(values) - allowed, 'COMPARISON_REFERENCE_OUTSIDE_TASK')
        if dimension['assessment'] == 'REVISE':
            require(dimension['citations'], 'REVISION_REQUIRES_ORIGINAL_SOURCE_CITATION')
        if dimension['assessment'] == 'SUPPORTED':
            require(dimension['citations'] or dimension['finding_ids'], 'SUPPORTED_ASSESSMENT_REQUIRES_SOURCE_LINK')
    require(set(DIMENSIONS) == seen, 'ALL_DIMENSIONS_REQUIRE_EXPLICIT_ASSESSMENT')
    require(isinstance(review['unresolved_issues'], list) and
            all(isinstance(issue, str) and issue.strip() for issue in review['unresolved_issues']), 'UNRESOLVED_ISSUE_TEXT_REQUIRED')


@_serialized
def compare(batch, comparison):
    """Freeze a human comparison; unresolved remains unresolved, never adjudicated."""
    review = read(comparison)
    require(isinstance(review, dict), 'OBJECT_REQUIRED')
    batch, task, bundle, sources, proposals = _load_task(batch, review.get('task_id'))
    reviewer_id = identifier(review.get('reviewer_id'))
    frozen = _frozen_source_review(batch, task, bundle, sources, reviewer_id)
    reveal_path = batch / 'researcher_only' / 'revealed' / task['task_id'] / reviewer_id / 'comparison_material.json'
    require(reveal_path.is_file(), 'REVEAL_REQUIRED_BEFORE_COMPARISON')
    revealed = verify_bound(read(reveal_path), 'reveal_sha256')
    _validate_comparison(review, task, bundle, sources, proposals, frozen, revealed)
    return _save_submission(batch, review, comparison, 'comparisons')


@_serialized
def summarize(batch):
    """Report human-review denominators; no clinical performance or auto-gold."""
    batch = Path(batch).resolve()
    manifest = verify_bound(read(batch / 'researcher_only' / 'manifest.json'), 'manifest_sha256')
    source_count = comparison_count = 0
    source_target_status = Counter()
    source_tasks, comparison_tasks = set(), set()
    dimension_counts = {dimension: Counter() for dimension in DIMENSIONS}
    for record in manifest['tasks']:
        task_id = record['task_id']
        _, task, bundle, sources, proposals = _load_task(batch, task_id)
        for path in sorted((batch / 'source_reviews' / task_id).glob('*/frozen.json')):
            frozen = _frozen_source_review(batch, task, bundle, sources, path.parent.name)
            source_count += 1
            target_status = frozen['submitted']['no_target_management_judgment_found']
            source_target_status['UNRESOLVED' if target_status is None else
                                 'NO_TARGET_FOUND_IN_SCOPE' if target_status else 'TARGET_FOUND_IN_SCOPE'] += 1
            source_tasks.add(task_id)
        for path in sorted((batch / 'comparisons' / task_id).glob('*/frozen.json')):
            value = verify_bound(read(path), 'submission_sha256')
            review = value['submitted']
            require(review['status'] == 'INDEPENDENT_COMPLETE' and review['origin'] == 'independent_human' and
                    value['independently_complete'] is True and review['reviewer_id'] == path.parent.name,
                    'NONINDEPENDENT_COMPLETION_IN_SUMMARY')
            source_review = _frozen_source_review(batch, task, bundle, sources, path.parent.name)
            revealed = verify_bound(read(batch / 'researcher_only' / 'revealed' / task_id / path.parent.name / 'comparison_material.json'), 'reveal_sha256')
            _validate_comparison(review, task, bundle, sources, proposals, source_review, revealed)
            require(sha_bytes((path.parent / 'submitted_bytes.json').read_bytes()) == value['original_submission_sha256'] and
                    read(path.parent / 'submitted_bytes.json') == review, 'FROZEN_COMPARISON_BYTES_CHANGED')
            comparison_count += 1
            comparison_tasks.add(task_id)
            for dimension in review['dimensions']:
                dimension_counts[dimension['dimension']][dimension['assessment']] += 1
    count = len(manifest['tasks'])
    return dict(version=VERSION, manifest_sha256=manifest['manifest_sha256'],
        prepared_tasks=count, independent_source_review_submissions=source_count,
        tasks_with_independent_source_review=len(source_tasks),
        source_target_assessment_counts={key: source_target_status[key] for key in
            ('TARGET_FOUND_IN_SCOPE', 'NO_TARGET_FOUND_IN_SCOPE', 'UNRESOLVED')},
        independent_comparison_submissions=comparison_count,
        tasks_with_independent_comparison=len(comparison_tasks),
        tasks_pending_source_review=count - len(source_tasks),
        tasks_pending_comparison=count - len(comparison_tasks),
        draft_submissions=sum(1 for _ in (batch / 'drafts').glob('*/*/*/frozen.json')),
        dimensional_reviewer_assessments={dimension: dict(denominator=comparison_count,
            counts={status: counts.get(status, 0) for status in sorted(ASSESSMENTS)},
            unresolved_or_out_of_scope=counts['UNRESOLVED'] + counts['OUT_OF_SCOPE'])
            for dimension, counts in dimension_counts.items()},
        accuracy='not_measured', population_recall='not_measured', adjudication_status='NOT_PERFORMED',
        clinical_reference_created=False, doctor_packets_created=False, model_api_calls=0,
        interpretation='Counts describe declared independent human reviews within sampled source scopes. Unresolved and out-of-scope dimensions are not negative labels; no algorithm accuracy, full-patient recall, or adjudicated truth is inferred.')
