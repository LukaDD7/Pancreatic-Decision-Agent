"""Build versioned automated material opinions for the researcher workbench.

The original assignment and human audit are never changed. This bridge stores
an independent chain and optional human comparison, not an artificial PASS.
"""
from copy import deepcopy
import json
from pathlib import Path

from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError, canonical_sha256, text_sha256
from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import validate_review_chain
from scripts.cohort_construction.p2_checkpoint_materials.review import create_review_draft
from scripts.cohort_construction.p2_checkpoint_material_review_app.build import (
    build_assignment, build_researcher_html, canonical_json, parse_json_unique, require_private_output,
    validate_assignment,
)


def derive_revision_preparation(chain, original_preparation):
    """Derive a pending receipt from exact original preparation plus replay.

    Old citations, uncertainty and source routing survive verbatim in the
    embedded original audit. Material scope changes never edit graph facts.
    """
    replay = validate_review_chain(chain)
    original = deepcopy(original_preparation)
    if (original.get('compiled_sha256') != chain['initial_compiled']['compiled_sha256']
            or original.get('manifest_sha256') != canonical_sha256(chain['initial_manifest'])):
        raise ContractError('REVISION_PREPARATION_ORIGINAL_BINDING_MISMATCH')
    if 'audit_sha256' in original and canonical_sha256({k:v for k,v in original.items()
            if k != 'audit_sha256'}) != original['audit_sha256']:
        raise ContractError('REVISION_PREPARATION_ORIGINAL_HASH_MISMATCH')
    result = deepcopy(original)
    included = {s['source_id'] for s in chain['manifest']['sources']}
    original_routes = {s['source_id']: s for s in original.get('source_routing', [])}
    registry = {s['source_id']: s for s in chain['source_registry']}
    routing = []
    for sid in sorted(set(original_routes) | set(registry)):
        route = deepcopy(original_routes.get(sid, dict(source_id=sid,
            in_event_source_ids=False, in_actual_model_evidence=False)))
        route['original_disposition'] = route.get('disposition', 'SUPPLIED_CANDIDATE_SOURCE')
        route['disposition'] = 'INCLUDED_FULL_ORIGINAL_TEXT' if sid in included else (
            'EXCLUDED_OUTSIDE_EVENT_SCOPE' if sid in registry else route['original_disposition'])
        route['material_scope_only_not_graph_fact'] = True
        if sid in registry:
            route.update(source_version=registry[sid]['source_version'], source_sha256=registry[sid]['sha256'],
                         original_registry_scope=registry[sid]['origin_scope'])
        routing.append(route)
    counts, chars = {}, {}
    decisions = []
    for source in chain['compiled']['sources']:
        for segment in source['segments']:
            disposition = segment['disposition']
            counts[disposition] = counts.get(disposition, 0) + 1
            chars[disposition] = chars.get(disposition, 0) + segment['end'] - segment['start']
            decisions.append(dict(segment_id='MRP-' + canonical_sha256([source['source_id'],segment['start'],segment['end'],disposition])[:24], source_id=source['source_id'],
                start=segment['start'], end=segment['end'], disposition=disposition,
                reason='AUTOMATED_MATERIAL_REVISION_PENDING_INDEPENDENT_REVIEW',
                auto_review_chain_sha256=replay['chain_sha256'], hint_ids=[]))
    result.update(rule_version='event-material-revision-preparation-0.2.0',
        preparation_status='REVISED_RESEARCHER_REVIEW_REQUIRED',
        compiled_sha256=chain['compiled']['compiled_sha256'],
        manifest_id=chain['manifest']['manifest_id'], manifest_sha256=canonical_sha256(chain['manifest']),
        source_hashes={s['source_id']:s['sha256'] for s in chain['manifest']['sources']},
        source_routing=routing, selected_source_ids=sorted(included),
        excluded_source_ids=sorted(s['source_id'] for s in routing if s['disposition']=='EXCLUDED_OUTSIDE_EVENT_SCOPE'),
        source_scope='Versioned material scope from replayed source operations; candidate graph and original scope facts unchanged.',
        original_preparation_audit=original, original_preparation_sha256=canonical_sha256(original),
        revision_chain_validation=replay, revision_ledger=[deepcopy(r['ledger']) for r in chain['rounds']],
        original_segment_decisions=deepcopy(original.get('segment_decisions', [])),
        segment_decisions=decisions, disposition_segment_counts=counts,
        disposition_character_counts=chars, unresolved_review_segment_count=counts.get('REVIEW',0),
        semantic_audit_status='NOT_ASSESSED', material_release_permitted=False,
        doctor_packet_created=False, clinical_reference_created=False,
        patient_level_coverage_complete=False, event_graph_modified=False,
        split_proposal_sha256=None,
        split_proposal_note='Initial split proposal retained inside original_preparation_audit; revised partition is bound by replay.',
        all_selected_sources_exactly_partitioned=True)
    result.pop('audit_sha256', None)
    result['audit_sha256'] = canonical_sha256(result)
    return result


