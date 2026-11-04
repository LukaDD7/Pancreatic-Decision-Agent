"""Resolve independently supplied annotation quotes; never generate labels or adjudicate."""
import argparse
import copy
import hashlib
import json
from pathlib import Path


def compile_annotation(bundle, annotation):
    result = copy.deepcopy(annotation)
    docs = {(d['patient_uid'], d['source_id']): d for d in bundle['documents']}
    if len(docs) != len(bundle['documents']): raise ValueError('duplicate source documents')
    if not result.get('annotator_ids'): raise ValueError('independent annotator identity required')
    if result.get('origin') != 'independent_human': raise ValueError('model outputs cannot be human annotation')
    if result.get('annotation_status') == 'ADJUDICATED':
        if len(set(result['annotator_ids'])) < 2 or not result.get('adjudicator_id'):
            raise ValueError('adjudicated gold requires independent annotators and adjudication provenance')
    seen = set()
    for mention in result['mentions']:
        if mention['id'] in seen: raise ValueError('duplicate annotation mention')
        seen.add(mention['id'])
        ref = mention.get('anchor', {})
        patient = ref.get('patient_uid')
        choices = [d for (p,s),d in docs.items() if s == ref.get('source_id') and (patient is None or patient == p)]
        if len(choices) != 1: raise ValueError('annotation source ambiguous or missing')
        doc = choices[0]; text = doc['text']; quote = ref.get('quote')
        if hashlib.sha256(text.encode()).hexdigest() != doc['source_sha256']:
            raise ValueError('annotation source hash mismatch')
        if not quote: raise ValueError('verbatim annotation quote required')
        positions = [i for i in range(len(text)) if text.startswith(quote,i)]
        if len(positions) != 1: raise ValueError('annotation quote absent or ambiguous; expand original quote')
        lo = positions[0]
        mention['anchor'] = {'patient_uid':doc['patient_uid'],'source_id':doc['source_id'],
            'source_sha256':doc['source_sha256'],'span':[lo,lo+len(quote)],'quote':quote}
        if mention.get('kind') not in {'DecisionMention','EvidenceMention','ActionMention','FeedbackMention'}:
            raise ValueError('unknown annotation role')
    if seen != set(result['mention_to_event']): raise ValueError('each human mention needs event membership')
    if any(e['from'] not in seen or e['to'] not in seen for e in result['relations']):
        raise ValueError('annotation relation endpoint not labeled')
    # Scope completeness is a human attestation, not inferred from a filled form.
    if result.get('annotation_status') == 'ADJUDICATED' and not result.get('complete_within_declared_scope'):
        raise ValueError('incomplete scope cannot be evaluated as closed-world gold')
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sources',type=Path,required=True);p.add_argument('--annotation',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    repo=Path(__file__).resolve().parents[3]
    if not any(a.output.resolve().is_relative_to(repo/d) for d in ['private','outputs']):p.error('human annotation must remain private')
    result=compile_annotation(json.loads(a.sources.read_text()),json.loads(a.annotation.read_text()))
    with a.output.open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2)


if __name__=='__main__':main()
