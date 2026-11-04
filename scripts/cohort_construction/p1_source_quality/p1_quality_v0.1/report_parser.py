"""Deterministic text representation checks, no clinical resectability inference."""
import datetime as dt
import hashlib
import html
import re
import unicodedata

def normalize(value):
    text=unicodedata.normalize('NFKC',html.unescape(str(value or '')))
    text=text.replace('\\n','\n').replace('\\r','\n').replace('\r','\n')
    text=text.replace('小于等于','<=').replace('大于等于','>=').replace('不超过','<=').replace('小于','<').replace('大于','>')
    return text

def compact(value):
    # Used for raw-source containment, not to generate or impute medical content.
    return re.sub(r'\s+','',normalize(value))

def dateof(value):
    m=re.search(r'(19\d{2}|20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})',str(value or ''))
    if not m:return ''
    try:return dt.date(*map(int,m.groups())).isoformat()
    except ValueError:return ''

def basename(value):
    return str(value or '').replace('\\','/').split('/')[-1].replace('.record_clusters.jsonl','')

def section(text,name,next_names):
    pattern=re.escape(name) if isinstance(name,str) else name
    m=re.search(r'(?:'+pattern+r')\s*[:：]',text)
    if not m:return None
    end=re.search(r'(?:'+ '|'.join(re.escape(n) for n in next_names)+r')\s*[:：]',text[m.end():])
    stop=m.end()+end.start() if end else len(text)
    return {'start':m.end(),'end':stop,'text':text[m.end():stop].strip()}

PLACEHOLDER=re.compile(r'^(?:[\s。.;；:_\-/□☐]*|未填|未填写|待填|待补|略|NA|N/A|null|None)$',re.I)
UNCERTAIN=re.compile(r'不能(?:明确|确定|排除)|无法(?:明确|判断|确定|测量|评估|评价)|难以(?:判断|确定|评估|辨认)|不明确|不确定|不详|关系不清|界面不清|分界不清|界限不清|不除外|待排|可疑|疑似|[?？]')
NEG=re.compile(r'无(?:明显)?(?:接触|受侵|侵犯|包绕|累及)|未见(?:明显)?(?:接触|受侵|侵犯|包绕|累及)|未受(?:累|侵)|无受累|(?:分界|界限|界面)(?:尚)?清(?:楚|晰)|脂肪间隙(?:尚)?(?:清晰|存在)')
POS=re.compile(r'接触|受侵|侵犯|包绕|包裹|累及|受压|压迫|粘连|黏连|附着|贴附|紧贴')
ANGLE=re.compile(r'(?P<op><=|>=|≤|≥|<|>|=)?\s*(?P<lo>\d{1,3})(?:\s*[-~—至]\s*(?P<hi>\d{1,3}))?\s*°')

def angle_value(text):
    m=ANGLE.search(text)
    if not m:return None
    v=int(m['lo']);hi=int(m['hi']) if m['hi'] else None
    if not 0<=v<=360 or (hi is not None and not v<=hi<=360):return {'invalid':True,'raw':m.group()}
    op=m['op'] or '='
    return {'op':op,'lo':v,'hi':hi,'raw':m.group()}

def angle_interval(a):
    if not a or a.get('invalid'):return None
    v=a['lo'];op=a['op'];hi=a.get('hi')
    if hi is not None:return (v,hi,True,True)
    if op in ['<','<=','≤']:return (0,v,True,op!='<')
    if op in ['>','>=','≥']:return (v,360,op!='>',True)
    return (v,v,True,True)

def disjoint(a,b):
    x=angle_interval(a);y=angle_interval(b)
    if not x or not y:return False
    if x[1]<y[0] or y[1]<x[0]:return True
    if x[1]==y[0] and not(x[3] and y[2]):return True
    if y[1]==x[0] and not(y[3] and x[2]):return True
    return False

