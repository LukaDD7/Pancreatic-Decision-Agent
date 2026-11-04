"""Only fabricated text, form values and graph IDs are used here."""
from __future__ import annotations
import base64
import copy
import json
import tempfile
from pathlib import Path
import pytest
from scripts.cohort_construction.p2_checkpoint_packet import canonical_sha256, text_sha256, make_review_submission
from scripts.cohort_construction.p2_checkpoint_packet.tests.test_compiler import packet
from scripts.cohort_construction.p2_checkpoint_fhir import (
    doctor_bundle_from_packet, packet_from_doctor_bundle, response_bundle_from_submission,
    submission_from_response_bundle, researcher_bundle_from_graph, validate_fhir_r4,
)
from scripts.cohort_construction.p2_checkpoint_fhir.common import EXT, full_url
from scripts.cohort_construction.p2_checkpoint_fhir.doctor import _encode as encode_doctor
from scripts.cohort_construction.p2_checkpoint_fhir.response import _encode as encode_response
from scripts.cohort_construction.p2_checkpoint_fhir.__main__ import main


def bundle():
    return doctor_bundle_from_packet(packet())


def basic(b):
    return next(e['resource'] for e in b['entry'] if e['resource']['resourceType'] == 'Basic')


def doc(b):
    return next(e['resource'] for e in b['entry'] if e['resource']['resourceType'] == 'DocumentReference')


def qresource(b):
    return next(e['resource'] for e in b['entry'] if e['resource']['resourceType'] == 'Questionnaire')


def ext(resource, name):
    return next(e for e in resource['extension'] if e['url'] == EXT + name)


def rehash(p):
    p['questionnaire']['sha256'] = canonical_sha256(p['questionnaire']['resource'])
    p['clinical_content_sha256'] = canonical_sha256(p['materials'])
    p['packet_sha256'] = canonical_sha256({k:v for k,v in p.items() if k != 'packet_sha256'})
    return p


def submission(p=None):
    p = p or packet()
    r = {'resourceType':'QuestionnaireResponse',
         'questionnaire':p['questionnaire']['canonical']+'|'+p['questionnaire']['version'],
         'status':'completed', 'authored':'2026-10-28T12:00:00Z',
         'item':[{'linkId':'fictional-item','answer':[{'valueCoding':{'system':'urn:fictional','code':'a'}}]}]}
    return make_review_submission(p,r,submission_id='fictional-submission-1',
        reviewer_assignment_id='fictional-assignment-1',submitted_at='2026-10-28T12:01:00Z')


def research_graph():
    return {'graph_id':'fictional-graph', 'graph_version':'1',
        'events':[{'event_id':'E0','checkpoint_status':'NOT_CHECKPOINT','episode_id':'EP0'},
                  {'event_id':'E1','checkpoint_status':'CANDIDATE_INITIAL','episode_id':'EP0'}],
        'episodes':[{'episode_id':'EP0','candidate_ids':['E0','E1'],'checkpoint_ids':['E1']}],
        'relations':[{'source_event_id':'E0','target_event_id':'E1','relation':'same_management_process','status':'supported'}]}


def test_doctor_roundtrip_exact_and_deterministic_r4():
    p = packet(); original = copy.deepcopy(p)
    b = doctor_bundle_from_packet(p)
    validate_fhir_r4(b)
    assert packet_from_doctor_bundle(b, expected_packet_sha256=p['packet_sha256']) == original
    assert b == doctor_bundle_from_packet(p)
    assert p == original
    assert set(e['resource']['resourceType'] for e in b['entry']) == {'Basic','DocumentReference','Questionnaire'}


def test_no_masked_text_or_research_object_in_doctor_bytes():
    b = bundle(); serialized=json.dumps(b,ensure_ascii=False)
    assert '决定乙' not in serialized and '实施丙' not in serialized
    assert '[资料按方案遮蔽]' in base64.b64decode(doc(b)['content'][0]['attachment']['data']).decode()
    assert 'EpisodeOfCare' not in serialized
    assert 'hash' not in doc(b)['content'][0]['attachment']
    assert 'url' not in doc(b)['content'][0]['attachment']


def test_materials_order_preserved_and_unicode_crlf_exact():
    p=packet()
    p['materials'].append({'material_id':'fictional-extra','source_id':'S2','source_version':'v1',
                          'text':'另件😀\r\né é','sha256':text_sha256('另件😀\r\né é')})
    rehash(p)
    assert packet_from_doctor_bundle(doctor_bundle_from_packet(p)) == p