def _canonical_chain_documents(chain):
    paths = ['chain', 'chain.initial_manifest', 'chain.initial_compiled', 'chain.source_registry',
             'chain.manifest', 'chain.compiled', 'chain.doctor_view', 'chain.task_context']
    if 'candidate_graph_snapshot' in chain['task_context']:
        paths.append('chain.task_context.candidate_graph_snapshot')
    for index, round_ in enumerate(chain['rounds']):
        paths += [f'chain.rounds.{index}.{name}' for name in
                  ('input_manifest','input_compiled','request','review','manifest','compiled','ledger','diff','application_receipt')]
        paths.append(f'chain.rounds.{index}.request_without_binding')
    if 'failure' in chain:
        paths += ['chain.failure.request', 'chain.failure.request_without_binding']
    result = {}
    for path in paths:
        if path.endswith('.request_without_binding'):
            request = chain['failure']['request'] if path.startswith('chain.failure.') else chain['rounds'][int(path.split('.')[2])]['request']
            value = {k:v for k,v in request.items() if k != 'response_binding'}
        else:
            value = dict(chain=chain)
            for key in path.split('.'):
                value = value[int(key)] if isinstance(value,list) else value[key]
        result[path] = canonical_json(value)
    return result


def build_auto_review_assignment(chain, draft, preparation_audit, *, assignment_id,
                                 selected_event=None, coverage_sources=(), source_fhir_sha256=None):
    """Build a new pending human assignment from a fully replayed chain."""
    replay = validate_review_chain(chain)
    if draft['status'] != 'PENDING' or draft['independence_attested']:
        raise ValueError('HUMAN_DRAFT_MUST_REMAIN_PENDING_UNATTESTED')
    revised = chain['compiled'] != chain['initial_compiled'] or chain['manifest'] != chain['initial_manifest']
    if revised:
        preparation = derive_revision_preparation(chain, preparation_audit)
        if draft['compiled_sha256'] not in (chain['initial_compiled']['compiled_sha256'], chain['compiled']['compiled_sha256']):
            raise ValueError('HUMAN_DRAFT_MUST_BIND_CHAIN_VERSION')
        draft = create_review_draft(chain['compiled'], chain['manifest'], preparation,
            reviewer_id=draft['reviewer_id'], issue_records=draft.get('issue_records', []),
            time_boundary=deepcopy(draft.get('decision_time_boundary')))
    else:
        preparation = deepcopy(preparation_audit)
        if draft['compiled_sha256'] != chain['compiled']['compiled_sha256']:
            raise ValueError('HUMAN_DRAFT_MUST_BIND_NEW_COMPILED_HASH')
    loaded = {s['source_id']:deepcopy(s) for s in coverage_sources}
    for source in chain['source_registry']:
        sid = source['source_id']
        if sid in loaded and loaded[sid]['text'] != source['text']:
            raise ContractError('REVISION_COVERAGE_SOURCE_TEXT_MISMATCH')
        loaded.setdefault(sid, dict(source_id=sid, text=source['text'], sha256=source['sha256']))
    assignment = build_assignment(chain['compiled'], chain['manifest'], deepcopy(draft), preparation,
        assignment_id=assignment_id, selected_event=selected_event,
        coverage_sources=list(loaded.values()), source_fhir_sha256=source_fhir_sha256)
    opinion = dict(format='researcher-automated-material-opinion-0.2.0',
        assignment_sha256=assignment['assignment_sha256'], automatic_state=chain['state'],
        stop_reason=chain['stop_reason'], chain=deepcopy(chain),
        canonical_documents=_canonical_chain_documents(chain), validation_receipt=replay,
        reviewer_type='llm_material_review', independence_attested=False,
        human_sample_status='NOT_ASSESSED', clinical_release_permitted=False, doctor_packet_created=False)
    validate_auto_review_opinion(assignment, opinion)
    return dict(assignment=assignment, auto_review=opinion)