def relationship(text,in_contact_summary=False):
    t=normalize(text);a=angle_value(t)
    if PLACEHOLDER.fullmatch(t.strip()):
        return ('CONTACT_OR_INVASION' if in_contact_summary and not t.strip() else 'MISSING'),a
    if a and a.get('invalid'):return 'UNRECOGNIZED_VALUE',a
    if UNCERTAIN.search(t):return 'EXPLICIT_INDETERMINATE',a
    if NEG.search(t):return 'NO_CONTACT_OR_INVASION',a
    if re.search(r'接触(?:面|角度)\s*[:：]?\s*无(?:[。.;；]|$)',t) or (in_contact_summary and t.strip(' 。.;；')=='无'):return 'NO_CONTACT_OR_INVASION',a
    if a and a['op']=='=' and a['lo']==0 and a.get('hi') is None:return 'NO_CONTACT_OR_INVASION',a
    if re.search(r'接触(?:面|角度)\s*[:：]?\s*[。.;；]?\s*$',t) and not a:return 'EMPTY_ANGLE_FIELD',a
    if POS.search(t) or a or (in_contact_summary and t.strip()):return 'CONTACT_OR_INVASION',a
    if re.search(r'通畅|显影|显示清晰|未见狭窄|未见闭塞',t):return 'PATENCY_ONLY',a
    return 'UNRECOGNIZED_VALUE',a

def vessel_evidence(text,vessels,source):
    # Numbered template slots and punctuated narrative clauses are handled alike.
    # A comma-separated list in a contact-summary supplies contact, never an unmentioned negative.
    evidence={k:[] for k in vessels}
    summary=section(text,'与血管接触',['可疑淋巴结','远处转移','其它诊断','其他诊断','其它','其他']) or section(text,'血管接触',['可疑淋巴结','远处转移','其它诊断','其他诊断','其它','其他'])
    for m in re.finditer(r'[^。；;\n]+',text):
        clause=m.group();matches=[]
        if re.search(r'(?:可疑)?淋巴结\s*[:：]',clause):continue
        for key,aliases in vessels.items():
            pat='|'.join(re.escape(a) if not a.isascii() else r'(?<![A-Za-z0-9_])'+re.escape(a)+r'(?![A-Za-z0-9_])' for a in aliases)
            for hit in re.finditer(pat,clause,re.I):matches.append((hit.start(),hit.end(),key))
        # Avoid duplicate overlapping aliases, e.g. 腹腔干 and 腹腔干动脉.
        matches.sort(key=lambda x:(x[0],-(x[1]-x[0])))
        hits=[]
        for hit in matches:
            if hits and hit[0]<hits[-1][1]:continue
            hits.append(hit)
        for i,(a,b,key) in enumerate(hits):
            stop=hits[i+1][0] if i+1<len(hits) else len(clause)
            tail=clause[b:stop].strip(' ,，、和及与:：')
            in_summary=bool(summary and summary['start']<=m.start()+a<summary['end'])
            state,angle=relationship(tail,in_summary)
            if re.search(r'(?:可疑|疑似|不除外)\s*$',clause[:a]) and state=='CONTACT_OR_INVASION':state='EXPLICIT_INDETERMINATE'
            # Explicit shared predicates after a list apply to the named members.
            if state in ['MISSING','UNRECOGNIZED_VALUE'] and i+1<len(hits) and not in_summary:
                between=clause[b:hits[-1][1]]
                if re.fullmatch(r'[\s、,，和及与\u4e00-\u9fffA-Za-z]*',between):
                    shared=clause[hits[-1][1]:]
                    if re.match(r'\s*(?:均|皆|都)',shared):state,angle=relationship(shared)
            if re.search(r'既往|此前|上次|原先',clause[:a]):
                # Historical comparison clauses are retained separately, not used to fill current slots.
                continue
            qstart=max(0,a-12);qend=min(len(clause),stop+10)
            evidence[key].append({'source_field':source,'normalized_start':m.start()+a,'normalized_end':m.start()+stop,
                                  'normalized_quote_start':m.start()+qstart,'normalized_quote_end':m.start()+qend,
                                  'quote':clause[qstart:qend],'state':state,'angle':angle})
    return evidence