def test_optional_episode_id_never_adds_other_checkpoint():
    p=packet(); b=doctor_bundle_from_packet(p,episode_id='approved-opaque-episode')
    assert packet_from_doctor_bundle(b) == p
    assert ext(basic(b),'episode-id')['valueString'] == 'approved-opaque-episode'
    assert sum(e['resource']['resourceType']=='Basic' for e in b['entry']) == 1


@pytest.mark.parametrize('field,value', [('patient_identifier','hidden'),('text',{'div':'future'}),('contained',[{'resourceType':'Basic'}]),('modifierExtension',[])])
def test_extra_doctor_basic_fields_rejected(field,value):
    b=bundle(); basic(b)[field]=value
    with pytest.raises(ValueError):packet_from_doctor_bundle(b)


def test_unknown_resource_or_future_checkpoint_rejected():
    b=bundle(); b['entry'].append({'fullUrl':full_url('future','x'),'resource':{'resourceType':'Basic','code':{'text':'future'}}})
    with pytest.raises(ValueError,match='EXACTLY_ONE'):packet_from_doctor_bundle(b)
    b=bundle(); b['entry'].append({'fullUrl':full_url('foreign','x'),'resource':{'resourceType':'Patient'}})
    with pytest.raises(ValueError,match='EXTRA_DOCTOR'):packet_from_doctor_bundle(b)


def test_unknown_extension_and_nested_extra_rejected():
    b=bundle(); basic(b)['extension'].append({'url':'urn:foreign','valueString':'hidden'})
    with pytest.raises(ValueError,match='EXTRA_DOCTOR'):packet_from_doctor_bundle(b)
    b=bundle(); ext(basic(b),'packet-binding')['extension'][0]['extra']='hidden'
    with pytest.raises(ValueError,match='BINDING_SHAPE'):packet_from_doctor_bundle(b)


def test_unknown_questionnaire_field_rejected_even_when_rehashed():
    p=packet(); p['questionnaire']['resource']['contained']=[{'resourceType':'Basic','code':{'text':'hidden'}}]
    rehash(p)
    with pytest.raises(ValueError,match='UNSUPPORTED_FIELD'):doctor_bundle_from_packet(p)


@pytest.mark.parametrize('attachment', [
    {'contentType':'text/plain;charset=utf-8','url':'https://foreign.invalid/data'},
    {'contentType':'text/plain;charset=utf-8','data':'AA==','url':'https://foreign.invalid/data'},
    {'contentType':'text/html','data':'PHA+c2VjcmV0PC9wPg=='},
    {'contentType':'text/plain;charset=utf-8','data':'%%%='},
    {'contentType':'text/plain;charset=utf-8','data':'/w=='},
    {'contentType':'text/plain;charset=utf-8','data':'AA==','hash':'YWJj'},
])
def test_external_or_invalid_attachment_rejected(attachment):
    b=bundle(); doc(b)['content'][0]['attachment']=attachment
    with pytest.raises(ValueError):packet_from_doctor_bundle(b)


def test_material_tampering_rejected():
    b=bundle();doc(b)['content'][0]['attachment']['data']=base64.b64encode('换字'.encode()).decode()
    with pytest.raises(ValueError,match='HASH_MISMATCH'):packet_from_doctor_bundle(b)


def test_form_tampering_rejected():
    b=bundle();qresource(b)['item'][0]['text']='被改的虚构题'
    with pytest.raises(ValueError,match='CANONICAL_JSON_MISMATCH'):packet_from_doctor_bundle(b)


def test_stale_packet_or_questionnaire_version_rejected():
    p=packet();p['packet_version']='2'
    with pytest.raises(ValueError,match='PACKET_HASH'):doctor_bundle_from_packet(p)
    p=packet();p['questionnaire']['resource']['version']='2';rehash(p)
    with pytest.raises(ValueError,match='QUESTIONNAIRE_BINDING'):doctor_bundle_from_packet(p)


def test_recomputed_untrusted_packet_hash_is_not_assignment_authenticity():
    p=packet();expected=p['packet_sha256'];p['packet_version']='2';rehash(p)
    b=doctor_bundle_from_packet(p)
    assert packet_from_doctor_bundle(b)==p
    with pytest.raises(ValueError,match='TRUSTED_PACKET_BINDING'):packet_from_doctor_bundle(b,expected_packet_sha256=expected)


def test_duplicate_local_reference_and_external_reference_rejected():
    b=bundle();basic(b)['extension'].append(copy.deepcopy(ext(basic(b),'material')))
    with pytest.raises(ValueError,match='DUPLICATE_MATERIAL'):packet_from_doctor_bundle(b)
    b=bundle();ext(basic(b),'material')['valueReference']['reference']='https://foreign.invalid/DocumentReference/1'
    with pytest.raises(ValueError,match='UNRESOLVED'):packet_from_doctor_bundle(b)


