"""Cross-runtime contracts and doctor-file isolation, using fictional material."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from scripts.cohort_construction.p2_checkpoint_fhir import doctor_bundle_from_packet
from scripts.cohort_construction.p2_checkpoint_review_app.build import build_reviewer_html
from scripts.cohort_construction.p2_checkpoint_review_app.synthetic_demo import synthetic_packet,HIDDEN_SENTINEL

ROOT=Path(__file__).resolve().parents[4]
APP=ROOT/'scripts/cohort_construction/p2_checkpoint_review_app/assets/reviewer.js'
NODE=shutil.which('node') or '/Users/lucachangretta/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
EXAMPLES=ROOT/'scripts/cohort_construction/p2_checkpoint_fhir/examples'
PROGRAM="""const fs=require('fs');globalThis.crypto=require('crypto').webcrypto;
const api=require(process.argv[1]);const v=api.parseJsonUnique(fs.readFileSync(0,'utf8'));
(async()=>{try{let r;if(v.op==='decode'){let x=await api.decodeDoctorBundle(v.bundle,v.expected);r={status:'PASS',packet:x.packet};}
else if(v.op==='response'){r=api.validateResponse(v.questionnaire,v.response);}
else if(v.op==='cases'){r=v.cases.map(c=>{try{api.validateResponse(c.questionnaire,c.response);return {name:c.name,valid:true,error:null}}catch(e){return {name:c.name,valid:false,error:e.code}}});}
else if(v.op==='parse'){r={value:api.parseJsonUnique(v.text)};}
process.stdout.write(JSON.stringify({ok:true,result:r}));}catch(e){process.stdout.write(JSON.stringify({ok:false,error:e.code||e.message}));}})();"""


def js(value):
    run=subprocess.run([NODE,'-e',PROGRAM,str(APP)],input=json.dumps(value,ensure_ascii=True),text=True,capture_output=True,check=True)
    return json.loads(run.stdout)


@pytest.mark.parametrize('filename',['doctor-bundle.synthetic.json','doctor-bundle.decimal.synthetic.json'])
def test_CE_APP001_browser_and_python_agree_on_permitted_bundle_hashes(filename):
    bundle=json.loads((EXAMPLES/filename).read_text())
    result=js(dict(op='decode',bundle=bundle))
    assert result['ok'] and result['result']['status']=='PASS'


def test_CE_APP001_questionnaire_response_cross_language_fixture_agreement():
    fixture=json.loads((ROOT/'scripts/cohort_construction/p2_checkpoint_review/tests/fixtures/cross_language_cases.json').read_text())
    result=js(dict(op='cases',cases=fixture['cases']))['result']
    for actual,expected in zip(result,fixture['cases']):
        assert actual['valid']==expected['expected_valid'],expected['name']
        assert actual['error']==expected['expected_error_code'],expected['name']


@pytest.mark.parametrize('mutation,code',[
    ('future-resource','FHIR_EXACTLY_ONE_CURRENT_CHECKPOINT_REQUIRED'),
    ('external-url','FHIR_INLINE_UTF8_TEXT_ONLY'),
    ('raw-text','FHIR_MATERIAL_HASH_MISMATCH'),
    ('prefilled-questionnaire','UNSUPPORTED_FIELD'),
    ('trusted-hash','FHIR_TRUSTED_PACKET_BINDING_MISMATCH'),
    ('duplicate-helper','FHIR_QUESTIONNAIRE_CANONICAL_JSON_INVALID')])
def test_CE_APP002_unapproved_or_tampered_bundle_is_rejected_before_rendering(mutation,code):
    p=synthetic_packet();b=doctor_bundle_from_packet(p);expected=p['packet_sha256']
    if mutation=='future-resource':b['entry'].append(dict(fullUrl='urn:uuid:unapproved',resource=dict(resourceType='Basic',code=dict(text='Future secret'))))
    if mutation=='external-url':b['entry'][1]['resource']['content'][0]['attachment']={'contentType':'text/plain;charset=utf-8','url':'https://example.invalid/whole-original'}
    if mutation=='raw-text':b['entry'][1]['resource']['content'][0]['attachment']['data']='aGlkZGVu'
    if mutation=='prefilled-questionnaire':
        b['entry'][-1]['resource']['item'][0]['item'][0]['initial']=[]
        helper=next(e for e in b['entry'][0]['resource']['extension'] if e['url'].endswith('/questionnaire-canonical-json'))
        helper['valueString']=json.dumps(b['entry'][-1]['resource'],ensure_ascii=False,sort_keys=True,separators=(',',':'))
    if mutation=='trusted-hash':expected='0'*64
    if mutation=='duplicate-helper':
        ext=next(e for e in b['entry'][0]['resource']['extension'] if e['url'].endswith('/questionnaire-canonical-json'))
        ext['valueString']='{"hidden":"future","hidden":"overwritten"}'
    r=js(dict(op='decode',bundle=b,expected=expected))
    assert not r['ok'] and r['error']==code


@pytest.mark.parametrize('text',['{"a":1,"a":2}','{"a":{"k":1,"k":2}}','{"x":NaN}','{"x":1e999}','{"x":"\\ud800"}'])
def test_CE_APP002_raw_json_parser_does_not_hide_duplicate_or_invalid_values(text):
    assert not js(dict(op='parse',text=text))['ok']


def test_CE_APP003_delivered_html_contains_only_doctor_materials_and_is_self_contained():
    p=synthetic_packet();b=doctor_bundle_from_packet(p)
    html=build_reviewer_html(b,assignment_id='SYNTHETIC-ASSIGNMENT',expected_packet_sha256=p['packet_sha256'])
    assert HIDDEN_SENTINEL not in html
    assert 'SYNTHETIC-AUDITOR-ONLY' not in html
    assert '__ASSIGNED_DOCTOR_DATA__' not in html
    assert '<script src=' not in html and '<link ' not in html
    assert "connect-src 'none'" in html and 'window.fetch' not in html
    assert p['packet_sha256'] in html


def test_CE_APP003_literal_closing_script_is_escaped_in_embedded_permitted_text():
    p=synthetic_packet(literal_markup=True)
    html=build_reviewer_html(doctor_bundle_from_packet(p),assignment_id='SYNTHETIC-ASSIGNMENT',expected_packet_sha256=p['packet_sha256'])
    assert '</script><script>window.XSS_EXECUTED' not in html
    assert HIDDEN_SENTINEL not in html


def test_CE_APP003_builder_checks_trusted_assignment_and_rejects_research_objects():
    p=synthetic_packet();b=doctor_bundle_from_packet(p)
    with pytest.raises(ValueError):build_reviewer_html(b,assignment_id='',expected_packet_sha256=p['packet_sha256'])
    with pytest.raises(ValueError):build_reviewer_html(b,assignment_id='a',expected_packet_sha256='0'*64)
    b['researcher_raw_text']=HIDDEN_SENTINEL
    with pytest.raises(ValueError):build_reviewer_html(b,assignment_id='a',expected_packet_sha256=p['packet_sha256'])