def resolve_vessel(items):
    states={e['state'] for e in items}
    substantive=states&{'NO_CONTACT_OR_INVASION','CONTACT_OR_INVASION','EXPLICIT_INDETERMINATE'}
    conflict='NO_CONTACT_OR_INVASION' in substantive and 'CONTACT_OR_INVASION' in substantive
    aa=[e['angle'] for e in items if e['angle'] and not e['angle'].get('invalid')]
    conflict=conflict or any(disjoint(a,b) for i,a in enumerate(aa) for b in aa[i+1:])
    if conflict:return {'state':'CONFLICT','represented':False,'evidence':items}
    # Malformed explicit slot is not hidden by a second incidental mention.
    if 'EMPTY_ANGLE_FIELD' in states:return {'state':'EMPTY_ANGLE_FIELD','represented':False,'evidence':items}
    if substantive:
        chosen='EXPLICIT_INDETERMINATE' if 'EXPLICIT_INDETERMINATE' in substantive else next(iter(sorted(substantive)))
        return {'state':chosen,'represented':True,'angle_unquantified':'CONTACT_OR_INVASION' in substantive and not aa,'evidence':items}
    return {'state':'PATENCY_ONLY' if 'PATENCY_ONLY' in states else 'UNRECOGNIZED_VALUE' if items else 'MISSING','represented':False,'evidence':items}

def field_value(text,pattern):
    m=re.search(r'(?:'+pattern+r')\s*[:：]\s*([^\n。；;]*)',text)
    if not m:return {'state':'MISSING','represented':False,'quote':''}
    value=m.group(1).strip()
    if not value or PLACEHOLDER.fullmatch(value):return {'state':'MISSING','represented':False,'quote':m.group()}
    if UNCERTAIN.search(value):state='EXPLICIT_INDETERMINATE'
    elif re.search(r'未见|无病变|无(?:明确|明显)?转移|^无(?:明显)?$|阴性|未发现',value):state='NEGATIVE'
    elif re.search(r'转移|种植',value):state='METASTATIC_STATEMENT'
    elif re.search(r'良性|囊肿|血管瘤',value):state='BENIGN_OR_POSSIBLY_BENIGN'
    elif re.search(r'^有|结节|病变|肿块',value):state='PRESENT_UNCLASSIFIED'
    else:state='UNRECOGNIZED_VALUE'
    return {'state':state,'represented':state!='UNRECOGNIZED_VALUE','quote':m.group(),'value':value}

