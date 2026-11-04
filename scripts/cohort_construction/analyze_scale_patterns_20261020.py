"""Post-scale diagnostics and explicitly separate named-operation sensitivity.

No frozen labels or P1 memberships are overwritten. Private notes are required.
The added dictionary is fitted on this exploratory sample; its yield is not an
independent accuracy estimate. All outputs remain private until aggregated.
"""
from pathlib import Path
from collections import Counter, defaultdict
import datetime as dt, hashlib, json, random, re
from scale_text_type_time_20261020 import CORE, SUPPORT, FAMILIES, NONTARGET_TITLE, HISTORY_MARKER, START, END, clock, dist, parse_dt, visitkey, gap_summary, write

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'local_artifacts/p2_scale_20261020'
OP=re.compile(r'胰(?:十二指肠|体尾|体|尾|头|腺)[^。；;\n]{0,18}(?:切除术|根治术)|全胰(?:腺)?切除(?:术)?|Whipple(?:手术|术)?',re.I)
INTENT=re.compile(r'拟施手术名称和手术方式[：:]?|拟施手术名称[：:]?|拟实施|拟施行|拟行|计划|建议|考虑(?:行|施行)|拟')
NEW='PLAN_NAMED_PANCREAS_RESECTION'


def sentence(text,start,end):
    a=max([text.rfind(c,0,start) for c in '。；;\n'])+1
    bs=[text.find(c,end) for c in '。；;\n'];bs=[b for b in bs if b>=0]
    b=min(bs) if bs else len(text)
    return re.sub(r'\s+','',text[a:b])


