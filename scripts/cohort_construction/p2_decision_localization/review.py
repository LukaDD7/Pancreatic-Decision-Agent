"""Build the private semantic-review ledger and bidirectional source cards.

Manual annotations are private inputs, not generated clinical ground truth.
This program verifies quoted spans and exposes rule matches and time neighbors.
It cannot independently validate the analyst's semantic interpretation.
"""
from pathlib import Path
from collections import Counter, defaultdict
import argparse
import json
from .documents import observed_time, visit_key
from .run import write


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    args=parser.parse_args();folder=args.input
    notes=list(map(json.loads,(folder/'notes.jsonl').read_text().splitlines()))
    ni={n['record_key']:n for n in notes};hits=json.loads((folder/'hits.json').read_text())
    hi=defaultdict(list)
    for h in hits:hi[h['record_key']].append(h)
    annotations=json.loads((folder/'semantic_annotations.json').read_text())
    by_visit=defaultdict(list);by_patient=defaultdict(list)
    for n in notes:
        by_visit[(n['patient_uid'],visit_key(n))].append(n)
        by_patient[n['patient_uid']].append(n)
    cards=['# 双向定位：私有语义记录与时间线\n\n分析者目的抽样源文书复核；非独立临床参考标准，不估计敏感度/准确率。'
           '“决策记录”指文书确实承载当次选择、暂缓或待评估安排；不能证明最初临床决定的精确时刻。原文及身份线索仅本地保留。\n']
    ledger=[]
    for a in annotations:
        n=ni[a['record_key']];text=n['text']
        for evidence in a['evidence']:
            assert text[evidence['start']:evidence['end']]==evidence['quote']
        row={**a,'patient_uid':n['patient_uid'],'document_type':n['document_type'],'family':n['family'],
             'visit_id':n['visit_id'],'body_time':n['body_time'],'system_time':n['system_time'],
             'explicit_dates':n['explicit_dates'],'rule_hits':hi[n['record_key']]}
        group=by_visit[(n['patient_uid'],visit_key(n))]
        t=observed_time(n)
        before=[x for x in group if observed_time(x) and t and observed_time(x)<=t and x['record_key']!=n['record_key'] and x['family'] not in {'PREOP_BRIEF','PREOP_DISCUSSION'} and any(h['candidate_eligible'] for h in hi[x['record_key']])]
        after=[x for x in group if observed_time(x) and t and observed_time(x)>=t and x['record_key']!=n['record_key'] and x['family'] in {'PREOP_BRIEF','PREOP_DISCUSSION'} and any(h['label']=='PLAN_RESECTION' for h in hi[x['record_key']])]
        neighbors=sorted(before,key=observed_time)[-2:]+sorted(after,key=observed_time)[:1]
        row['nearest_source_neighbors']=[{'record_key':x['record_key'],'family':x['family'],'body_time':x['body_time'],'system_time':x['system_time'],'explicit_dates':x['explicit_dates'],'body_proxy_gap_hours':round((observed_time(x)-t).total_seconds()/3600,3)} for x in neighbors]
        # Reverse review must not condition the neighborhood on a rule hit.
        # Include physician records across VISIT_ID within +/-7 days, with a
        # full patient index retained in notes.jsonl for longer history tracing.
        independent=[x for x in by_patient[n['patient_uid']] if observed_time(x) and t and abs((observed_time(x)-t).total_seconds())<=7*86400 and x['family'] in {'FIRST_COURSE','FIRST_SENIOR_ROUND','WARD_ROUND','CONSULT','MDT','OUTPATIENT','PREOP_BRIEF','PREOP_DISCUSSION','TRANSFER_IN','TRANSFER_OUT','ADMISSION'}]
        independent=sorted(independent,key=lambda x:(observed_time(x),x['record_key']))
        row['unconditioned_neighborhood_records']=[x['record_key'] for x in independent]
        ledger.append(row)
        cards.append(f"\n## {a['case_id']}：{a['title']}\n\n语义类型：{a['semantic_class']}；定位：{a['interpretation']}\n\n原始文书名：{n['document_type']}；类型组：{n['family']}；VISIT_ID：{n['visit_id']}\n\n正文头：{n['body_time']}；系统：{n['system_time']}；命名日期字段：{n['explicit_dates']}\n\n来源键：{n['record_key']}；source_record_id：{n['source_record_id']}；source_file：{n['source_file']}；row：{n['source_row']}\n")
        for e in a['evidence']:cards.append(f"\n> {e['quote']}\n\n原文偏移：{e['start']}–{e['end']}\n")
        cards.append('\n反查程序：\n\n| 标签 | 位置/时间角色 | 操作对象 | 自动候选 |\n|---|---|---|---|\n')
        for h in hi[n['record_key']]:cards.append(f"| {h['label']} | {h['section']} / {h['role']} | {h['target']} | {h['candidate_eligible']} |\n")
        cards.append('\n双向邻域（只按正文优先观察时钟排序，不认证真实先后）：\n')
        for x in neighbors:
            gap=(observed_time(x)-t).total_seconds()/3600
            cards.append(f"\n- {gap:+.2f}h：{x['document_type']}｜正文头{x['body_time']}｜系统{x['system_time']}｜{x['record_key']}\n")
            for h in hi[x['record_key']]:
                if h['candidate_eligible']:cards.append(f"\n  > {h['sentence']}\n")
        cards.append('\n逆向工程结论：'+a['engineering_implication']+'\n')
        cards.append('\n<details><summary>不按标签筛选的同患者前后7天完整文书邻域（可跨visit）</summary>\n')
        for x in independent:
            cards.append(f"\n### {x['document_type']}｜{x['record_key']}\n\nvisit={x['visit_id']}；正文头={x['body_time']}；系统={x['system_time']}；日期字段={x['explicit_dates']}\n\n{x['text']}\n")
        cards.append('\n</details>\n')
        cards.append('\n<details><summary>完整源文书</summary>\n\n'+text+'\n\n</details>\n')
    summary={'review_records':len(ledger),'review_patients':len({r['patient_uid'] for r in ledger}),
             'semantic_classes':dict(Counter(r['semantic_class'] for r in ledger)),
             'sampling':'Purposeful diagnostic cases, informed by earlier source inspection; rules iterated on these cases. Not independent validation.',
             'analyst_supported_records':sum(r['semantic_class'].startswith('SUPPORTED_') for r in ledger),
             'source_quote_offsets_verified':True}
    write(folder/'semantic_review_ledger.json',ledger);write(folder/'semantic_review_aggregate.json',summary)
    (folder/'双向语义决策记录样本-v0.3-20261020.md').write_text(''.join(cards))
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