def test_duplicate_fullurl_or_wrong_reference_type_rejected():
    b=bundle();b['entry'][1]['fullUrl']=b['entry'][0]['fullUrl']
    with pytest.raises(ValueError,match='DUPLICATE_FULLURL'):packet_from_doctor_bundle(b)
    b=bundle();ext(basic(b),'material')['valueReference']=copy.deepcopy(ext(basic(b),'questionnaire')['valueReference'])
    with pytest.raises(ValueError,match='WRONG_TYPE'):packet_from_doctor_bundle(b)


def test_missing_material_or_q_reference_rejected():
    b=bundle();b['entry'].pop(1)
    with pytest.raises(ValueError,match='UNRESOLVED'):packet_from_doctor_bundle(b)
    b=bundle();basic(b)['extension']=[e for e in basic(b)['extension'] if e['url']!=EXT+'questionnaire']
    with pytest.raises(ValueError,match='CARDINALITY'):packet_from_doctor_bundle(b)


def test_order_or_url_mutation_rejected():
    b=bundle();b['entry'].reverse()
    with pytest.raises(ValueError,match='EXTRA_DOCTOR'):packet_from_doctor_bundle(b)
    b=bundle();b['entry'][0]['fullUrl']=full_url('rewritten','x')
    with pytest.raises(ValueError,match='EXTRA_DOCTOR'):packet_from_doctor_bundle(b)


def test_decimal_date_unicode_questionnaire_survives_js_number_serialization():
    p=packet();q=p['questionnaire']['resource'];q['date']='2026-10-28T12:00:00Z';q['title']='虚构😀é é'
    q['item'] += [{'linkId':'decimal','type':'decimal','text':'虚构数值'},
                  {'linkId':'conditional','type':'string','text':'虚构条件题','enableWhen':[{'question':'decimal','operator':'>','answerDecimal':1.0}]},
                  {'linkId':'tiny','type':'string','text':'虚构微小值条件','enableWhen':[{'question':'decimal','operator':'<','answerDecimal':1e-7}]}]
    rehash(p);b=doctor_bundle_from_packet(p)
    # Simulate JS JSON.parse/stringify changing 1.0 to 1, without changing helper text.
    qresource(b)['item'][2]['enableWhen'][0]['answerDecimal']=1
    assert packet_from_doctor_bundle(b)==p
    assert '1.0' in ext(basic(b),'questionnaire-canonical-json')['valueString']
    qresource(b)['item'][2]['enableWhen'][0]['answerDecimal']=True
    with pytest.raises(ValueError,match='CANONICAL_JSON_MISMATCH'):packet_from_doctor_bundle(b)


def test_official_schema_rejects_actual_invalid_fhir():
    b=bundle();b['type']='not-a-fhir-bundle-type'
    with pytest.raises(ValueError,match='FHIR_R4_SCHEMA'):validate_fhir_r4(b)


def test_response_roundtrip_r4_and_no_materials_returned():
    p=packet();s=submission(p);b=response_bundle_from_submission(s,p);validate_fhir_r4(b)
    assert submission_from_response_bundle(b,p)==s
    assert [e['resource']['resourceType'] for e in b['entry']]==['Basic','QuestionnaireResponse']
    assert 'DocumentReference' not in json.dumps(b)


def test_response_unknown_code_and_unanswered_required_item_rejected():
    p=packet();s=submission(p);s['questionnaire_response']['item'][0]['answer'][0]['valueCoding']['code']='not-in-form'
    with pytest.raises(ValueError):response_bundle_from_submission(s,p)
    p=packet();p['questionnaire']['resource']['item'][0]['required']=True;rehash(p)
    s=submission(p);s['questionnaire_response']['item']=[]
    with pytest.raises(ValueError):response_bundle_from_submission(s,p)


def test_response_packet_assignment_and_form_version_binding_rejected():
    p=packet();s=submission(p);b=response_bundle_from_submission(s,p)
    for name in ['packet-sha256','questionnaire-version','checkpoint-id']:
        bad=copy.deepcopy(b)
        next(e for e in ext(basic(bad),'submission-binding')['extension'] if e['url']==name)['valueString']='changed'
        with pytest.raises(ValueError):submission_from_response_bundle(bad,p)


