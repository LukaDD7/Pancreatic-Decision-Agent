"""Integration checks migrate old protections and add new source-gap controls."""
import collections
import hashlib
import json
import random
from pathlib import Path
from .core import relation_to_cutoff,validate_partition,sha
from .prepare_scale10 import BASE,OUT

def main():
    checks=[]
    def check(name,condition,detail=''):
        checks.append(dict(check=name,passed=bool(condition),detail=detail))
        assert condition,(name,detail)
    old_receipt=json.loads((BASE/'run_receipt.json').read_text())
    check('baseline_all_frozen_artifacts_unchanged',all(hashlib.sha256((BASE/p).read_bytes()).hexdigest()==h for p,h in old_receipt['artifacts'].items()))
    mf=json.loads((OUT/'sampling_manifest.json').read_text());frame=json.loads((BASE/'sampling_frame_patient_uids.json').read_text())
    if isinstance(frame,dict):frame=frame.get('patient_uids',frame.get('uids'))
    expected=random.Random(mf['seed']).sample(sorted(set(frame)-set(mf['excluded_prior_development_patient_uids'])),10)
    check('same_seed_and_ten_patients',expected==[c['patient_uid'] for c in mf['selected']])
    check('unchanged_two_core_rule_files',all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in mf['rules_sha256'].items()))
    old_packets={p['packet_id']:p for p in json.loads((BASE/'packet_manifest.json').read_text())}
    packet_diffs=[];observations=[];totals=collections.Counter();all_audits={}
    for c in mf['selected']:
        cid=c['case_id'];base=OUT/cid
        data=json.loads((base/'sources.json').read_text());sources=data['sources'];byid={s['source_id']:s for s in sources}
        raw=Path(data['source_container']).read_bytes()
        check(cid+':original_container_unchanged',hashlib.sha256(raw).hexdigest()==data['source_container_sha256'])
        check(cid+':all_sources_hashes_match',all(sha(s['text'])==s['source_sha256'] for s in sources))
        check(cid+':all_original_families_inventoried',set(data['source_family_inventory'])=={'documents_all','imaging_all','laboratory_original_L1_all','laboratory_source_quarantine','pathology_source_rows_all'})
        totals.update(data['source_family_inventory']);r=json.loads((base/'trial_result.json').read_text())
        for m in r['mixed_document_masks']:validate_partition(byid[m['source_id']],m['segments'])
        check(cid+':mixed_partitions_and_stage_keep_adverse',True)
        audits=json.loads((base/'packet_audit.json').read_text());all_audits[cid]=audits
        check(cid+':one_input_per_checkpoint',len(audits)==len(r['checkpoints']) and set(p['checkpoint_id'] for p in audits)==set(cp['checkpoint_id'] for cp in r['checkpoints']))
        check(cid+':episode_task_not_split_by_added_reports',len(r['episodes'])==1)
        check(cid+':all_candidate_evidence_quotes_match',all(a['quote']==byid[a['source_id']]['text'][a['start']:a['end']] for cp in r['checkpoints'] for a in cp['evidence']+cp['documented_decision']))
        for p in audits:
            ledger=p['source_selection'];sp=p['source_provenance'];sel={a['source_id'] for a in sp}
            check(p['packet_id']+':every_source_has_a_reason',len(ledger)==len(sources) and {x['source_id'] for x in ledger}==set(byid) and all(x['reason'] for x in ledger))
            text=Path(p['doctor_view_file']).read_text();view=json.loads(text)
            clinical='\n'.join(t for b in view['sources'] for t in b['passages'])
            check(p['packet_id']+':doctor_agent_same_frozen_bytes',p['doctor_view_file']==p['agent_view_file'] and hashlib.sha256(text.encode()).hexdigest()==p['clinical_view_sha256'])
            cp=next(x for x in r['checkpoints'] if x['checkpoint_id']==p['checkpoint_id'])
            check(p['packet_id']+':current_choice_absent',all(a['quote'] not in clinical for a in cp['documented_decision']))
            check(p['packet_id']+':original_quotes_not_rewritten',all(a['quote']==byid[z['source_id']]['text'][a['start']:a['end']] for z in sp for a in z['spans']))
            check(p['packet_id']+':current_observations_not_overmasked',all(x['disposition']!='KEEP' or x['quote'] in clinical for x in p['current_evidence_visibility']))
            safe=all(relation_to_cutoff(byid[sid]['clocks']['exam_time' if byid[sid]['kind']=='imaging' else 'report_time'],p['cutoff_proxy'])=='BEFORE' for sid in sel if byid[sid]['kind'] in ['imaging','lab','pathology'])
            check(p['packet_id']+':no_future_or_same_unordered_raw_results',safe)
            check(p['packet_id']+':no_quarantine_promoted_or_source_access_link',not any(byid[sid]['kind']=='quarantine' for sid in sel) and all(k not in text for k in ['patient_source_bundle','source_provenance','current_choice_occurrences','raw_record_path']))
            if p['clinical_phase']=='after_exploration_before_current_choice':
                check(p['packet_id']+':operative_metadata_and_execution_hidden','实施手术名称' not in clinical and '遂决定' not in clinical and '手术顺利，麻醉效果满意' not in clinical)
                observations.append(dict(case_id=cid,operative_phase='usable_retrospective_reconstruction',late_segment_reviewed=True,new_late_checkpoint=False))
            if p['baseline_view_id']:
                before=old_packets[p['baseline_view_id']]
                old_by={z['source_id']:z for z in before['source_provenance']}
                def coverage(pp):
                    out=collections.defaultdict(set)
                    for z in pp:
                        for a in z['spans']:out[z['source_id']].update(range(a['start'],a['end']))
                    return out
                oc=coverage(before['source_provenance']);nc=coverage(sp)
                added=sum(len(v-oc[sid]) for sid,v in nc.items());removed=sum(len(v-nc[sid]) for sid,v in oc.items())
                packet_diffs.append(dict(case_id=cid,packet_id=p['packet_id'],old_unique_source_rows=len(oc),new_unique_source_rows=len(nc),new_kind_counts=p['source_family_selected'],
                    added_source_characters=added,removed_source_characters=removed,new_original_source_ids=[sid for sid in nc if sid not in oc],
                    old_clinical_view_sha256=before['clinical_view_sha256'],new_clinical_view_sha256=p['clinical_view_sha256']))
    def packet(cid,pid):
        a=next(x for x in all_audits[cid] if x['packet_id']==pid)
        v=json.loads(Path(a['doctor_view_file']).read_text());return a,'\n'.join(q for s in v['sources'] for q in s['passages'])
    a,t=packet('SCALE-06','VIEW-06-01');check('P2SRC-001:CTA_in_preop_state','冠状动脉CTA未见明显异常' in t)
    a,t=packet('SCALE-06','CPVIEW-SCALE-06-CP02');check('P2SRC-001:CTA_not_in_earlier_request','冠状动脉CTA未见明显异常' not in t)
    a,t=packet('SCALE-06','VIEW-06-01');check('P2SRC-001:abnormal_troponin_and_echo_not_erased','2.224' in t and '2.016' in t and '左室收缩功能正常' in t)
    a,t=packet('SCALE-10','VIEW-10-01');check('P2SRC-002:late_MRI_not_in_preop',not any(z['source_id']=='94c356ff146a54e167ae909e' for z in a['source_provenance']))
    a,t=packet('SCALE-10','VIEW-10-02');check('P2SRC-002:same_MRI_in_later_phase',any(z['source_id']=='94c356ff146a54e167ae909e' for z in a['source_provenance']))
    a,t=packet('SCALE-07','VIEW-07-01');check('P2SRC-002:afternoon_CT_MR_not_moved_to_morning',not ({'37c1ced7e5c3709df39f573e','5e9d684a2abd84055ba62d83'} & {z['source_id'] for z in a['source_provenance']}))
    a,t=packet('SCALE-09','VIEW-09-03');check('P2SRC-006:MRI_in_decision_field_preserved','肠系膜上静脉≤180°' in t and 'EUS+FNA' not in t)
    a,t=packet('SCALE-09','VIEW-09-01');check('P2SRC-001:retrieval_does_not_start_only_at_first_checkpoint',any(z['source_id']=='735c7e5ba1d790a75807b759' for z in a['source_provenance']))
    a,t=packet('SCALE-05','VIEW-05-03');check('P2SRC-005:prior_non_checkpoint_plan_survives','定于2023年11月09日' in t and '包绕肝动脉及肠系膜上动脉' in t and '予取消手术' not in t)
    a,t=packet('SCALE-01','VIEW-01-01');check('P2SRC-006:preop_adverse_SMA_CT_preserved','包绕肠系膜上动脉' in t)
    pointdiff=json.loads((OUT/'checkpoint_diff.json').read_text())
    check('all_28_old_points_aligned_by_source_span',sum(d['old_id'] is not None for d in pointdiff)==28)
    check('all_22_old_views_rerun',len(packet_diffs)==22)
    result=dict(status='PASS',checks=checks,check_count=len(checks),mechanical_scope='Original-source/time/mask/coverage and deterministic semantic-proposal constraints; not independent clinical validation.',source_inventory_totals=dict(totals),baseline_protections_migrated=True)
    (OUT/'integration_checks.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (OUT/'input_diff.json').write_text(json.dumps(packet_diffs,ensure_ascii=False,indent=2)+'\n')
    (OUT/'operative_phase_retest.json').write_text(json.dumps(observations,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(status='PASS',check_count=len(checks),inventory=dict(totals),views_matched=len(packet_diffs),new_assessment_rounds=sum(d['old_id'] is None for d in pointdiff)),ensure_ascii=False))

if __name__=='__main__':main()