def main():
    selection=json.loads((OUT/'selection.json').read_text())['selected']
    ordered=[r['patient_uid'] for r in selection];aliases={r['patient_uid']:'S%03d'%r['rank'] for r in selection}
    notes=[json.loads(s) for s in (OUT/'clinical_notes.jsonl').read_text().splitlines()]
    images=defaultdict(dict)
    manifest=json.loads((ROOT/'local_artifacts/source_cache_20261020/image_cache_manifest.json').read_text())
    for meta in manifest['files'].values():
        for line in Path(meta['cached_path']).read_text().splitlines():
            x=json.loads(line);t=parse_dt(x.get('exam_datetime'));method=str(x.get('exam_method') or '')
            if not t or not re.search(r'CT|MR|磁共振',method,re.I) or not (x.get('original_findings') or x.get('original_impression')):continue
            key=(str(x.get('exam_no_raw') or ''),str(t),'MR' if re.search(r'MR|磁共振',method,re.I) else 'CT')
            images[x['patient_uid']][key]={'exam_time':t,'method':method,'exam_key':key,'index_report_uid':x.get('index_report_uid')}
    ni={r['record_key']:r for r in notes}
    for r in notes:
        for k in ['body_time','system_time','admission_time','discharge_time']:r[k]=parse_dt(r[k])
    hits=json.loads((OUT/'label_hits.json').read_text())
    hi=defaultdict(list)
    for h in hits:hi[h['record_key']].append(h)
    newhits=[]
    for r in notes:
        text=r['text']
        for m in OP.finditer(text):
            # Same clause, bounded intent-to-operation relation; no outcome inference.
            left=max([text.rfind(c,0,m.start()) for c in '。；;\n'])+1
            before=text[max(left,m.start()-80):m.start()]
            intents=list(INTENT.finditer(before))
            if not intents:continue
            local=before[intents[-1].start():]
            if re.search(r'术后|切除后|已于|曾于',local):continue
            start=m.start()-len(before)+intents[-1].start()
            past=bool(HISTORY_MARKER.search(text[max(0,start-30):start]))
            newhits.append({'patient_uid':r['patient_uid'],'record_key':r['record_key'],'label':NEW,'start':start,'end':m.end(),'match':text[start:m.end()],'past_marker':past,'context':text[max(0,start-50):m.end()+100]})
    for h in newhits:hi[h['record_key']].append(h)
    for r in notes:
        r['supplement_labels']=r['labels']+([NEW] if any(h['label']==NEW for h in hi[r['record_key']]) else [])
        r['supplement_usable']=r['usable_labels']+([NEW] if any(h['label']==NEW and not h['past_marker'] for h in hi[r['record_key']]) else [])

    prior=json.loads((OUT/'candidate_points.json').read_text())
    points=[p for p in prior if p['clock']=='body_preferred' and p['lookback_hours']==48]
    fingerprints=defaultdict(set);record_fps=defaultdict(set)
    for r in notes:
        for h in hi[r['record_key']]:
            if h['label'] not in CORE:continue
            fp=(r['patient_uid'],h['label'],sentence(r['text'],h['start'],h['end']))
            fingerprints[fp].add(visitkey(r));record_fps[r['record_key']].add(fp)
    flagged=[p for p in points if any(len(fingerprints[fp])>1 for fp in record_fps[p['record_key']])]
    questions=[h for h in hits if h['label']=='INDICATION_YES' and re.search(r'是否|有无',ni[h['record_key']]['text'][max(0,h['start']-4):h['start']])]
    output={'supplement_pattern':{'operation':OP.pattern,'intent':INTENT.pattern,'max_preceding_characters':80,'same_clause':True},'samples':{},'literal_yes_question_scope_flag':{'occurrences':len(questions),'notes':len({h['record_key'] for h in questions}),'patients':len({h['patient_uid'] for h in questions})},'original_candidate_cross_visit_identical_judgment_sentence_flag':{'candidate_points':len(flagged),'patients':len({p['patient_uid'] for p in flagged}),'action':'Flag only; identical wording is not proof that a new decision did not occur.'}}
    supplementary_points=[];anchor_pairs=[]
    for n in [100,300]:
        subset=set(ordered[:n]);rr=[r for r in notes if r['patient_uid'] in subset]
        newly={r['patient_uid'] for r in rr if NEW in r['supplement_labels']}
        corepatients={r['patient_uid'] for r in rr if CORE.intersection(r['labels'])}
        data={'named_resection_notes':sum(NEW in r['supplement_labels'] for r in rr),'named_resection_patients':len(newly),'named_resection_patients_without_original_core_anywhere':len(newly-corepatients),'clocks':{}}
        for which in ['body_preferred','system']:
            window=[r for r in rr if clock(r,which) and START<=clock(r,which)<=END]
            eligible=[r for r in window if r['clinical_type'] and r['family'] in FAMILIES and not NONTARGET_TITLE.search(r['document_type'])]
            cc=CORE|{NEW};ss=SUPPORT|{NEW};anchors={};support=defaultdict(list)
            for r in eligible:
                if ss.intersection(r['supplement_usable']):support[(r['patient_uid'],visitkey(r))].append(r)
                for lab in sorted(cc.intersection(r['supplement_usable'])):
                    key=(r['patient_uid'],visitkey(r),lab)
                    if key not in anchors or clock(r,which)<clock(anchors[key],which):anchors[key]=r
            variants={}
            for hours in [24,48,168]:
                chosen={}
                for (u,v,lab),anchor in anchors.items():
                    options=[r for r in support[(u,v)] if 0<=(clock(anchor,which)-clock(r,which)).total_seconds()/3600<=hours]
                    picked=min(options,key=lambda r:(clock(r,which),r['record_key'])) if options else anchor
                    chosen[picked['record_key']]=picked
                counts=Counter(r['patient_uid'] for r in chosen.values())
                variants[str(hours)+'h']={'total':len(chosen),'all_patients':dist([counts[u] for u in ordered[:n]]),'positive_patients':dist([v for v in counts.values() if v]),'patients_zero':sum(counts[u]==0 for u in subset),'patients_ge2':sum(counts[u]>=2 for u in subset)}
                if n==300:
                    supplementary_points.extend({'patient_uid':r['patient_uid'],'record_key':r['record_key'],'clock':which,'lookback_hours':hours,'time':str(clock(r,which)),'labels':r['supplement_usable'],'family':r['family']} for r in chosen.values())
            # First pre-op witness per visit, merging briefing/discussion only here.
            timepatterns={}
            for lab in ['PLAN_CURATIVE_PANCREAS',NEW,'INDICATION_NO','NAT_INTENT']:
                aa={}
                afams={'术前小结','术前讨论'} if lab in {'PLAN_CURATIVE_PANCREAS',NEW} else {'首次上级查房'}
                for r in window:
                    if r['clinical_type'] and lab in r['supplement_labels'] and r['family'] in afams:
                        key=(r['patient_uid'],visitkey(r))
                        if key not in aa or clock(r,which)<clock(aa[key],which):aa[key]=r
                pairs=defaultdict(list)
                for (u,v),a in aa.items():
                    for kind,same_label in [('earlier_same_label_nonpreop',True),('earlier_first_senior',False)]:
                        pool=[r for r in window if r['patient_uid']==u and visitkey(r)==v and r['record_key']!=a['record_key'] and clock(r,which)<=clock(a,which) and (r['family'] in {'首次上级查房','首次病程','会诊记录','其他查房','入院记录'} if same_label else r['family']=='首次上级查房') and (not same_label or lab in r['supplement_labels'])]
                        if not pool:continue
                        r=max(pool,key=lambda r:clock(r,which));gap=round((clock(a,which)-clock(r,which)).total_seconds()/3600,3)
                        pair={'sample_n':n,'clock':which,'patient_uid':u,'label':lab,'anchor_record':a['record_key'],'earlier_record':r['record_key'],'earlier_family':r['family'],'kind':kind,'gap_hours':gap}
                        pairs[kind].append(pair);anchor_pairs.append(pair)
                    for kind,require_abdomen in [('earlier_CT_MR_exam',False),('earlier_abdominal_CT_MR_exam',True)]:
                        pool=[im for im in images[u].values() if im['exam_time']<=clock(a,which) and (not require_abdomen or re.search(r'胰|上腹|腹部|肝|胆|脾|肾|腹盆',im['method']))]
                        if not pool:continue
                        im=max(pool,key=lambda im:im['exam_time']);gap=round((clock(a,which)-im['exam_time']).total_seconds()/3600,3)
                        pair={'sample_n':n,'clock':which,'patient_uid':u,'label':lab,'anchor_record':a['record_key'],'kind':kind,'exam_key':im['exam_key'],'method':im['method'],'gap_hours':gap}
                        pairs[kind].append(pair);anchor_pairs.append(pair)
                timepatterns[lab]={'anchors':len(aa),'pairs':{k:gap_summary(v,len(aa)) for k,v in pairs.items()}}
            data['clocks'][which]={'variants':variants,'preop_or_senior_time_patterns':timepatterns}
        output['samples'][str(n)]=data

    # Deterministic stratified source-review packet. These are selected cases,
    # not an estimate of sensitivity/PPV or an independent clinical adjudication.
    rng=random.Random(20261020);chosen={}
    def add(kind,rs,k):
        pool=sorted(rs,key=lambda r:r['record_key'])
        for r in rng.sample(pool,min(k,len(pool))):chosen.setdefault(r['record_key'],{'row':r,'strata':[]})['strata'].append(kind)
    for label in ['PLAN_CURATIVE_PANCREAS','INDICATION_YES','INDICATION_NO','PAST_INDICATION_NO','NAT_INTENT','SURGERY_INTENT_GENERIC','FITNESS_NO_CONTRAINDICATION']:
        add(label,[r for r in notes if label in r['labels']],2)
    covered={p['patient_uid'] for p in points}
    add('ZERO_OLD_CORE_BUT_NAMED_RESECTION',[r for r in notes if r['patient_uid'] not in covered and NEW in r['supplement_labels'] and r['family'] in {'术前小结','术前讨论'}],4)
    pc=Counter(p['patient_uid'] for p in points)
    for uid,count in pc.most_common(3):
        pp=sorted([p for p in points if p['patient_uid']==uid],key=lambda p:p['time'])
        for p in [pp[0],pp[-1]]:chosen.setdefault(p['record_key'],{'row':ni[p['record_key']],'strata':[]})['strata'].append('HIGH_CANDIDATE_COUNT_FIRST_LAST')
    packet=['# 私有抽样卡：300人扩样\n\n仅本地。包含原文；样本序号不是匿名化保证。分层目的抽样，不用于估计准确率。\n']
    for i,(key,item) in enumerate(chosen.items(),1):
        r=item['row'];packet.append(f"\n## C{i:02d}｜{aliases[r['patient_uid']]}｜{','.join(item['strata'])}\n\n文书：{r['document_type']}；visit：{r['visit_id']}；正文头：{r['body_time']}；系统：{r['system_time']}；source：{r['source_record_id']}；key：{key}\n")
        for h in hi[key]:packet.append(f"\n- {h['label']}，past={h['past_marker']}，offset={h['start']}:{h['end']}：{h['context']}\n")
        packet.append('\n<details><summary>完整文书</summary>\n\n'+r['text']+'\n\n</details>\n')
    (OUT/'扩样分层原文样本卡-20261020.md').write_text(''.join(packet))
    write(OUT/'diagnostics.json',output);write(OUT/'supplement_candidate_points.json',supplementary_points);write(OUT/'supplement_label_hits.json',newhits);write(OUT/'supplement_anchor_pairs.json',anchor_pairs)
    write(OUT/'source_review_selection.json',[{'card':f'C{i:02d}','record_key':key,'strata':item['strata']} for i,(key,item) in enumerate(chosen.items(),1)])
    output['script_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    output['original_summary_sha256']=hashlib.sha256((OUT/'summary.json').read_bytes()).hexdigest()
    write(OUT/'diagnostics.json',output)
    # Integrity checks against original exact text; spans are replayable.
    for h in hits+newhits:assert ni[h['record_key']]['text'][h['start']:h['end']]==h['match']
    for n in ['100','300']:
        for which in ['body_preferred','system']:
            assert output['samples'][n]['clocks'][which]['variants']['48h']['all_patients']['n']==int(n)
    print(json.dumps({'diagnostics':{n:{'named_patients':d['named_resection_patients'],'missed_old_core':d['named_resection_patients_without_original_core_anywhere'],'48h':d['clocks']['body_preferred']['variants']['48h']} for n,d in output['samples'].items()},'repeat_flag':output['original_candidate_cross_visit_identical_judgment_sentence_flag'],'source_cards':len(chosen)},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