def test_response_mutation_extra_resource_and_unknown_fields_rejected():
    p=packet();b=response_bundle_from_submission(submission(p),p)
    b['entry'][1]['resource']['item'][0]['answer'][0]['valueCoding']['code']='b'
    with pytest.raises(ValueError,match='JSON_MISMATCH'):submission_from_response_bundle(b,p)
    b=response_bundle_from_submission(submission(p),p);b['entry'].append(bundle()['entry'][1])
    with pytest.raises(ValueError,match='EXACT_RESOURCE'):submission_from_response_bundle(b,p)
    b=response_bundle_from_submission(submission(p),p);b['entry'][1]['resource']['text']={'div':'hidden'}
    with pytest.raises(ValueError):submission_from_response_bundle(b,p)


def test_response_hash_tampering_and_duplicate_json_helper_keys_rejected():
    p=packet();s=submission(p);b=response_bundle_from_submission(s,p)
    ext(basic(b),'response-content-sha256')['valueString']='0'*64
    with pytest.raises(ValueError,match='RESPONSE_HASH'):submission_from_response_bundle(b,p)
    raw=json.dumps(s['questionnaire_response'],sort_keys=True,separators=(',',':'))
    raw=raw[:-1]+',"item":'+json.dumps(s['questionnaire_response']['item'])+'}'
    b=encode_response(s,raw)
    with pytest.raises(ValueError):submission_from_response_bundle(b,p)


def test_response_js_decimal_helper_supported_without_python_repr_requirement():
    p=packet();p['questionnaire']['resource']['item']=[{'linkId':'tiny','type':'decimal','required':True}];rehash(p)
    s=submission(p);s['questionnaire_response']['item']=[{'linkId':'tiny','answer':[{'valueDecimal':1e-7}]}]
    raw=json.dumps(s['questionnaire_response'],ensure_ascii=False,sort_keys=True,separators=(',',':')).replace('1e-07','0.0000001')
    b=encode_response(s,raw)
    assert submission_from_response_bundle(b,p)==s


def test_research_bundle_real_refs_and_candidate_status_only():
    g=research_graph();original=copy.deepcopy(g);b=researcher_bundle_from_graph(g);validate_fhir_r4(b)
    assert g==original
    urls={e['fullUrl'] for e in b['entry']}
    for e in b['entry']:
        r=e['resource'];assert r['resourceType']=='Basic'
        assert ext(r,'audience')['valueString']=='researcher-only'
        assert ext(r,'assertion-status')['valueString']=='algorithm-candidate-not-clinical-reference'
        for x in r['extension']:
            if 'valueReference' in x:assert x['valueReference']['reference'] in urls
    with pytest.raises(ValueError):packet_from_doctor_bundle(b)


@pytest.mark.parametrize('mutation', ['badrelation','dangling','duplicateevent','badcheckpoint','missingepisode'])
def test_researcher_graph_invalid_refs_rejected(mutation):
    g=research_graph()
    if mutation=='badrelation':g['relations'][0]['relation']='clinical-confirmed'
    elif mutation=='dangling':g['relations'][0]['source_event_id']='absent'
    elif mutation=='duplicateevent':g['events'].append(copy.deepcopy(g['events'][0]))
    elif mutation=='badcheckpoint':g['episodes'][0]['checkpoint_ids']=['not-a-member']
    else:g['events'][0]['episode_id']='absent'
    with pytest.raises(ValueError):researcher_bundle_from_graph(g)


def test_cli_no_overwrite_and_doctor_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);p=packet();(root/'packet.json').write_text(json.dumps(p,ensure_ascii=False))
        assert main(['doctor-export','--input',str(root/'packet.json'),'--output',str(root/'bundle.json')])==0
        assert main(['doctor-import','--input',str(root/'bundle.json'),'--output',str(root/'roundtrip.json'),
                     '--expected-packet-sha256',p['packet_sha256']])==0
        assert json.loads((root/'roundtrip.json').read_text())==p
        with pytest.raises(FileExistsError):main(['doctor-export','--input',str(root/'packet.json'),'--output',str(root/'bundle.json')])


def test_response_import_can_bind_to_trusted_reviewer_assignment():
    p=packet();s=submission(p);b=response_bundle_from_submission(s,p)
    assert submission_from_response_bundle(b,p,expected_reviewer_assignment_id=s['reviewer_assignment_id'])==s
    with pytest.raises(ValueError,match='TRUSTED_ASSIGNMENT'):
        submission_from_response_bundle(b,p,expected_reviewer_assignment_id='another-assignment')


