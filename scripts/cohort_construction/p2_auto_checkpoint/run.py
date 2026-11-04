"""Run automatic candidates and LLM semantic screening on a patient manifest."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path

from .extract import comparison_clock, digest, extract, load_sources, read, time_order, window, write
from .fhir_io import build, validate, import_sources
from .semantic import Client, analyze_evolution, normalize

ROOT = Path(__file__).resolve().parents[3]


def screen(normalized):
    eligible, unselected = [], []
    for c in normalized:
        r = c['semantic']
        if r['scope'] == 'no' or r['role'] in ['report_restatement', 'template_or_hypothetical', 'nondecision']:
            unselected.append(dict(candidate_id=c['candidate_id'], reason='SEMANTIC_'+r['role']+'_'+r['scope']))
        else:
            eligible.append(c)
    return eligible, unselected


def choose_comparator(current, eligible, previous, retained=None):
    """Use a same-note historical sequence when exact times are unavailable.

    This only proposes a comparator. The model must still assess whether the
    two choices concern the same management question and have evidence change.
    """
    if current.get('selection_basis') == 'historical_test_trace':
        earlier = [c for c in eligible if c['source_id']==current['source_id']
                   and c['candidate_id']!=current['candidate_id']
                   and c['anchor']['end'] <= current['anchor']['start']
                   and c['semantic']['role'] in ['decision','historical_performed_test']]
        if earlier:
            return dict(max(earlier,key=lambda c:c['anchor']['end']),
                        comparison_basis='same_source_narrative_order_only_not_exact_timed_interval')
    if current.get('management_track') and retained is not None:
        same=[c for c in retained if c.get('management_track')==current['management_track']
              and time_order(comparison_clock(c),comparison_clock(current))=='BEFORE']
        if same:
            return dict(max(same,key=lambda c:comparison_clock(c)['value']),
                        comparison_basis='prior_retained_same_management_track_not_confirmed_clinical_previous')
        # A first visible cardiac workup should not be compared against an
        # unrelated nutrition choice just because it was recorded last.
        return None
    return previous


def select_status(c, evolution, previous):
    if c['semantic']['scope'] != 'yes':
        return 'UNCERTAIN_SCOPE'
    test_trace = c.get('selection_basis') in ['performed_test_report', 'historical_test_trace']
    if not test_trace and (c['semantic']['role'] != 'decision' or c['historical_hint'] or c['semantic']['temporal_role'] == 'historical'):
        return 'HISTORICAL_OR_UNRESOLVED_CANDIDATE'
    if not comparison_clock(c)['value']:
        return 'UNCERTAIN_TIME'
    if c.get('previous_order_uncertain'):
        return 'UNCERTAIN_PREVIOUS_ORDER'
    if evolution.get('status') != 'VALIDATED_QUOTES':
        return 'UNCERTAIN_SEMANTIC_PROCESS'
    if not evolution['current_judgment_supported']:
        return 'UNCERTAIN_CURRENT_JUDGMENT'
    if evolution.get('evidence_guard')=='REPEATED_WORDING_IDENTITY_UNCERTAIN':
        return 'UNCERTAIN_REPEATED_WORDING_IDENTITY'
    if evolution.get('evidence_guard'):
        return 'REPEATED_OR_FORM_ONLY_NO_NEW_CHECKPOINT'
    if evolution['relation_to_previous'] == 'same_round':
        return 'GROUP_WITH_PREVIOUS_PENDING_COREFERENCE_REVIEW'
    if evolution['evidence_evolution'] == 'repeat_only':
        return 'REPEATED_PLAN_NO_NEW_CHECKPOINT'
    if previous is None and evolution['evidence_evolution'] == 'first_visible':
        if test_trace:
            return 'CANDIDATE_FIRST_VISIBLE_TEST_TIME_UNCERTAIN' if evolution['supports'] else 'UNCERTAIN_PRECHOICE_STATE'
        return 'CANDIDATE_FIRST_VISIBLE'
    if evolution['evidence_evolution'] == 'new_relevant' and evolution['supports']:
        # Current-choice quotes alone do not establish new prior evidence.
        prior = [a for a in evolution['supports'] if a['source_id'] not in c.get('same_test_source_ids', [])
                 and not (a['source_id'] == c['source_id'] and a['end'] > c.get('choice_start',c['anchor']['start']))]
        if previous:
            previous_is_test_trace=previous.get('selection_basis') in ['performed_test_report','historical_test_trace']
            known=previous.get('carried_evidence',[])
            if not previous_is_test_trace:
                known=known+previous.get('semantic',{}).get('supports',[])
            prior=[a for a in prior if not any(k['source_id']==a['source_id'] and k['start']<=a['start'] and k['end']>=a['end'] for k in known)]
        if not prior:
            return 'UNCERTAIN_NO_PRECHOICE_EVIDENCE_SUPPORT'
        if evolution['management_process_link'] == 'supported' and not evolution['connection_supports']:
            return 'UNCERTAIN_CONNECTION_SUPPORT'
        return 'CANDIDATE_TEST_NEW_EVIDENCE_TIME_UNCERTAIN' if test_trace else 'CANDIDATE_NEW_EVIDENCE'
    return 'UNCERTAIN_EVIDENCE_EVOLUTION'


def locate_bundle(uid):
    paths = list((ROOT/'data/PDAC/batches').glob('*/patients/'+uid+'/patient_source_bundle.json'))
    if len(paths) != 1:
        raise ValueError('PATIENT_BUNDLE_NOT_UNIQUE')
    return paths[0]


def process_case(row, args, config):
    cid = row['case_id']; out = args.output/cid; out.mkdir(parents=True, exist_ok=True)
    if row.get('fhir_path'):
        path=Path(row['fhir_path']); sources,_=import_sources(read(path))
        inventory=dict(Counter(s['family'] for s in sources))
    else:
        path = locate_bundle(row['patient_uid']); sources, inventory = load_sources(path, row['patient_uid'])
    source_fhir = build(sources, cid, [], [])
    source_fhir_check = validate(source_fhir, args.schema)
    restored, roundtrip = import_sources(source_fhir)
    assert {(s['source_id'],s['source_sha256']) for s in sources} == {(s['source_id'],s['source_sha256']) for s in restored}
    # Extraction actually consumes the FHIR round-trip source table.
    sources=restored
    write(out/'source_fhir_r4_bundle.json', source_fhir)
    candidates, reports, coverage = extract(sources, config)
    write(out/'source_inventory.json', dict(inventory=inventory, source_container_sha256=digest(read(path)),
                                           coverage=coverage, report_events=reports,
                                           note='Report rows may be variants of the same examination; none is automatically a decision.'))
    write(out/'rule_candidates.json', candidates)
    if args.stage == 'extract':
        return dict(case_id=cid, rule_candidates=len(candidates), report_rows=len(reports), source_inventory=inventory)
    client = Client(args.cache_dir or args.output/'model_cache', config)
    primary=[c for c in candidates if c.get('flow_route','primary')=='primary']
    support_only=[dict(candidate_id=c['candidate_id'],reason=c['flow_reason'],
                       bundle_primary_id=c.get('bundle_primary_id'),anchor=c['anchor'])
                  for c in candidates if c.get('flow_route')=='support_only']
    write(out/'document_support_mentions.json',support_only)
    write(out/'progress.json',dict(stage='normalization',primary_inputs=len(primary),support_mentions=len(support_only)))
    normalized, failures = normalize(primary, sources, client, config)
    write(out/'normalization.json', dict(results=normalized, failures=failures))
    eligible, unselected = screen(normalized)
    decisions, windows, episodes = [], [], []
    previous = None; episode = None; repeat_seen = {};retained=[];episode_by_candidate={}
    for index,c in enumerate(eligible):
        write(out/'progress.json',dict(stage='evidence_evolution',completed=index,total=len(eligible),model_calls=len(client.receipts)))
        # Only exact same historical performance text+date+topic is collapsed
        # as a repeated mention; prospective/current judgments always survive.
        key = c['possible_repeat_key']
        if c['historical_hint'] and key in repeat_seen:
            unselected.append(dict(candidate_id=c['candidate_id'], reason='EXACT_HISTORICAL_RESTATEMENT', prior_candidate_id=repeat_seen[key]))
            continue
        if c['historical_hint']:
            repeat_seen[key] = c['candidate_id']
        comparison_previous = choose_comparator(c, eligible, previous,retained if config.get('document_aware_flow') else None)
        narrative_order = comparison_previous and comparison_previous.get('comparison_basis')=='same_source_narrative_order_only_not_exact_timed_interval'
        if comparison_previous and not narrative_order and time_order(comparison_clock(comparison_previous), comparison_clock(c)) != 'BEFORE':
            same_note_prior_stage=(comparison_previous['source_id']==c['source_id'] and
                                   comparison_previous['anchor']['end']<=c['anchor']['start'] and
                                   not comparison_previous['historical_hint'] and not c['historical_hint'] and
                                   comparison_previous['decision_clock']['value'] and c['decision_clock']['value'])
            if not same_note_prior_stage:
                comparison_previous = None
                c=dict(c,previous_order_uncertain=True)
        win = window(sources, c, comparison_previous)
        test_trace = c.get('selection_basis') in ['performed_test_report', 'historical_test_trace']
        if (not test_trace and (not c['decision_clock']['value'] or c['historical_hint'] or
            c['semantic']['role']!='decision' or c['semantic']['temporal_role']=='historical' or
            c['semantic']['scope']!='yes' or c.get('previous_order_uncertain'))):
            evo=dict(status='NOT_EVALUATED_PREREQUISITE_UNCERTAIN',
                     reason='CLOCK_ROLE_SCOPE_OR_HISTORICAL_PRECHOICE_STATE_UNRESOLVED',
                     missing_information=c['semantic']['missing_information'])
        else:
            try:
                evo, _ = analyze_evolution(c, comparison_previous, win, sources, client, config)
            except Exception as exc:
                evo = dict(status='FAILED_OR_INVALID_QUOTES', error=type(exc).__name__)
        status = select_status(c, evo, comparison_previous)
        item = dict(c, evolution=evo, checkpoint_status=status,
                    previous_candidate_id=comparison_previous['candidate_id'] if comparison_previous else None,
                    uncertainty_dimensions=dict(scope=c['semantic']['scope'],
                                                event_time=c['decision_clock']['precision'],
                                                evidence_link=evo.get('management_process_link', 'uncertain'),
                                                source_availability='proxy_not_certified',
                                                prechoice_completeness='not_certified'),
                    blind_replay_ready=False, expert_reference=False)
        if status.startswith('CANDIDATE_'):
            linked_episode=episode_by_candidate.get(item['previous_candidate_id']) if evo.get('same_task')=='yes' else None
            if linked_episode:
                episode=linked_episode
            else:
                episode = dict(episode_id='EP'+digest([cid, c['candidate_id']])[:18],
                               management_question=c['semantic']['management_question'],
                               status='candidate_not_clinician_confirmed', candidate_ids=[], uncertain_links=[])
                episodes.append(episode)
            episode['candidate_ids'].append(c['candidate_id'])
            if comparison_previous and evo.get('same_task') == 'uncertain':
                episode['uncertain_links'].append([comparison_previous['candidate_id'], c['candidate_id']])
            item['episode_id'] = episode['episode_id']
            episode_by_candidate[c['candidate_id']]=episode
            # Unknown earlier choice times cannot become a precise lower bound
            # for the next candidate. Keep them as nodes, not fabricated clocks.
            if c['decision_clock']['value']:
                previous = dict(c,carried_evidence=evo.get('supports',[]))
                retained.append(previous)
        decisions.append(item)
        write(out/'window_audits'/f'{c["candidate_id"]}.json', dict(
            candidate_id=c['candidate_id'], previous_candidate_id=item['previous_candidate_id'], **win))
        windows.append(dict(candidate_id=c['candidate_id'], included=len(win['blocks']),
                            dispositions=dict(Counter(x['disposition'] for x in win['source_ledger']))))
    write(out/'decision_events.json', decisions)
    write(out/'unselected_and_failed.json', dict(unselected=unselected, support_mentions=support_only,failures=failures))
    write(out/'episodes.json', episodes)
    write(out/'model_receipts.json', client.receipts)
    bundle = build(sources, cid, decisions, episodes)
    check = validate(bundle, args.schema)
    write(out/'research_fhir_r4_bundle.json', bundle)
    summary = dict(case_id=cid, rule_candidates=len(candidates), report_rows=len(reports),
                   primary_model_inputs=len(primary),support_only_mentions=len(support_only),
                   source_inventory=inventory, normalized=len(normalized), normalization_failures=len(failures),
                   semantic_decision_candidates=len(decisions),
                   checkpoint_statuses=dict(Counter(x['checkpoint_status'] for x in decisions)),
                   candidate_checkpoints=sum(x['checkpoint_status'].startswith('CANDIDATE_') for x in decisions),
                   candidate_episodes=len(episodes), model_calls=len(client.receipts),
                   paged_evidence_windows=sum(bool(e['evolution'].get('paging')) for e in decisions),
                   evidence_pages=sum(e['evolution'].get('paging',{}).get('page_count',0) for e in decisions),
                   cached_calls=sum(r['cached'] for r in client.receipts), fhir_validation=check,
                   source_fhir_validation=source_fhir_check, fhir_source_roundtrip=roundtrip,
                   input_window_summary=windows, clinical_accuracy_measured=False,
                   blind_packets_created=False, case_specific_anchor_list_used=False,
                   source_input_sha256=digest(read(path)))
    write(out/'model_receipts.json', client.receipts)
    write(out/'summary.json', summary)
    write(out/'progress.json',dict(stage='complete',summary_path=str(out/'summary.json')))
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    inputs=ap.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--manifest', type=Path)
    inputs.add_argument('--fhir-bundle', type=Path)
    ap.add_argument('--case-id', default='FHIR-CASE')
    ap.add_argument('--config', type=Path, default=ROOT/'config/p2_auto_checkpoint_v03.json')
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--cache-dir', type=Path)
    ap.add_argument('--schema', type=Path, required=True)
    ap.add_argument('--cases', nargs='*')
    ap.add_argument('--stage', choices=['extract', 'full'], default='full')
    ap.add_argument('--workers', type=int, default=3)
    a = ap.parse_args(); config = read(a.config)
    manifest = read(a.manifest) if a.manifest else [dict(case_id=a.case_id,fhir_path=str(a.fhir_bundle))]
    rows = manifest['selected'] if isinstance(manifest, dict) else manifest
    if a.cases:
        rows = [r for r in rows if r['case_id'] in a.cases]
    if not rows:raise ValueError('NO_CASES')
    a.output.mkdir(parents=True, exist_ok=True)
    write(a.output/'run_contract.json', dict(config=config, config_sha256=digest(config),
          input_manifest_sha256=digest(manifest), stage=a.stage, model_configuration='existing config/model_provider.json',
          clinical_reference_used=False, prior_decision_event_outputs_read=False,
          old_case_membership_only_from_manifest=True, automatic_semantic_calls=a.stage=='full',
          source_code_sha256={p.name:digest(p.read_text()) for p in Path(__file__).parent.glob('*.py')}))
    results = []
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = {pool.submit(process_case, row, a, config):row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                r = future.result()
            except Exception as exc:
                r = dict(case_id=row['case_id'], status='FAILED', error=type(exc).__name__, detail=str(exc)[:600])
                write(a.output/row['case_id']/'failure.json', r)
            results.append(r)
            print(json.dumps({k:v for k,v in r.items() if k not in ['input_window_summary','source_inventory']}, ensure_ascii=False), flush=True)
            write(a.output/'summary.json', sorted(results, key=lambda x:x['case_id']))
    if any(r.get('status')=='FAILED' for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
