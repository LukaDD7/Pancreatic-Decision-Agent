"""Symmetric pre-adjudication agreement; neither annotator is treated as gold."""
import argparse
from collections import defaultdict
import json
from pathlib import Path

from .core import digest
from .evaluate import _identity, _in_scope, _clusters, _b3, coref_pairs, maximum_assignment, DD, PROCESS


def compare(a, b):
    for x in [a,b]:
        if x.get('annotation_status') != 'INDEPENDENT_COMPLETE' or not x.get('complete_within_declared_scope'):
            raise ValueError('IAA requires completed independent annotations before adjudication')
        if x.get('origin') != 'independent_human' or not x.get('annotator_ids'):
            raise ValueError('independent annotation provenance required')
        if x.get('scope',{}).get('kind') != 'source_windows':raise ValueError('explicit source scope required')
        if any(not all(k in w for k in ['patient_uid','source_id','source_sha256','start','end'])
               or not 0 <= w['start'] < w['end'] for w in x['scope'].get('windows',[])):
            raise ValueError('invalid independent annotation scope window')
        if not x['scope'].get('windows') or not all(_in_scope(m,x['scope']) for m in x['mentions']):
            raise ValueError('annotation mentions must fall within nonempty declared source scope')
    if set(a['annotator_ids']) & set(b['annotator_ids']):raise ValueError('independent annotators must differ')
    if digest(a['scope']) != digest(b['scope']):raise ValueError('IAA requires identical source scope')
    am,bm=({m['id']:m for m in x['mentions']} for x in [a,b])
    at={m:digest(_identity(v)) for m,v in am.items()};bt={m:digest(_identity(v)) for m,v in bm.items()}
    if len(set(at.values())) != len(at) or len(set(bt.values())) != len(bt):raise ValueError('ambiguous duplicate annotation span/type')
    if set(am)!=set(a['mention_to_event']) or set(bm)!=set(b['mention_to_event']):raise ValueError('incomplete human event membership')
    ac,ax=_clusters(am,a['mention_to_event'],at);bc,bx=_clusters(bm,b['mention_to_event'],bt)
    ais,bis=sorted(ac),sorted(bc)
    w=[[len(ac[i]&bc[j])/len(ac[i]|bc[j]) if ax[i]==bx[j] else 0 for j in bis] for i in ais]
    match={ais[i]:bis[j] for i,j in maximum_assignment(w) if w[i][j]>0}
    amap={m:'B:'+match[e] if e in match else 'A:'+e for m,e in a['mention_to_event'].items()}
    bmap={m:'B:'+e for m,e in b['mention_to_event'].items()}
    def rels(x,mapping,labels):
        return {(e['stage'],mapping[e['from']],mapping[e['to']],e['relation']) for e in x['relations'] if e['relation'] in labels}
    def agree(labels):
        aa,bb=rels(a,amap,labels),rels(b,bmap,labels)
        return {'A_positive_edges':len(aa),'B_positive_edges':len(bb),'shared_positive_edges':len(aa&bb),
                'symmetric_f1':2*len(aa&bb)/(len(aa)+len(bb)) if aa or bb else None}
    ad={at[m]:a['mention_to_event'][m] for m in am if am[m]['kind']=='DecisionMention'}
    bd={bt[m]:b['mention_to_event'][m] for m in bm if bm[m]['kind']=='DecisionMention'}
    ap,bp=coref_pairs(ad),coref_pairs(bd)
    common=set(at.values())&set(bt.values());field_results={}
    ai={v:k for k,v in at.items()};bi={v:k for k,v in bt.items()}
    for field in ['management_question','assessment','intended_action','condition','temporality']:
        values=[]
        for token in common:
            av=am[ai[token]].get(field) if field=='temporality' else am[ai[token]].get('signature',{}).get(field)
            bv=bm[bi[token]].get(field) if field=='temporality' else bm[bi[token]].get('signature',{}).get(field)
            if isinstance(av,dict):av={k:v for k,v in av.items() if k!='supports'}
            if isinstance(bv,dict):bv={k:v for k,v in bv.items() if k!='supports'}
            if av is not None and bv is not None:values.append(av==bv)
        field_results[field]={'compared':len(values),'exact_agreement':sum(values)/len(values) if values else None}
    return {'purpose':'pre-adjudication agreement, not accuracy or clinical Reference',
        'mention_span_type_agreement':{'A_mentions':len(am),'B_mentions':len(bm),'shared':len(common),
            'symmetric_f1':2*len(common)/(len(am)+len(bm)) if am or bm else None},
        'decision_coreference_pair_agreement':{'shared_pairs':len(ap&bp),'A_pairs':len(ap),'B_pairs':len(bp),
            'symmetric_f1':2*len(ap&bp)/(len(ap)+len(bp)) if ap or bp else None},
        'b3_common_decision_mentions':_b3(ad,bd),
        'decision_relation_agreement':agree(DD),'process_link_agreement':agree(PROCESS),
        'exact_field_agreement_on_shared_mentions':field_results,
        'uncertain_relations_A':sum(e['relation']=='uncertain' for e in a['relations']),
        'uncertain_relations_B':sum(e['relation']=='uncertain' for e in b['relations']),
        'limitations':'Exact span/type agreement; event alignment is one-to-one and relation-blind. No kappa over a presumed negative-pair universe. Free-text exact agreement is not semantic agreement.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--a',type=Path,required=True);p.add_argument('--b',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    x=p.parse_args();repo=Path(__file__).resolve().parents[3]
    if not any(x.output.resolve().is_relative_to(repo/d) for d in ['private','outputs']):p.error('IAA output must remain private')
    result=compare(json.loads(x.a.read_text()),json.loads(x.b.read_text()))
    with x.output.open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2)


if __name__=='__main__':main()