def parse_report(record,rules):
    finding=normalize(record.get('original_findings'));impression=normalize(record.get('original_impression'))
    combined=finding+'\n'+impression
    structured=all(re.search(re.escape(h)+r'\s*[:：]',combined) for h in ['肿块评估','动脉评估','静脉评估'])
    tumour=section(finding,'肿块评估',['胰胆管评估','动脉评估','静脉评估','其它评估','其他评估'])
    # Narrative fallback is bounded to pancreas-containing clauses, not all lesion measurements.
    tumour_text=tumour['text'] if tumour else '\n'.join(m.group() for m in re.finditer(r'[^。；;\n]*(?:胰头|胰颈|胰体|胰尾|钩突|胰腺)[^。；;\n]*',combined))
    lesion_context=bool(re.search(r'肿块|肿瘤|占位|病灶|胰腺癌|PDAC|软组织(?:密度|影)|结节',tumour_text,re.I) or (tumour and re.search(r'位于|呈.{0,8}(?:密度|信号)|强化|弥散|DWI|T1WI|T2WI',tumour_text,re.I)))
    # Never borrow a node/liver/kidney measurement after the pancreatic description.
    size_text=re.split(r'淋巴结|肝内|肝脏|肾脏|脾脏|胆囊',tumour_text,maxsplit=1)[0]
    site=re.search(rules['lesion_site_pattern'],tumour_text)
    dim_matches=list(re.finditer(r'(\d+(?:\.\d+)?)\s*(mm|cm|毫米|厘米)?\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*(mm|cm|毫米|厘米)?(?:\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*(mm|cm|毫米|厘米)?)?',size_text,re.I))
    dim_matches=[m for m in dim_matches if any(m[i] for i in [2,4,6])]
    dims=dim_matches[0] if dim_matches else None
    one=re.search(r'(?:最大径|直径|长径|大小)(?:约|为|达|：|:|\s)*(\d+(?:\.\d+)?)\s*(mm|cm|毫米|厘米)',size_text,re.I)
    size={'state':'MISSING','represented':False,'quote':''}
    if dims:
        fallback=[dims[i] for i in [2,4,6] if dims[i]][-1]
        values=[float(dims[i])*(10 if (dims[i+1] or fallback).lower() in ['cm','厘米'] else 1) for i in [1,3,5] if dims[i]]
        size={'state':'NUMERIC' if all(v>0 for v in values) else 'INVALID','represented':all(v>0 for v in values),'values_mm':values,'quote':dims.group(),'measurement_count':len(dim_matches)}
    elif one:
        value=float(one[1]);factor=10 if one[2].lower() in ['cm','厘米'] else 1
        size={'state':'NUMERIC' if value>0 else 'INVALID','represented':value>0,'values_mm':[value*factor],'quote':one.group()}
    elif re.search(r'无法测量|难以测量|未见(?:明确|明显)?(?:肿块|病灶)|病灶(?:不再|难以)(?:显示|辨认)|病灶消失',tumour_text):
        size={'state':'EXPLICIT_NONMEASURABLE','represented':True,'quote':tumour_text[:250]}
    ev={k:[] for k in rules['required_vessels']}
    for source,text in [('original_findings',finding),('original_impression',impression)]:
        found=vessel_evidence(text,rules['required_vessels'],source)
        for k in ev:ev[k].extend(found[k])
    vessels={k:resolve_vessel(v) for k,v in ev.items()}
    distant=field_value(combined,r'远处转移')
    liver=field_value(combined,r'肝脏(?:病变)?')
    peritoneal=field_value(combined,r'腹膜或网膜结节|腹膜(?:及|、)网膜结节|腹膜转移')
    met_represented=distant['represented'] or (liver['represented'] and peritoneal['represented'])
    conflicts=[f'VESSEL_CONFLICT_{k}' for k,v in vessels.items() if v['state']=='CONFLICT']
    if len(dim_matches)>1:conflicts.append('MULTIPLE_LESION_MEASUREMENTS_REVIEW')
    if distant['state']=='NEGATIVE' and any(x['state']=='METASTATIC_STATEMENT' for x in [liver,peritoneal]):conflicts.append('METASTASIS_FIELD_CONFLICT')
    post=bool(re.search(rules['postoperative_context_pattern'],combined))
    method=normalize(record.get('exam_method'))
    modality='CT' if re.search(r'CT|计算机断层',method,re.I) else 'MR' if re.search(r'MR|磁共振',method,re.I) else 'OTHER'
    anatomical_method=bool(re.search(r'胰|腹|肝脏|MRCP',method,re.I))
    contrast=bool(re.search(r'增强|CTA|造影',method,re.I))
    return {'template_detected':structured,'modality':modality,'anatomical_method':anatomical_method,'contrast_explicit':contrast,
            'postoperative_context':post,'lesion_context':lesion_context,'lesion_site':{'represented':bool(site),'value':site.group() if site else None,'quote':tumour_text[:250]},
            'lesion_size':size,'vessels':vessels,'metastatic_domain':{'represented':bool(met_represented),'distant':distant,'liver':liver,'peritoneal':peritoneal},
            'conflicts':conflicts,'has_text':bool(combined.strip()),'normalized_text_sha256':hashlib.sha256(combined.encode()).hexdigest()}

def evaluate_report(record,parsed,provenance,rules):
    reasons=[]
    if parsed['modality']=='OTHER' or not parsed['anatomical_method']:return 'NOT_APPLICABLE',['OUT_OF_MODALITY_OR_BODY_SCOPE']
    if parsed['postoperative_context']:return 'REVIEW',['POSTOPERATIVE_OR_RECURRENCE_CONTEXT']
    if not parsed['has_text']:return 'NOT_MET',['NO_REPORT_TEXT']
    if not provenance['verified']:reasons.append(provenance['reason'])
    if not dateof(record.get('exam_datetime')):reasons.append('MISSING_EXAM_DATE')
    if not parsed['contrast_explicit']:reasons.append('CONTRAST_PROTOCOL_UNVERIFIED')
    if not parsed['lesion_context']:reasons.append('PANCREATIC_LESION_CONTEXT_NOT_REPRESENTED')
    if not parsed['lesion_site']['represented']:reasons.append('LESION_SITE_NOT_REPRESENTED')
    if not parsed['lesion_size']['represented']:reasons.append('LESION_SIZE_NOT_REPRESENTED')
    for k,v in parsed['vessels'].items():
        if not v['represented']:reasons.append(f'VESSEL_{k}_{v["state"]}')
    if not parsed['metastatic_domain']['represented']:reasons.append('METASTATIC_DOMAIN_NOT_REPRESENTED')
    reasons.extend(parsed['conflicts'])
    if not reasons:return 'PASS',[]
    # Partial or unrecognized source formats are reviewable, never clinical nonresectability.
    return 'REVIEW',sorted(set(reasons))