def test_regression_issue_registry_points_to_executable_controls():
    import importlib
    registry=json.loads((Path(__file__).parents[1]/'regression_issue_map.json').read_text())
    ids=[i['issue_id'] for i in registry['issues']]
    assert len(ids)==len(set(ids)) and len(ids)==6
    for issue in registry['issues']:
        assert len(issue['required_tests'])>=2
        for name in issue['required_tests']:
            module_name,function_name=name.rsplit('.',1)
            assert module_name.startswith('scripts.cohort_construction.p2_checkpoint_fhir.tests.')
            assert callable(getattr(importlib.import_module(module_name),function_name))


def test_empty_permitted_text_has_zero_size_without_empty_fhir_primitive():
    p=packet();p['materials'][0]['text']='';p['materials'][0]['sha256']=text_sha256('');rehash(p)
    b=doctor_bundle_from_packet(p)
    assert doc(b)['content'][0]['attachment']=={'contentType':'text/plain;charset=utf-8','size':0}
    assert packet_from_doctor_bundle(b)==p
    doc(b)['content'][0]['attachment']={'contentType':'text/plain;charset=utf-8','data':''}
    with pytest.raises(ValueError,match='INLINE_UTF8'):packet_from_doctor_bundle(b)


def test_fhir_schema_errors_do_not_echo_raw_document_content():
    b=bundle();b['type']='fabricated-private-marker'
    with pytest.raises(ValueError) as error:validate_fhir_r4(b)
    assert 'fabricated-private-marker' not in str(error.value)
    assert 'attachment' not in str(error.value)


def test_cli_response_and_researcher_paths_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);p=packet();s=submission(p);g=research_graph()
        for name,value in [('packet.json',p),('submission.json',s),('graph.json',g)]:
            (root/name).write_text(json.dumps(value,ensure_ascii=False))
        main(['response-export','--input',str(root/'submission.json'),'--packet',str(root/'packet.json'),'--output',str(root/'response.json')])
        main(['response-import','--input',str(root/'response.json'),'--packet',str(root/'packet.json'),'--output',str(root/'accepted.json'),
              '--expected-reviewer-assignment-id',s['reviewer_assignment_id']])
        assert json.loads((root/'accepted.json').read_text())==s
        main(['research-export','--input',str(root/'graph.json'),'--output',str(root/'research.json')])
        validate_fhir_r4(json.loads((root/'research.json').read_text()))


def test_raw_json_cli_rejects_discarded_duplicate_key_content():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);raw=json.dumps(bundle(),ensure_ascii=False)
        raw=raw.replace('"type": "collection"','"type":"fabricated-hidden-text", "type":"collection"',1)
        (root/'duplicate.json').write_text(raw)
        with pytest.raises(ValueError,match='DUPLICATE_JSON_HELPER_KEY'):
            main(['doctor-import','--input',str(root/'duplicate.json'),'--output',str(root/'out.json')])
        assert not (root/'out.json').exists()


@pytest.mark.parametrize('raw', ['{"a":NaN}','{"a":1e999}','{"a":"\\ud800"}','{"\\udfff":"x"}'])
def test_raw_json_rejects_nonfinite_and_unpaired_surrogates(raw):
    from scripts.cohort_construction.p2_checkpoint_fhir.common import parse_json_unique
    with pytest.raises(ValueError):parse_json_unique(raw)


def test_local_draft_extension_definitions_are_r4_and_cover_wire_contracts():
    definitions=json.loads((Path(__file__).parents[1]/'contracts/local-extension-definitions.bundle.json').read_text())
    validate_fhir_r4(definitions)
    urls={e['resource']['url'] for e in definitions['entry']}
    used=set()
    for b in [bundle(),response_bundle_from_submission(submission(),packet()),researcher_bundle_from_graph(research_graph())]:
        for entry in b['entry']:
            used.update(e['url'] for e in entry['resource'].get('extension',[]))
    assert used.issubset(urls)
    for e in definitions['entry']:
        r=e['resource'];assert r['resourceType']=='StructureDefinition'
        assert r['status']=='draft' and r['experimental'] is True
        assert r['url'].startswith('https://checkpoint-research.invalid/')
        assert 'snapshot' not in r # Validator checks JSON, not generated profile snapshots.


def test_oversized_integer_form_is_controlled_validation_error_not_float_overflow():
    p=packet();q=p['questionnaire']['resource']
    q['item'] += [{'linkId':'decimal','type':'decimal'},
        {'linkId':'conditional','type':'string','enableWhen':[{'question':'decimal','operator':'>','answerDecimal':10**400}]}]
    rehash(p)
    # Bypass export's form validator to exercise a hostile inbound numeric form.
    with pytest.raises(ValueError):packet_from_doctor_bundle(encode_doctor(p))
