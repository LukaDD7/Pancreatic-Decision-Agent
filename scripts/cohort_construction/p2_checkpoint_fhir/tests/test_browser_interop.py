"""Independent browser/Python wire tests with fabricated source text only."""
from __future__ import annotations
import base64
import copy
import json
from pathlib import Path
import shutil
import subprocess
import pytest
from scripts.cohort_construction.p2_checkpoint_packet import canonical_sha256,text_sha256
from scripts.cohort_construction.p2_checkpoint_review import validate_submission
from scripts.cohort_construction.p2_checkpoint_fhir import (
    doctor_bundle_from_packet,packet_from_doctor_bundle,
    response_bundle_from_submission,submission_from_response_bundle,
)
from .test_fhir_adapter import packet,rehash,basic,ext,doc

ROOT=Path(__file__).resolve().parents[4]
APP=ROOT/'scripts/cohort_construction/p2_checkpoint_review_app/assets/reviewer.js'
NODE=shutil.which('node') or '/Users/lucachangretta/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
PROGRAM=r"""
const fs=require('fs'); globalThis.crypto=require('crypto').webcrypto;
const api=require(process.argv[1]), request=api.parseJsonUnique(fs.readFileSync(0,'utf8'));
(async()=>{try{
 const decoded=await api.decodeDoctorBundle(request.bundle,request.expected);
 let value;
 if(request.op==='submission'){
   value=api.submission(decoded.packet,request.response,'fictional-browser-assignment',
       '2026-10-28T12:00:00Z','fictional-browser-submission');
 }else{
   value=await api.encodeDoctorBundle(decoded.packet,decoded.questionnaire_canonical_json,decoded.episode_id);
 }
 process.stdout.write(JSON.stringify({ok:true,value}));
}catch(e){process.stdout.write(JSON.stringify({ok:false,code:e.code||e.message}));}})();
"""


def browser(bundle,expected,**kwargs):
    if not Path(NODE).is_file():pytest.skip('Node runtime is needed for independent browser interoperation')
    result=subprocess.run([NODE,'-e',PROGRAM,str(APP)],input=json.dumps({'bundle':bundle,'expected':expected,**kwargs},ensure_ascii=True),
        text=True,capture_output=True,check=True,timeout=30)
    return json.loads(result.stdout)


@pytest.mark.parametrize('text',[
    '', '\ufeff虚构患者许可原文。','文内\ufeff不应删除。','甲😀\r\n乙\n丙\r丁','é é',
    '</script><script>window.FICTIONAL_XSS=1</script>', '\u2028\u2029&<>',
])
def test_browser_preserves_exact_permitted_text_including_leading_bom(text):
    p=packet();p['materials'][0]['text']=text;p['materials'][0]['sha256']=text_sha256(text);rehash(p)
    b=doctor_bundle_from_packet(p);result=browser(b,p['packet_sha256'])
    assert result['ok'],result
    restored=packet_from_doctor_bundle(result['value'],expected_packet_sha256=p['packet_sha256'])
    assert restored==p
    assert restored['materials'][0]['text']==text


@pytest.mark.parametrize('mutation',[
    'future-checkpoint','extra-resource','foreign-attachment','unknown-property','unknown-extension',
    'duplicate-material','material-tamper','questionnaire-helper-duplicate','frozen-false',
])
def test_browser_and_python_reject_same_isolation_counterexamples(mutation):
    p=packet();b=doctor_bundle_from_packet(p)
    if mutation=='future-checkpoint':
        b['entry'].append({'fullUrl':'urn:uuid:fictional-future','resource':{'resourceType':'Basic','code':{'text':'future'}}})
    elif mutation=='extra-resource':
        b['entry'].append({'fullUrl':'urn:uuid:fictional-patient','resource':{'resourceType':'Patient'}})
    elif mutation=='foreign-attachment':doc(b)['content'][0]['attachment']['url']='https://foreign.invalid/full-text'
    elif mutation=='unknown-property':b['researcher_full_text']='fictional-hidden-content'
    elif mutation=='unknown-extension':basic(b)['extension'].append({'url':'urn:unapproved','valueString':'fictional-hidden-content'})
    elif mutation=='duplicate-material':basic(b)['extension'].append(copy.deepcopy(ext(basic(b),'material')))
    elif mutation=='material-tamper':doc(b)['content'][0]['attachment']['data']=base64.b64encode('改字'.encode()).decode()
    elif mutation=='questionnaire-helper-duplicate':ext(basic(b),'questionnaire-canonical-json')['valueString']='{"item":[],"item":[]}'
    elif mutation=='frozen-false':ext(basic(b),'questionnaire-frozen')['valueBoolean']=False
    with pytest.raises(ValueError):packet_from_doctor_bundle(b,expected_packet_sha256=p['packet_sha256'])
    assert not browser(b,p['packet_sha256'])['ok']


def test_browser_exported_multiselect_and_decimal_response_enters_python_fhir_roundtrip():
    p=packet();p['questionnaire']['resource']['item'] += [{'linkId':'decimal','type':'decimal','required':True}];rehash(p)
    response={'resourceType':'QuestionnaireResponse','questionnaire':p['questionnaire']['canonical']+'|'+p['questionnaire']['version'],
        'status':'completed','item':[
            {'linkId':'fictional-item','answer':[{'valueCoding':{'system':'urn:fictional','code':'a'}},
                                               {'valueCoding':{'system':'urn:fictional','code':'b'}}]},
            {'linkId':'decimal','answer':[{'valueDecimal':1e-7}]},
        ]}
    result=browser(doctor_bundle_from_packet(p),p['packet_sha256'],op='submission',response=response)
    assert result['ok'],result
    submission=result['value'];validate_submission(submission,p)
    b=response_bundle_from_submission(submission,p)
    assert submission_from_response_bundle(b,p,expected_reviewer_assignment_id='fictional-browser-assignment')==submission
    assert len(submission['questionnaire_response']['item'][0]['answer'])==2
    assert submission['questionnaire_response']['item'][1]['answer'][0]['valueDecimal']==1e-7