def validate_auto_review_opinion(assignment, opinion):
    """Replay historical operations; checksum matching alone is insufficient."""
    verified = validate_assignment(assignment)
    required = {'format', 'assignment_sha256', 'automatic_state', 'stop_reason', 'chain',
                'canonical_documents', 'validation_receipt', 'reviewer_type',
                'independence_attested', 'human_sample_status',
                'clinical_release_permitted', 'doctor_packet_created'}
    if not isinstance(opinion, dict) or set(opinion) != required:
        raise ValueError('AUTO_OPINION_FIELDS_MISMATCH')
    if (opinion['format'] != 'researcher-automated-material-opinion-0.2.0'
            or opinion['assignment_sha256'] != assignment['assignment_sha256']
            or opinion['reviewer_type'] != 'llm_material_review'
            or opinion['independence_attested'] is not False
            or opinion['human_sample_status'] != 'NOT_ASSESSED'
            or opinion['clinical_release_permitted'] is not False
            or opinion['doctor_packet_created'] is not False):
        raise ValueError('AUTO_OPINION_GOVERNANCE_MISMATCH')
    chain = opinion['chain']
    replay = validate_review_chain(chain)
    if replay != opinion['validation_receipt'] or _canonical_chain_documents(chain) != opinion['canonical_documents']:
        raise ValueError('AUTO_OPINION_REPLAY_RECEIPT_MISMATCH')
    documents = verified['documents']
    if (chain['compiled'] != documents['compiled'] or chain['manifest'] != documents['manifest']
            or opinion['automatic_state'] != chain['state'] or opinion['stop_reason'] != chain['stop_reason']):
        raise ValueError('AUTO_OPINION_INPUT_BINDING_MISMATCH')
    event = documents.get('selected_event')
    graph = replay['candidate_graph_binding']
    if graph is not None and event is not None:
        graph_event = next(e for e in chain['task_context']['candidate_graph_snapshot']['events'] if e['event_id'] == graph['target_event_id'])
        if event != graph_event:
            raise ValueError('AUTO_OPINION_SELECTED_EVENT_GRAPH_MISMATCH')
    preparation = documents['preparation_audit']
    revised = chain['compiled'] != chain['initial_compiled'] or chain['manifest'] != chain['initial_manifest']
    if revised:
        original = preparation.get('original_preparation_audit')
        if original is None or preparation != derive_revision_preparation(chain, original):
            raise ValueError('AUTO_OPINION_REVISION_PREPARATION_MISMATCH')
    return True


def render_auto_opinion(html, assignment, opinion):
    """Inject an independently hash-bound, read-only researcher opinion panel."""
    validate_auto_review_opinion(assignment, opinion)
    embedded = canonical_json(opinion)
    for char, escaped in [('&', '\\u0026'), ('<', '\\u003c'), ('>', '\\u003e'),
                          (' ', '\\u2028'), (' ', '\\u2029')]:
        embedded = embedded.replace(char, escaped)
    script = Path(__file__).with_name('workbench.js').read_text(encoding='utf-8')
    return html.replace('</body>', '<style>.auto-opinion{background:#fff;border:1px solid #d5dde2;border-radius:12px;padding:20px;margin-bottom:20px}.auto-opinion li{margin:18px 0}.auto-opinion pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f6;padding:10px;font-size:12px}</style><p id="auto-opinion-error" class="error" role="alert" hidden></p><script type="application/json" id="auto-opinion-data">'
                        + embedded + '</script><script>' + script + '</script></body>')


def write_workbench_package(package, output):
    output = require_private_output(output)
    output.mkdir(parents=True, exist_ok=False)
    assignment = package['assignment']
    validate_auto_review_opinion(assignment, package['auto_review'])
    html = render_auto_opinion(build_researcher_html(assignment), assignment, package['auto_review'])
    for name, value in [('researcher-assignment.json', assignment),
                        ('automated-material-review.json', package['auto_review'])]:
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output / 'researcher-review.html').write_text(html, encoding='utf-8')
    receipt = dict(status='RESEARCHER_ASSIGNMENT_WITH_AUTO_OPINION_PENDING_HUMAN_REVIEW',
                   assignment_sha256=assignment['assignment_sha256'],
                   auto_review_chain_sha256=canonical_sha256(package['auto_review']['chain']),
                   automatic_state=package['auto_review']['automatic_state'],
                   human_sample_status='NOT_ASSESSED', clinical_release_permitted=False,
                   doctor_packet_created=False, clinical_reference_created=False)
    (output / 'build-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    return receipt


def receive_auto_review_submission(package, response):
    """Revalidate the original governed package before human response recovery."""
    from scripts.cohort_construction.p2_checkpoint_material_review_app.receive import receive_submission
    validate_auto_review_opinion(package['assignment'], package['auto_review'])
    return receive_submission(package['assignment'], response)
