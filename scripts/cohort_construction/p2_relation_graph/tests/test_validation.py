"""Validation infrastructure counterexamples; entirely fictional, never human gold."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.cohort_construction.model_api import ModelAPISettings
from scripts.cohort_construction.p2_relation_graph.core import digest
from scripts.cohort_construction.p2_relation_graph.evaluate import evaluate, maximum_assignment
from scripts.cohort_construction.p2_relation_graph.model import SemanticClient, ModelFailure
from scripts.cohort_construction.p2_relation_graph.review_workflow import ReviewLedger, summarize


def fixture():
    ids = ['m1','m2','m3','m4']
    mentions = [{'id': i, 'kind': 'DecisionMention'} for i in ids]
    membership = {i: 'E'+i[1:] for i in ids}
    graph = {'mentions': copy.deepcopy(mentions), 'membership': membership, 'edges': []}
    gold = {'annotation_status': 'ADJUDICATED', 'complete_within_declared_scope': True,
            'origin': 'synthetic_fixture', 'annotator_ids': ['fixture'],
            'scope': {'kind':'aligned_mentions','mention_ids':ids}, 'mentions': mentions,
            'mention_to_event': copy.deepcopy(membership), 'relations': []}
    return graph, gold


def edge(a='m1', b='m2', label='revise', score=.75):
    return {'stage':'typed_relation' if label in {'informs','implemented_by','evaluated_by','triggers_reassessment'} else 'decision_relation',
            'from':a, 'to':b, 'relation':label,'score':score,'status':'REVIEW'}


class EvaluationTests(unittest.TestCase):
    def test_EV001_representative_mention_does_not_change_event_edge(self):
        g,t=fixture();g['membership']['m2']='E1';t['mention_to_event']['m2']='E1'
        g['edges']=[edge('m2','m3')];t['relations']=[edge('m1','m3')]
        r=evaluate(g,t)
        self.assertEqual(r['typed_relation_edges']['true_positive'],1)
        self.assertEqual(r['candidate_edge_recall'],1)

    def test_EV002_spurious_edges_enter_precision_denominator(self):
        g,t=fixture();t['relations']=[edge()];g['edges']=[edge(),edge('m1','m3'),edge('m4','m2')]
        r=evaluate(g,t)['decision_relation_edges']
        self.assertEqual(r['false_positive'],2);self.assertAlmostEqual(r['precision'],1/3)

    def test_EV003_selective_curve_includes_all_accepted_edges(self):
        g,t=fixture();t['relations']=[edge()];g['edges']=[edge(),edge('m1','m3'),edge('m4','m2')]
        curve=next(c for c in evaluate(g,t)['coverage_precision_curve'] if c['threshold']==.75)
        self.assertEqual(curve['accepted_edges'],3);self.assertAlmostEqual(curve['precision'],1/3)

    def test_EV004_outside_scope_reported_separately(self):
        g,t=fixture();t['scope']['mention_ids']=['m1','m2','m3'];t['mentions']=t['mentions'][:3];t['mention_to_event'].pop('m4')
        t['relations']=[edge()];g['edges']=[edge(),edge('m4','m2')]
        r=evaluate(g,t);self.assertEqual(r['typed_relation_edges']['precision'],1)
        self.assertEqual(r['out_of_scope_prediction_edges'],1)
        t['scope']='unverifiable free text'
        with self.assertRaises(ValueError):evaluate(g,t)

    def test_EV005_unknown_gold_is_not_assumed_negative(self):
        g,t=fixture();g['edges']=[edge(),edge('m1','m3')];t['relations']=[edge(),edge('m1','m3','uncertain')]
        r=evaluate(g,t);c=r['coverage_precision_curve'][0]
        self.assertEqual(r['unassessed_prediction_edges'],1);self.assertIsNone(c['precision'])
        self.assertEqual(c['precision_lower_bound'],.5);self.assertEqual(c['precision_upper_bound'],1)

    def test_EV006_alignment_is_global_one_to_one_not_greedy(self):
        self.assertEqual(maximum_assignment([[.9,.8],[.85,.1]]),[(0,1),(1,0)])
        self.assertEqual(maximum_assignment([]),[])
        g,t=fixture();g['membership']={i:'merged' for i in g['membership']}
        r=evaluate(g,t);self.assertEqual(r['event_alignment']['matched_events'],1)
        self.assertLess(r['coreference_b3_common_mentions']['f1'],1)

    def test_EV007_duplicate_event_edge_counted_once(self):
        g,t=fixture();g['membership']['m2']='E1';t['mention_to_event']['m2']='E1'
        t['relations']=[edge('m1','m3')];g['edges']=[edge('m1','m3'),edge('m2','m3')]
        self.assertEqual(evaluate(g,t)['typed_relation_edges']['predicted'],1)

    def test_EV008_wrong_direction_and_self_loop_are_errors(self):
        g,t=fixture();t['relations']=[edge()];g['edges']=[edge('m2','m1'),edge('m1','m1')]
        r=evaluate(g,t)['typed_relation_edges'];self.assertEqual(r['false_positive'],2);self.assertEqual(r['false_negative'],1)

    def test_EV009_decision_and_process_metrics_are_separate(self):
        g,t=fixture();g['mentions'][2]['kind']=t['mentions'][2]['kind']='EvidenceMention'
        g['edges']=[edge('m1','m2','supersede'),edge('m3','m1','informs')]
        t['relations']=[edge(),edge('m3','m1','informs')]
        r=evaluate(g,t);self.assertEqual(r['decision_relation_edges']['f1'],0);self.assertEqual(r['process_link_edges']['f1'],1)

    def test_EV010_missing_gold_nodes_and_edges_remain_in_denominator(self):
        g,t=fixture();g['mentions']=g['mentions'][:2];g['membership']={k:v for k,v in g['membership'].items() if k in {'m1','m2'}}
        t['relations']=[edge('m1','m3')];r=evaluate(g,t)
        self.assertEqual(r['mention_detection_and_typing']['recall'],.5)
        self.assertEqual(r['candidate_edge_recall'],0);self.assertFalse(r['scope_graph_complete'])

    def test_EV011_source_identity_not_signature_ids_controls_alignment(self):
        g,t=fixture();g['mentions']=g['mentions'][:2];t['mentions']=t['mentions'][:2]
        t['mention_to_event']={k:v for k,v in t['mention_to_event'].items() if k in {'m1','m2'}}
        window={'patient_uid':'synthetic','source_id':'doc','source_sha256':'a'*64,'start':0,'end':100}
        t['scope']={'kind':'source_windows','windows':[window]};t['origin']='independent_human'
        t['annotator_ids']=['fixture-A','fixture-B'];t['adjudicator_id']='fixture-adjudicator'
        for i,(pm,gm) in enumerate(zip(g['mentions'],t['mentions'])):
            pm['anchor']=gm['anchor']={k:v for k,v in window.items() if k not in {'start','end'}}|{'span':[i*10,i*10+5]}
            pm['id']='new-'+pm['id']
        g['membership']={'new-m1':'P1','new-m2':'P2'};g['edges']=[edge('new-m1','new-m2')];t['relations']=[edge()]
        self.assertEqual(evaluate(g,t)['typed_relation_edges']['true_positive'],1)
        g['mentions'][1]['anchor']={**g['mentions'][1]['anchor'],'source_sha256':'b'*64}
        self.assertEqual(evaluate(g,t)['out_of_scope_prediction_mentions'],1)

    def test_EV012_auxiliary_clustering_part_of_graph_completeness(self):
        g,t=fixture()
        for m in g['mentions'][2:]+t['mentions'][2:]:m['kind']='EvidenceMention'
        t['mention_to_event']['m4']='E3'
        self.assertFalse(evaluate(g,t)['scope_graph_complete'])

    def test_EV013_invalid_gold_types_not_scored_as_valid_graph(self):
        g,t=fixture();t['relations']=[edge('m1','m2','informs')]
        with self.assertRaises(ValueError):evaluate(g,t)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.settings=ModelAPISettings('synthetic-key','https://example.invalid/v1','kimi-k3')
    def tearDown(self):self.tmp.cleanup()
    def client(self,mode):return SemanticClient(self.root,settings=self.settings,cache_mode=mode,replicate_id='test',retries=0)
    def mock(self,value=True):
        m=Mock(status_code=200);m.json.return_value={'id':'fake-id','model':'kimi-k3','choices':[{'finish_reason':'stop','message':{'content':json.dumps({'ok':value})}}]};return m

    def test_RP001_bypass_makes_identical_fresh_requests(self):
        with patch('requests.post',return_value=self.mock()) as post:
            for _ in range(3):self.client('bypass').ask('same',{}, {'type':'object'})
            self.assertEqual(post.call_count,3)
            bodies=[c.kwargs['json'] for c in post.call_args_list];self.assertEqual(bodies[0],bodies[1]);self.assertEqual(bodies[1],bodies[2])
        self.assertEqual(len(list((self.root/'responses').glob('*_raw.json'))),3)
        self.assertFalse(list(self.root.glob('*.json')))

    def test_RP002_read_only_never_calls_network(self):
        with patch('requests.post',return_value=self.mock()) as post:
            with self.assertRaises(ModelFailure):self.client('read_only').ask('same',{}, {'type':'object'})
            self.assertEqual(post.call_count,0)
            self.client('read_write').ask('same',{}, {'type':'object'})
            self.client('read_only').ask('same',{}, {'type':'object'})
            self.assertEqual(post.call_count,1)

    def test_RP003_refresh_keeps_prior_response(self):
        with patch('requests.post',return_value=self.mock(True)):
            self.client('read_write').ask('same',{}, {'type':'object'})
        with patch('requests.post',return_value=self.mock(False)):
            self.client('refresh').ask('same',{}, {'type':'object'})
        self.assertTrue(list((self.root/'responses').glob('prior_*.json')))
        with patch('requests.post') as post:
            self.assertEqual(self.client('read_only').ask('same',{}, {'type':'object'}),{'ok':False});post.assert_not_called()

    def test_RP004_invalid_response_preserved_without_resampling(self):
        mock=self.mock();mock.json.return_value['choices'][0]['message']['content']='not-json'
        with patch('requests.post',return_value=mock) as post:
            with self.assertRaises(ModelFailure):self.client('bypass').ask('same',{}, {'type':'object'})
            self.assertEqual(post.call_count,1)
        raw=json.loads(next((self.root/'responses').glob('*_raw.json')).read_text());self.assertEqual(raw['content'],'not-json')
        attempt=json.loads(next((self.root/'attempts').glob('*.json')).read_text());self.assertEqual(attempt['status'],'FAILED')

    def test_RP005_cache_response_mutation_detected(self):
        with patch('requests.post',return_value=self.mock()):self.client('read_write').ask('same',{}, {'type':'object'})
        path=next(self.root.glob('*.json'));saved=json.loads(path.read_text());saved['result']={'ok':'changed'};path.write_text(json.dumps(saved))
        with patch('requests.post') as post:
            with self.assertRaises(ModelFailure):self.client('read_only').ask('same',{}, {'type':'object'})
            post.assert_not_called()


FORM={'type':'object','required':['acceptable_mode_set','severity','confidence'],'properties':{'acceptable_mode_set':{'type':'array'},'severity':{'type':'string'},'confidence':{'type':'number'}}}


def packet(v='v1',parents=None):
    return {'proposal_id':'C1','version_id':v,'patient_alias':'synthetic-patient',
            'parent_versions':parents or [],'root_proposals':['C1'],'packet_sha256':digest(v),
            'as_of':'2025-01-02T10:00:00+08:00','protocol_version':'fictional-protocol',
            'preparation_status':'SOURCE_AND_BOUNDARY_REVIEWED','preparation_reviewer_id':'preparer',
            'reference_form_version':'fictional-form','reference_form_sha256':digest(FORM)}


def gate(p,reviewer='A',valid=True):
    return {'version_id':p['version_id'],'packet_sha256':p['packet_sha256'],'reviewer_id':reviewer,
            'decision':'VALID' if valid else 'REPAIR_REQUIRED','issues':[] if valid else ['INVALID_DUPLICATE'],
            'rationale':'' if valid else 'Fictional suspected duplicate; source review requested',
            'duration_seconds':10}


def reference(p,reviewer='A'):
    return {'version_id':p['version_id'],'packet_sha256':p['packet_sha256'],'reviewer_id':reviewer,
            'form_version':'fictional-form','form_sha256':digest(FORM),'form_schema':FORM,
            'clinical_reference':{'acceptable_mode_set':['FICTIONAL_MODE'],'severity':'FICTIONAL_GRADE','confidence':.7}}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[4]/'private';root.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=root);self.ledger=ReviewLedger(Path(self.tmp.name)/'review.jsonl',required_valid_reviewers=1)
        self.p=packet();self.ledger.append('PACKET',self.p)
    def tearDown(self):self.tmp.cleanup()

    def adjudicate(self):
        self.ledger.append('ADJUDICATION',{'parent_versions':['v1'],'action':'MERGE','confirmed_error':True,
            'error_root_proposals':['C1'],
            'rule_version':'atomicity-v0.1','adjudicator_id':'S','rationale':'synthetic duplicate finding'})

    def test_WF001_invalid_gate_blocks_clinical_reference(self):
        self.ledger.append('GATE',gate(self.p,valid=False))
        with self.assertRaises(ValueError):self.ledger.append('REFERENCE',reference(self.p))

    def test_WF002_valid_gate_allows_version_bound_reference(self):
        self.ledger.append('GATE',gate(self.p));self.ledger.append('REFERENCE',reference(self.p))
        r=summarize(self.ledger.read(),1);self.assertEqual(r['clinical_responses_currently_active'],1)
        bad=reference(self.p,'B');bad['packet_sha256']='wrong'
        with self.assertRaises(ValueError):self.ledger.append('REFERENCE',bad)

    def test_WF003_repair_requires_another_blinded_reviewer(self):
        self.ledger.append('GATE',gate(self.p,valid=False));self.adjudicate();p=packet('v2',['v1']);self.ledger.append('PACKET',p)
        with self.assertRaises(ValueError):self.ledger.append('GATE',gate(p,'A'))
        with self.assertRaises(ValueError):self.ledger.append('GATE',gate(p,'S'))
        self.ledger.append('GATE',gate(p,'B'));self.ledger.append('REFERENCE',reference(p,'B'))
        self.assertEqual(summarize(self.ledger.read(),1)['confirmed_error_original_proposals'],1)

    def test_WF004_initial_version_and_root_lineage_immutable(self):
        with self.assertRaises(ValueError):self.ledger.append('PACKET',self.p)
        self.ledger.append('GATE',gate(self.p,valid=False));self.adjudicate();p=packet('v2',['v1']);p['root_proposals']=['other']
        with self.assertRaises(ValueError):self.ledger.append('PACKET',p)

    def test_WF005_later_state_exposure_blocks_earlier_review(self):
        self.ledger.append('GATE',gate(self.p));p=packet('early');p['proposal_id']='C2';p['root_proposals']=['C2'];p['as_of']='2025-01-01T10:00:00+08:00'
        self.ledger.append('PACKET',p)
        with self.assertRaises(ValueError):self.ledger.append('GATE',gate(p))

    def test_WF006_later_flag_quarantines_previous_reference(self):
        self.ledger.append('GATE',gate(self.p));self.ledger.append('REFERENCE',reference(self.p));self.ledger.append('GATE',gate(self.p,'B',False))
        r=summarize(self.ledger.read(),1);self.assertEqual(r['clinical_responses_quarantined'],1);self.assertEqual(r['clinical_responses_recorded'],1)

    def test_WF007_pending_and_flagged_denominators_distinct(self):
        self.ledger.append('GATE',gate(self.p));p=packet('pending');p['proposal_id']='C2';p['root_proposals']=['C2'];self.ledger.append('PACKET',p)
        r=summarize(self.ledger.read(),1);self.assertEqual(r['first_pass_valid_over_all_proposed'],.5);self.assertEqual(r['first_pass_valid_over_reviewed'],1)

    def test_WF008_ledger_tampering_detected(self):
        rows=self.ledger.read();rows[0]['payload']['packet_sha256']='edited'
        with self.assertRaises(ValueError):ReviewLedger.verify(rows)

    def test_WF009_schema_and_exposure_cannot_be_bypassed(self):
        self.ledger.append('GATE',gate(self.p));r=reference(self.p);r['form_schema']={'type':'object'};r['form_sha256']=digest(r['form_schema'])
        with self.assertRaises(ValueError):self.ledger.append('REFERENCE',r)
        self.ledger.append('EXPOSURE',{'patient_alias':self.p['patient_alias'],'reviewer_id':'A','reason':'FUTURE_OUTCOME'})
        with self.assertRaises(ValueError):self.ledger.append('REFERENCE',reference(self.p))

    def test_WF010_unprepared_packet_and_clinical_answer_in_gate_rejected(self):
        p=packet('unprepared');p['preparation_status']='ALGORITHM_ONLY'
        with self.assertRaises(ValueError):self.ledger.append('PACKET',p)
        g=gate(self.p);g['clinical_reference']={}
        with self.assertRaises(ValueError):self.ledger.append('GATE',g)
        g=gate(self.p,valid=False);g.pop('rationale')
        with self.assertRaises(ValueError):self.ledger.append('GATE',g)

    def test_WF011_new_version_cannot_discard_parent_lineage(self):
        with self.assertRaises(ValueError):self.ledger.append('PACKET',packet('hidden-new-version'))

    def test_WF012_merge_can_include_valid_parent_without_labeling_both_wrong(self):
        self.ledger.append('GATE',gate(self.p));self.ledger.append('REFERENCE',reference(self.p))
        p=packet('v2');p['proposal_id']='C2';p['root_proposals']=['C2'];self.ledger.append('PACKET',p);self.ledger.append('GATE',gate(p,'B',False))
        self.ledger.append('ADJUDICATION',{'parent_versions':['v1','v2'],'action':'MERGE','confirmed_error':True,
            'error_root_proposals':['C2'],'rule_version':'fixture','adjudicator_id':'S','rationale':'one duplicate of valid parent'})
        r=summarize(self.ledger.read());self.assertEqual(r['confirmed_error_original_proposals'],1)
        self.assertEqual(r['structurally_affected_original_proposals'],2);self.assertEqual(r['clinical_responses_quarantined'],1)

    def test_WF013_later_exposure_does_not_rewrite_sealed_prior_rating(self):
        self.ledger.append('GATE',gate(self.p));self.ledger.append('REFERENCE',reference(self.p))
        self.ledger.append('EXPOSURE',{'patient_alias':self.p['patient_alias'],'reviewer_id':'A','reason':'LATER_RESEARCH_ACCESS'})
        self.assertEqual(summarize(self.ledger.read())['clinical_responses_currently_active'],1)
        p=packet('another');p['proposal_id']='C2';p['root_proposals']=['C2'];self.ledger.append('PACKET',p)
        with self.assertRaises(ValueError):self.ledger.append('GATE',gate(p))


class AnnotationTests(unittest.TestCase):
    def bundle(self,text='尚待复查。'):
        from hashlib import sha256
        return {'documents':[{'patient_uid':'p','source_id':'s','source_sha256':sha256(text.encode()).hexdigest(),'text':text}]}
    def annotation(self):
        return {'origin':'independent_human','annotator_ids':['A'],'annotation_status':'INDEPENDENT_DRAFT',
            'complete_within_declared_scope':False,'mentions':[{'id':'a','kind':'DecisionMention','anchor':{'source_id':'s','quote':'尚待复查'}}],
            'mention_to_event':{'a':'E1'},'relations':[]}

    def test_AN001_quotes_compile_without_creating_adjudication(self):
        from scripts.cohort_construction.p2_relation_graph.annotation_compile import compile_annotation
        bundle=self.bundle();a=self.annotation();r=compile_annotation(bundle,a)
        self.assertEqual(r['mentions'][0]['anchor']['span'],[0,4]);self.assertEqual(r['annotation_status'],'INDEPENDENT_DRAFT')
        a['annotation_status']='ADJUDICATED'
        with self.assertRaises(ValueError):compile_annotation(bundle,a)

    def test_AN002_repeated_quote_cannot_be_guessed(self):
        from scripts.cohort_construction.p2_relation_graph.annotation_compile import compile_annotation
        with self.assertRaises(ValueError):compile_annotation(self.bundle('尚待复查。尚待复查。'),self.annotation())

    def test_AN003_independent_materials_identical_without_model_labels(self):
        from scripts.cohort_construction.p2_relation_graph.annotation_pilot import prepare
        from scripts.cohort_construction.p2_relation_graph.demo import data
        notes,units=data()
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'pilot';r=prepare(notes,units,output,count=1)
            a=output/'annotator_A/LOCAL-01';b=output/'annotator_B/LOCAL-01'
            self.assertEqual((a/'sources.json').read_bytes(),(b/'sources.json').read_bytes())
            self.assertFalse(r['human_gold_created']);draft=json.loads((a/'annotation.json').read_text())
            self.assertFalse(draft['complete_within_declared_scope']);self.assertFalse(draft['mentions'])
            self.assertNotIn('stratum',json.loads((a/'sources.json').read_text()))

    def test_AN004_iaa_does_not_require_fabricated_adjudicated_gold(self):
        from scripts.cohort_construction.p2_relation_graph.inter_annotator import compare
        a=self.annotation();a['annotation_status']='INDEPENDENT_COMPLETE';a['complete_within_declared_scope']=True
        a['scope']={'kind':'source_windows','windows':[{'patient_uid':'p','source_id':'s','source_sha256':'a'*64,'start':0,'end':10}]}
        a['mentions'][0]['anchor']={'patient_uid':'p','source_id':'s','source_sha256':'a'*64,'span':[0,4],'quote':'尚待复查'}
        b=copy.deepcopy(a);b['annotator_ids']=['B'];b['mentions'][0]['id']='b';b['mention_to_event']={'b':'B1'}
        r=compare(a,b);self.assertEqual(r['mention_span_type_agreement']['symmetric_f1'],1)
        self.assertNotIn('gold',r['decision_coreference_pair_agreement'])
        self.assertEqual(a['annotation_status'],'INDEPENDENT_COMPLETE')
        b['scope']['windows']=[{'changed':True}]
        with self.assertRaises(ValueError):compare(a,b)

    def test_AN005_iaa_requires_separate_independent_people(self):
        from scripts.cohort_construction.p2_relation_graph.inter_annotator import compare
        a=self.annotation();a['annotation_status']='INDEPENDENT_COMPLETE';a['complete_within_declared_scope']=True
        a['scope']={'kind':'source_windows','windows':[{'patient_uid':'p','source_id':'s','source_sha256':'a'*64,'start':0,'end':10}]}
        a['mentions'][0]['anchor']={'patient_uid':'p','source_id':'s','source_sha256':'a'*64,'span':[0,4],'quote':'尚待复查'}
        with self.assertRaises(ValueError):compare(a,copy.deepcopy(a))


if __name__=='__main__':unittest.main()
