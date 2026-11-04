"""Synthetic end-to-end orchestration controls, without a model endpoint."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.cohort_construction.p2_auto_checkpoint_v04 import engine
from scripts.cohort_construction.p2_auto_checkpoint_v04.prompts import UNCERTAINTY_KEYS
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_engine import source
from scripts.cohort_construction.p2_auto_checkpoint_v06 import run


def discovery_response(action, boundary):
    return dict(events=[dict(local_id='management-1',scope='yes',record_function='new_decision',
        management_question='虚构的检查安排',action=action,
        action_quotes=[dict(source_id='C',quote=action)],
        choice_boundary_quotes=[dict(source_id='C',quote=boundary)],
        prechoice_facts=[dict(source_id='C',quote='复查提示变化。')],postchoice_facts=[],
        choice_time=None,time_quotes=[],time_basis='record_time_proxy',phase='虚构评估阶段',
        uncertainty=[],rationale='虚构控制',uncertainty_dimensions={key:dict(status='uncertain',
            reason_category='genuine_missing_information',explanation='虚构未明信息',source_quotes=[])
            for key in UNCERTAINTY_KEYS})],rejected_mentions=[])


class PipelineClient:
    def __init__(self, *args):
        self.receipts=[]
        self.calls=[]

    def ask(self, task, payload, schema):
        self.calls.append((task,deepcopy(payload)))
        receipt=dict(request_sha256='hash-'+str(len(self.calls)),cached=False,status='SUCCESS')
        self.receipts.append(receipt)
        if task=='document_event_discovery':
            response=discovery_response('复查提示变化。决定补充检查。','原文不存在的决定')
        elif task=='document_event_discovery_feedback':
            response=discovery_response('决定补充检查。','决定补充检查。')
        elif task=='document_checkpoint_review':
            eid=payload['events'][0]['event']['event_id']
            response=dict(items=[dict(event_id=eid,scope='yes',record_function='new_decision',
                checkpoint='first_anchor',previous_event_id=None,same_event_with=[],
                same_process_previous_id=None,process_link='uncertain',evidence_evolution='uncertain',
                observation_quotes=[],connection_quotes=[],rationale='虚构首个可见安排',uncertainty=[],
                evidence_comparison=dict(previous_known_quotes=[],current_information_quotes=[],
                    change_kind='none',reinterpretation_quotes=[],explanation='虚构起始记录'),
                evidence_connection='uncertain',observation_availability=[],
                uncertainty_dimensions=discovery_response('x','x')['events'][0]['uncertainty_dimensions'])])
        else:
            raise AssertionError('Unexpected additional request: '+task)
        return response,receipt


def config():
    return dict(version='p2-auto-checkpoint-0.6.0',discovery_context_characters=80000,
        review_context_characters=150000,preserve_current_prechoice_context=True,
        max_review_payload_characters=240000,discovery_feedback_max_rounds=1,
        review_feedback_max_rounds=1,include_support_documents=True,final_graph_validation=True)


def test_feedback_recompiles_ids_and_context_before_review_and_final_graph():
    current=source('C','复查提示变化。决定补充检查。','2022-01-02 09:00')
    client=PipelineClient()
    with TemporaryDirectory() as d,patch.object(engine,'Client',return_value=client):
        events,episodes,summary=engine.process_sources('fictional',[current],config(),Path(d)/'out',Path(d)/'cache',workers=1)
        saved=json.loads((Path(d)/'out/discovery.json').read_text())
        audited=json.loads((Path(d)/'out/final_graph_audit.json').read_text())
    assert [task for task,payload in client.calls]==[
        'document_event_discovery','document_event_discovery_feedback','document_checkpoint_review']
    first=discovery_response('复查提示变化。决定补充检查。','原文不存在的决定')
    unit=engine.build_units([current],config())[0]
    initial=engine.compile_discovery_response(unit,[current],first,[],dict(request_sha256='initial'))
    assert events[0]['event_id']!=initial['events'][0]['event_id']
    review_payload=client.calls[-1][1]
    assert review_payload['events'][0]['event']['event_id']==events[0]['event_id']
    assert initial['events'][0]['event_id'] not in json.dumps(review_payload,ensure_ascii=False)
    assert all('决定补充检查' not in b['text'] for b in review_payload['evidence_source_pool'])
    assert saved['events'][0]['action_citations']['valid'][0]['quote']=='决定补充检查。'
    assert saved['units'][0]['discovery_feedback_audit']['rounds_attempted']==1
    assert audited['event_count']==1
    assert summary['discovery_feedback_units_attempted']==1
    assert summary['review_feedback_groups_attempted']==0
    assert summary['candidate_checkpoints']==1 and len(episodes)==1
    assert not events[0]['blind_replay_ready'] and not summary['blind_packets_created']


def test_invalid_feedback_limits_fail_before_client_construction():
    for key in ('review_feedback_max_rounds','discovery_feedback_max_rounds'):
        for value in (True,2,-1,'1'):
            invalid=config();invalid[key]=value
            with TemporaryDirectory() as d,patch.object(engine,'Client') as client:
                with pytest.raises(ValueError):
                    engine.process_sources('fictional',[],invalid,Path(d)/'out',Path(d)/'cache')
                client.assert_not_called()
                assert not (Path(d)/'out').exists()


def test_v06_preflight_preserves_existing_output_and_rejects_legacy_config():
    with TemporaryDirectory() as d:
        path=Path(d)/'config.json';out=Path(d)/'existing';out.mkdir()
        path.write_text(json.dumps(dict(config(),semantic_model='deepseek-v4.1-flash')))
        with pytest.raises(ValueError,match='NEW_OUTPUT'):
            run.preflight(['--config',str(path),'--output',str(out)])
        legacy=dict(config(),semantic_model='deepseek-v4.1-flash',version='p2-auto-checkpoint-0.5.0')
        path.write_text(json.dumps(legacy))
        with pytest.raises(ValueError,match='EXPLICIT_V06'):
            run.preflight(['--config',str(path),'--output',str(Path(d)/'new')])


def test_v05_configs_keep_feedback_and_support_discovery_disabled():
    for filename in ('p2_auto_checkpoint_v05.json','p2_auto_checkpoint_v05_deepseek.json'):
        c=json.loads((run.ROOT/'config'/filename).read_text())
        assert not any(c.get(key,False) for key in ('include_support_documents','final_graph_validation',
            'review_feedback_max_rounds','discovery_feedback_max_rounds'))


@pytest.mark.parametrize('error,marker',[('missing','MODEL_OMITTED_EVENT_ID'),
                                       ('duplicate','MODEL_DUPLICATED_EVENT_ID')])
def test_initial_review_id_error_has_one_fixed_pool_revision(error,marker):
    class BrokenReviewClient(PipelineClient):
        def ask(self, task, payload, schema):
            if task=='document_checkpoint_review_repair':
                result,receipt=super().ask('document_checkpoint_review',payload,schema)
                self.calls[-1]=(task,self.calls[-1][1])
                return result,receipt
            result,receipt=super().ask(task,payload,schema)
            if task=='document_checkpoint_review':
                result['items']=[] if error=='missing' else result['items']*2
            return result,receipt
    current=source('C','复查提示变化。决定补充检查。','2022-01-02 09:00')
    client=BrokenReviewClient()
    with TemporaryDirectory() as d,patch.object(engine,'Client',return_value=client):
        events,episodes,summary=engine.process_sources('fictional',[current],config(),Path(d)/'out',Path(d)/'cache',workers=1)
        audit=json.loads((Path(d)/'out/initial_review_id_audits.json').read_text())[0]
        original=json.loads((Path(d)/'out/reviews_before_feedback.json').read_text())[0]
    eid=events[0]['event_id']
    assert audit['missing_event_ids' if error=='missing' else 'duplicated_event_ids']==[eid]
    assert original['semantic_review']['rationale']==marker
    assert original['checkpoint_status']=='UNCERTAIN_SEMANTIC_JUDGMENT'
    assert events[0]['checkpoint_status']=='CANDIDATE_FIRST_ANCHOR'
    assert marker in events[0]['review_refinement']['triggers']
    assert summary['review_feedback_groups_attempted']==1
    assert [task for task,payload in client.calls].count('document_checkpoint_review_repair')==1
    first=next(payload for task,payload in client.calls if task=='document_checkpoint_review')
    revised=client.calls[-1][1]
    for key in ('current_full_document','evidence_source_pool','earlier_event_ledger'):
        assert revised[key]==first[key]


def test_unexpected_initial_review_id_is_retained_in_audit_without_creating_event():
    class ExtraIDClient(PipelineClient):
        def ask(self, task, payload, schema):
            result,receipt=super().ask(task,payload,schema)
            if task=='document_checkpoint_review':
                unexpected=deepcopy(result['items'][0]);unexpected['event_id']='UNPROVIDED_EVENT'
                result['items'].append(unexpected)
            return result,receipt
    current=source('C','复查提示变化。决定补充检查。','2022-01-02 09:00')
    client=ExtraIDClient()
    with TemporaryDirectory() as d,patch.object(engine,'Client',return_value=client):
        events,_,_=engine.process_sources('fictional',[current],config(),Path(d)/'out',Path(d)/'cache',workers=1)
        audit=json.loads((Path(d)/'out/initial_review_id_audits.json').read_text())[0]
    assert audit['unexpected_event_ids']==['UNPROVIDED_EVENT']
    assert len(events)==1 and events[0]['event_id']!='UNPROVIDED_EVENT'
    assert not any(task=='document_checkpoint_review_repair' for task,payload in client.calls)


def test_exact_source_input_snapshot_survives_removed_temporary_path():
    with TemporaryDirectory() as d:
        args=SimpleNamespace(sources=Path(d)/'temporary',output=Path(d)/'output')
        original=args.sources/'fictional'/'source_fhir_r4_bundle.json'
        original.parent.mkdir(parents=True)
        content=b'{"resourceType":"Bundle",\r\n"entry":[]}\r\n'
        original.write_bytes(content)
        receipt=run.archive_source_input('fictional',args)
        original.unlink()
        assert Path(receipt['snapshot_path']).read_bytes()==content
        assert receipt['exact_bytes_preserved'] and receipt['researcher_only']
        assert len(receipt['sha256'])==64


def test_source_snapshot_refuses_overwrite():
    with TemporaryDirectory() as d:
        args=SimpleNamespace(sources=Path(d)/'sources',output=Path(d)/'output')
        original=args.sources/'fictional'/'source_fhir_r4_bundle.json'
        original.parent.mkdir(parents=True);original.write_text('{}')
        first=run.archive_source_input('fictional',args)
        original.write_text('{"changed":true}')
        with pytest.raises(FileExistsError):run.archive_source_input('fictional',args)
        assert Path(first['snapshot_path']).read_text()=='{}'
