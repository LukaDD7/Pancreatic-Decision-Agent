"""Concrete N01 checkpoint assembly; source choices are reviewed case settings.

This is a local review draft, never a production blinded input or general P2
inclusion rule. No clinical reference action is generated or sent to an agent.
"""
from pathlib import Path
from datetime import datetime
import argparse
import hashlib
import json

from .snapshot import document_block, precise_time, select_reported_labs, split_at_action, visibility_status


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[3])
    args=ap.parse_args();repo=args.repo.resolve()
    source_dir=repo/'local_artifacts/p2_dynamic_pathway_probe_v0.1_20261021'
    case=next(c for c in json.loads((source_dir/'five_case_review.json').read_text()) if c['case_id']=='N01')
    notes=[n for n in map(json.loads,(source_dir/'review_patient_context.jsonl').read_text().splitlines())
           if n['patient_uid']==case['patient_uid']]
    bykey={n['record_key']:n for n in notes}
    anchor=bykey[case['record_key']]
    cutoff=precise_time(anchor['body_time']);assert cutoff==datetime(2024,12,10,9,26)
    project=Path('/Users/lucachangretta/Documents/ChatGPT/不可逆胰腺癌根治术决策智能体')
    source_patient=project/'data/PDAC/batches/batch_008/patients'/case['patient_uid']
    bundle_path=source_patient/'patient_source_bundle.json'
    bundle=json.loads(bundle_path.read_text())
    images_path=source_patient/'imaging_reports_all_sources.jsonl'
    images=[json.loads(s) for s in images_path.read_text().splitlines()]
    out=repo/'local_artifacts/n01_checkpoint_casebook_v0.1_20261022';out.mkdir(parents=True,exist_ok=True)

    # Reviewed, declared inclusion: all six prior clinical source notes in this
    # local reconstruction. Administrative education/consent forms are not state.
    prior_keys=['f77d41fe3013b6ef315de923','7e3233b42b99f392725381cd',
                'db297a7d328daaf6145b92ba','4ae5a785ff15ff090c2bdcef',
                '0ab9a6dd33a1da04c003a9f7','396323346aab110ac28bf70f']
    identities={r.get('NAME','') for r in bundle['laboratory_original_L1_all']}
    identities.update(r.get('PATIENT_ID','') for r in bundle['documents_all'])
    values=[v for v in identities if v]
    current_prefix,held_span=split_at_action(anchor['text'],'今予PTCD术胆汁引流')
    prior_blocks=[]
    for key in prior_keys:
        n=bykey[key]
        event=precise_time(n['body_time']) or precise_time(n['system_time'])
        assert event and event<cutoff
        prior_blocks.append(document_block(n,n['text'],values))
    current_block=document_block(anchor,current_prefix,values,len(current_prefix))

    # Use full raw original-cluster MR/CT reports once each, not the extracted
    # staging labels or duplicate copies in disease-specific spreadsheets.
    imaging_ids=['all-original:A039.csv.record_clusters.jsonl:18145',
                 'all-original:A039.csv.record_clusters.jsonl:15857']
    image_blocks=[]
    for report_id in imaging_ids:
        r=next(r for r in images if r['index_report_uid']==report_id)
        assert precise_time(r['exam_datetime'])<cutoff
        image_blocks.append({'source_id':report_id,'source_kind':'imaging_report',
                             'exam_time':r['exam_datetime'],'method':r['exam_method'],
                             'findings':r['original_findings'],'impression':r['original_impression'],
                             'time_basis':'EXAM_TIME_ONLY_REPORT_RELEASE_AND_READING_UNVERIFIED'})
    lab_rows,lab_audit=select_reported_labs(bundle['laboratory_original_L1_all'],cutoff)
    packet={'case_token':'N01','status':'EVENT_TIME_RECONSTRUCTION_DRAFT_NOT_FORMAL_BLINDED_PACKET',
            'hypothetical_checkpoint_time':str(cutoff),
            'task':'截至所示资料，当前根治性切除路径应如何管理？请说明下一步处理或补充信息，以及什么变化会触发重新评估。',
            'clinical_documents':prior_blocks+[current_block],
            'imaging_reports':image_blocks,'laboratory_results_raw':lab_rows,
            'availability_certified':False}
    write(out/'agent_state_reconstruction.json',packet)
    write(out/'reviewer_source_audit.json',{
        'anchor_source':anchor,'held_current_action_span':held_span,
        'held_current_action_text':anchor['text'][held_span['action_start']:held_span['action_end']],
        'prior_source_notes':[bykey[k] for k in prior_keys],
        'visibility_audit':[{**{'source_id':k},**visibility_status(bykey[k]['body_time'],bykey[k]['system_time'],cutoff)} for k in prior_keys],
        'anchor_visibility':visibility_status(anchor['body_time'],anchor['system_time'],cutoff),
        'imaging_sources':[r for r in images if r['index_report_uid'] in imaging_ids],
        'reviewer_only_procedure_corroboration':[r for r in images if r['index_report_uid'] in [
            'all-original:A039.csv.record_clusters.jsonl:15801',
            'all-original:A039.csv.record_clusters.jsonl:9348']],
        'lab_selection_audit':lab_audit,
        'all_context_note_metadata':[{k:n[k] for k in ['record_key','document_type','body_time','system_time','source_file','source_row']} for n in notes],
        'NOT_agent_input_fields':['observed current choice','future procedure/outcome','future pathology','P0 confirmation label','researcher summary/decision labels']})

    followup_keys=['e5ac77a875f3b4a3eb999a83','43f819d71d70034cc3bf0c96','1e6a2b321477ddbb96b21bfc']
    write(out/'reviewer_only_followup.json',[bykey[k] for k in followup_keys])
    lines=['# N01：候选时点的决策前资料草稿\n',
           '按正文时序重建，尚未认证当时可见性；不是正式盲评包。原文按来源分块展示，未改写或拼接临床事实。\n',
           f'假设评价时点：{cutoff}。\n',packet['task']+'\n']
    for block in packet['clinical_documents']:
        lines += [f'\n## {block["document_type"]}\n',
                  f'来源键：{block["source_id"]}；正文头{block["body_time"]}；系统创建{block["record_create_time"]}\n',
                  block['text']+'\n']
    for block in image_blocks:
        lines += [f'\n## 原始影像报告：{block["method"]}\n',f'检查时间{block["exam_time"]}；来源键{block["source_id"]}；签发/阅片时间未认证\n',block['findings']+'\n',block['impression']+'\n']
    lines += ['\n## 时点前报告的检验原始字段\n','报告时间是来源代理，送检时间缺失保留；数值及“已复”等原文不作二次解释。\n',
              '|报告时间|项目|原始结果|原始单位|原参考值|\n|---|---|---|---|---|\n']
    for r in lab_rows:
        lines.append('|'+ '|'.join(str(r[k] or '').replace('|','\\|') for k in ['report_time','item','result_raw','unit_raw','reference_raw'])+'|\n')
    (out/'N01-决策前资料草稿.md').write_text('\n'.join(lines))
    follow=['# N01：研究者查看的当次选择与后续原文\n','本文件不进入Agent输入，也不等于临床Reference。\n',
            '## 当次来源文书完整原文\n',anchor['text']+'\n']
    for key in followup_keys:
        n=bykey[key]
        follow += [f'\n## {n["document_type"]}｜正文{n["body_time"]}｜系统{n["system_time"]}\n',n['text']+'\n']
    (out/'N01-当次选择与后续-研究者查看.md').write_text('\n'.join(follow))

    assert len(lab_rows)==138
    assert len(packet['clinical_documents'])==7 and len(image_blocks)==2
    assert all(k not in {b['source_id'] for b in packet['clinical_documents']} for k in followup_keys)
    assert '今予PTCD' not in current_block['text']
    assert not {'patient','confirmation_evidence','pathology_all','NAT_outcome_summary_analysis_only'} & set(packet)
    assert all(precise_time(r['report_time'])<cutoff for r in lab_rows)
    adjacency={}
    for clock in ['body_time','system_time']:
        ordered=sorted([n for n in notes if precise_time(n[clock])],key=lambda n:(precise_time(n[clock]),n['record_key']))
        i=next(i for i,n in enumerate(ordered) if n['record_key']==anchor['record_key'])
        adjacency[clock]={'previous_source_id':ordered[i-1]['record_key'],'next_source_id':ordered[i+1]['record_key']}
        assert adjacency[clock]['previous_source_id']=='396323346aab110ac28bf70f'
        assert adjacency[clock]['next_source_id']=='e5ac77a875f3b4a3eb999a83'
    write(out/'build_receipt.json',{
        'status':packet['status'],'clinical_blocks':7,'prior_laboratory_rows':len(lab_rows),'imaging_blocks':2,
        'selected_prior_documents_created_after_nominal_cutoff':sum(not visibility_status(bykey[k]['body_time'],bykey[k]['system_time'],cutoff)['create_precedes_cutoff'] for k in prior_keys),
        'adjacent_clinical_sources_in_both_observation_clocks':adjacency,
        'source_inputs_sha256':{str(p):sha(p) for p in [bundle_path,images_path,source_dir/'review_patient_context.jsonl']},
        'code_sha256':{p.name:sha(p) for p in Path(__file__).parent.glob('*.py')},
        'outputs_sha256':{p.name:sha(p) for p in out.iterdir() if p.is_file() and p.name!='build_receipt.json'},
        'checks':'source spans/action cut; 138 prior lab rows; future clinical notes excluded; raw outcome containers excluded',
        'formal_blind_evaluation_ready':False})
    print(json.dumps({'clinical_blocks':7,'prior_labs':len(lab_rows),'imaging_blocks':2,
                      'formal_blind_evaluation_ready':False},ensure_ascii=False))


if __name__=='__main__':main()
