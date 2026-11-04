"""CE-MRA regressions: private, immutable, hash-bound material review."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest

from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import build as build_fhir
from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError, canonical_sha256, compile_partition
from scripts.cohort_construction.p2_checkpoint_materials.review import create_review_draft
from scripts.cohort_construction.p2_checkpoint_material_review_app.build import (
    build_assignment, build_files, build_researcher_html, canonical_json, parse_json_unique, validate_assignment,
)
from scripts.cohort_construction.p2_checkpoint_material_review_app.receive import receive_files, receive_submission
from scripts.cohort_construction.p2_checkpoint_material_review_app.synthetic_demo import synthetic_assignment, synthetic_inputs

APP = Path(__file__).resolve().parents[1] / 'assets/reviewer.js'
NODE = shutil.which('node') or '/Users/lucachangretta/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
PROGRAM = '''const fs=require('fs');globalThis.crypto=require('crypto').webcrypto;
const api=require(process.argv[1]);const v=api.parseJsonUnique(fs.readFileSync(0,'utf8'));
(async()=>{try{let result;if(v.op==='parse')result=api.parseJsonUnique(v.raw);
else{const ctx=await api.decodeAssignment(v.assignment);if(v.op==='decode')result={unloaded:[...ctx.unloaded],audit:api.newAudit(ctx)};
else if(v.op==='response')result=await api.makeResponse(ctx,v.audit);else if(v.op==='restore')result=await api.restoreResponse(ctx,v.response);}
process.stdout.write(JSON.stringify({ok:true,result}));}catch(e){process.stdout.write(JSON.stringify({ok:false,error:e.message}));}})();'''


def js(value):
    result = subprocess.run([NODE, '-e', PROGRAM, str(APP)], input=json.dumps(value, ensure_ascii=True),
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def pending(assignment):
    audit = deepcopy(validate_assignment(assignment)['documents']['draft'])
    audit.update(assessment_id='SYNTHETIC-NEW-HUMAN-AUDIT', reviewer_id='SYNTHETIC-ACTUAL-REVIEWER',
                 reviewer_type='human', assessed_at='2026-10-29T10:00:00+08:00', audit_protocol_version='SYNTHETIC-PROTOCOL')
    return audit


def envelope(assignment, audit):
    return dict(format='checkpoint-researcher-material-response-0.1.0', assignment_id=assignment['assignment_id'],
                assignment_sha256=assignment['assignment_sha256'], input_bindings=assignment['bindings'],
                audit=audit, audit_sha256=canonical_sha256(audit))


def test_CE_MRA001_exact_inputs_and_nonbmp_crlf_hashes_roundtrip_without_clinical_inference():
    assignment = synthetic_assignment()
    original = deepcopy(assignment)
    verified = validate_assignment(assignment)
    result = js(dict(op='decode', assignment=assignment))
    assert result['ok'], result
    audit = result['result']['audit']
    assert audit['status']=='PENDING' and audit['independence_attested'] is False
    assert audit['reviewer_id']=='' and audit['reviewer_type']==''
    assert all(not x for x in audit['checks'].values())
    assert all(x['assessment']=='UNRESOLVED' for x in audit['segment_assessments'])
    assert all(x['disposition']=='UNRESOLVED' for x in audit['source_coverage_assessments'])
    assert audit['review_scope']['scope_status']=='INCOMPLETE'
    assert audit['assessment_id']!=verified['documents']['draft']['assessment_id']
    assert ''.join(x['exact_quote'] for x in audit['segment_assessments']).endswith('🧬\r\n')
    assert assignment==original
    # Existing model floats retain their original Python canonical lexemes.
    assert '1.0' in assignment['documents']['selected_event']


@pytest.mark.parametrize('mutation', ['assignment-hash','compiled-document','manifest-document','draft-document','preparation-document','coverage-document'])
def test_CE_MRA001_changed_input_or_assignment_hash_is_rejected_in_both_runtimes(mutation):
    assignment = synthetic_assignment()
    if mutation=='assignment-hash':assignment['assignment_sha256']='0'*64
    elif mutation=='coverage-document':assignment['coverage_sources_json']+=' '
    else:assignment['documents'][mutation.split('-')[0].replace('preparation','preparation_audit')]+=' '
    if mutation!='assignment-hash':
        assignment['assignment_sha256']=canonical_sha256({k:v for k,v in assignment.items() if k!='assignment_sha256'})
    with pytest.raises(ContractError):validate_assignment(assignment)
    assert not js(dict(op='decode',assignment=assignment))['ok']


def test_CE_MRA002_pending_export_requires_new_explicit_actual_reviewer_metadata():
    assignment=synthetic_assignment();draft=deepcopy(validate_assignment(assignment)['documents']['draft'])
    with pytest.raises(ContractError,match='MRA_NEW_ACTUAL_REVIEWER_REQUIRED'):
        receive_submission(assignment,envelope(assignment,draft))
    assert not js(dict(op='response',assignment=assignment,audit=draft))['ok']
    audit=pending(assignment)
    assert receive_submission(assignment,envelope(assignment,audit))==audit
    assert js(dict(op='response',assignment=assignment,audit=audit))['ok']
    for key,value in [('reviewer_id',''),('reviewer_type',''),('assessment_id',draft['assessment_id']),('reviewer_id',draft['reviewer_id']),('assessed_at','2026-10-29T10:00:00')]:
        invalid=deepcopy(audit);invalid[key]=value
        with pytest.raises(ContractError):receive_submission(assignment,envelope(assignment,invalid))
        assert not js(dict(op='response',assignment=assignment,audit=invalid))['ok']


def test_CE_MRA003_unloaded_source_directory_never_becomes_reviewed_coverage():
    assignment=synthetic_assignment(load_excluded=False);audit=pending(assignment)
    assert validate_assignment(assignment)['unloaded_source_ids']==['S-fictional-outside']
    assert receive_submission(assignment,envelope(assignment,audit))['status']=='PENDING'
    outside=next(x for x in audit['source_coverage_assessments'] if x['source_id']=='S-fictional-outside')
    for disposition in ['REVIEWED_INCLUDED','REVIEWED_EXCLUDED','REQUIRED_MISSING','DELIVERY_NOT_FOUND']:
        outside['disposition']=disposition
        with pytest.raises(ContractError,match='MRA_UNLOADED_SOURCE_MUST_REMAIN_UNRESOLVED'):
            receive_submission(assignment,envelope(assignment,audit))
        assert js(dict(op='response',assignment=assignment,audit=audit))['error']=='MRA_UNLOADED_SOURCE_MUST_REMAIN_UNRESOLVED'


def test_CE_MRA004_review_cannot_accept_and_pending_or_fail_revision_never_passes():
    assignment=synthetic_assignment();audit=pending(assignment)
    segment=next(x for x in audit['segment_assessments'] if x['original_disposition']=='REVIEW')
    segment['assessment']='ACCEPT'
    with pytest.raises(ContractError,match='ACCEPT_REQUIRES_PRIOR_KEEP_OR_MASK'):
        receive_submission(assignment,envelope(assignment,audit))
    assert js(dict(op='response',assignment=assignment,audit=audit))['error']=='ACCEPT_REQUIRES_PRIOR_KEEP_OR_MASK'
    for status in ['PENDING','FAIL']:
        audit['status']=status;segment.update(assessment='REVISE',revised_disposition='KEEP')
        assert receive_submission(assignment,envelope(assignment,audit))['status']==status
        assert js(dict(op='response',assignment=assignment,audit=audit))['result']['audit']['status']==status
    audit.update(status='PASS',independence_attested=True)
    audit['checks']={k:True for k in audit['checks']};audit['review_scope']['scope_status']='COMPLETE'
    for item in audit['source_coverage_assessments']:item['disposition']='REVIEWED_INCLUDED'
    for item in audit['segment_assessments']:item.update(assessment='REVISE',revised_disposition='MASK')
    with pytest.raises(ContractError,match='REVISION_REQUIRES_RECOMPILE_AND_REAUDIT'):
        receive_submission(assignment,envelope(assignment,audit))
    assert js(dict(op='response',assignment=assignment,audit=audit))['error']=='REVISION_REQUIRES_RECOMPILE_AND_REAUDIT'


def passing_control():
    documents,sources,proposal=synthetic_inputs()
    # Fictional positive control only: construct an already-resolved partition.
    # This test does not make a clinical permission judgement or modify real tasks.
    for segment in proposal['segments']:
        if segment['disposition']=='REVIEW':segment['disposition']='KEEP'
    documents['compiled']=compile_partition(documents['manifest'],proposal)
    documents['preparation_audit']['compiled_sha256']=documents['compiled']['compiled_sha256']
    documents['draft']=create_review_draft(documents['compiled'],documents['manifest'],
        documents['preparation_audit'],reviewer_id='AI-DRAFT-NOT-A-HUMAN')
    assignment=build_assignment(**documents,assignment_id='SYNTHETIC-PASS-CONTROL',coverage_sources=sources)
    audit=pending(assignment);audit.update(status='PASS',independence_attested=True)
    audit['review_scope']['scope_status']='COMPLETE';audit['checks']={k:True for k in audit['checks']}
    for item in audit['segment_assessments']:item['assessment']='ACCEPT'
    for item in audit['source_coverage_assessments']:
        item['disposition']='REVIEWED_INCLUDED' if item['source_id']=='S-fictional-included' else 'REVIEWED_EXCLUDED'
    return assignment,audit


def test_CE_MRA004_explicit_synthetic_pass_requires_acceptance_of_current_partition_and_all_declarations():
    assignment,audit=passing_control()
    assert receive_submission(assignment,envelope(assignment,audit))['status']=='PASS'
    assert js(dict(op='response',assignment=assignment,audit=audit))['result']['audit']['status']=='PASS'
    for mutation,code in [('segment','UNRESOLVED_SEGMENT_CANNOT_PASS'),('coverage','UNRESOLVED_SOURCE_COVERAGE_CANNOT_PASS'),
                           ('attestation','INDEPENDENCE_ATTESTATION_REQUIRED'),('checks','SEMANTIC_CHECKS_INCOMPLETE')]:
        invalid=deepcopy(audit)
        if mutation=='segment':invalid['segment_assessments'][0]['assessment']='UNRESOLVED'
        elif mutation=='coverage':invalid['source_coverage_assessments'][0]['disposition']='UNRESOLVED'
        elif mutation=='attestation':invalid['independence_attested']=False
        else:invalid['checks']['segmentation_semantics_reviewed']=False
        with pytest.raises(ContractError,match=code):receive_submission(assignment,envelope(assignment,invalid))
        assert js(dict(op='response',assignment=assignment,audit=invalid))['error']==code


def test_CE_MRA004_pass_cannot_silently_change_source_scope_but_pending_and_fail_preserve_proposals():
    assignment,audit=passing_control()
    for location,source_id,disposition in [('included','S-fictional-included','REVIEWED_EXCLUDED'),
                                           ('excluded','S-fictional-outside','REVIEWED_INCLUDED')]:
        for status in ['PASS','PENDING','FAIL']:
            changed=deepcopy(audit);changed['status']=status
            next(x for x in changed['source_coverage_assessments'] if x['source_id']==source_id)['disposition']=disposition
            if status=='PASS':
                code='SOURCE_SCOPE_REVISION_REQUIRES_NEW_PARTITION_AND_REAUDIT'
                with pytest.raises(ContractError,match=code):receive_submission(assignment,envelope(assignment,changed))
                assert js(dict(op='response',assignment=assignment,audit=changed))['error']==code
            else:
                assert receive_submission(assignment,envelope(assignment,changed))['status']==status
                assert js(dict(op='response',assignment=assignment,audit=changed))['result']['audit']['status']==status
        for gap in ['REQUIRED_MISSING','DELIVERY_NOT_FOUND']:
            changed=deepcopy(audit)
            next(x for x in changed['source_coverage_assessments'] if x['source_id']==source_id)['disposition']=gap
            assert receive_submission(assignment,envelope(assignment,changed))['status']=='PASS'
            assert js(dict(op='response',assignment=assignment,audit=changed))['result']['audit']['status']=='PASS'


def test_CE_MRA005_response_and_restore_verify_task_and_audit_hash_and_span_text():
    assignment=synthetic_assignment();audit=pending(assignment)
    response=js(dict(op='response',assignment=assignment,audit=audit))['result']
    assert receive_submission(assignment,response)==audit
    assert js(dict(op='restore',assignment=assignment,response=response))['result']==audit
    for mutation in ['assignment_sha256','input_bindings','audit_sha256','source_quote']:
        wrong=deepcopy(response)
        if mutation=='assignment_sha256':wrong[mutation]='0'*64
        elif mutation=='input_bindings':wrong[mutation]['draft']='0'*64
        elif mutation=='audit_sha256':wrong[mutation]='0'*64
        else:
            wrong['audit']['segment_assessments'][0]['exact_quote']='虚构但不符合原文的改写'
            wrong['audit_sha256']=canonical_sha256(wrong['audit'])
        with pytest.raises(ContractError):receive_submission(assignment,wrong)
        assert not js(dict(op='restore',assignment=assignment,response=wrong))['ok']


def test_CE_MRA006_html_is_researcher_only_offline_and_literal_markup_is_escaped():
    assignment=synthetic_assignment(literal_markup=True)
    html=build_researcher_html(assignment)
    assert '含未遮原文，禁止医生盲评' in html
    assert '</script><script>window.XSS_EXECUTED' not in html
    assert '<script src=' not in html and '<link ' not in html
    assert "connect-src 'none'" in html
    assert not any(x in html for x in ['fetch(', 'XMLHttpRequest', 'sendBeacon(', 'localStorage', 'sessionStorage'])


@pytest.mark.parametrize('raw',['{"k":1,"k":2}','{"nested":{"x":1,"x":2}}','{"x":NaN}','{"x":1e999}','{"x":"\\ud800"}'])
def test_CE_MRA005_raw_json_rejects_duplicate_nonfinite_and_invalid_unicode(raw):
    with pytest.raises((ContractError,ValueError,UnicodeError)):parse_json_unique(raw)
    assert not js(dict(op='parse',raw=raw))['ok']


def files_fixture(tmp_path):
    documents,sources,proposal=synthetic_inputs();directory=tmp_path/'task';directory.mkdir()
    filenames=dict(compiled='compiled_partition.json',manifest='source_manifest.json',draft='semantic-audit.draft.json',preparation_audit='researcher_audit.json',selected_event='selected_reviewed_event.json')
    for name,filename in filenames.items():(directory/filename).write_text(json.dumps(documents[name],ensure_ascii=False))
    for s in sources:s.update(kind='document',family='fictional',title='虚构原文',clocks={},clock={'value':None},clock_basis='unknown',provenance={})
    fhir=tmp_path/'source.fhir.json';fhir.write_text(json.dumps(build_fhir(sources,'SYNTHETIC-PATIENT',[],[]),ensure_ascii=False))
    (directory/'input_receipt.json').write_text(json.dumps(dict(source_fhir_input_sha256=hashlib.sha256(fhir.read_bytes()).hexdigest())))
    return directory,fhir


def test_CE_MRA003_explicit_fhir_input_byte_hash_and_scope_are_verified(tmp_path,monkeypatch):
    monkeypatch.setattr('scripts.cohort_construction.p2_checkpoint_material_review_app.build.PRIVATE_ROOT',tmp_path/'private')
    (tmp_path/'private').mkdir(mode=0o700)
    directory,fhir=files_fixture(tmp_path);before={p.name:p.read_bytes() for p in directory.iterdir()}
    out=tmp_path/'private'/'build'
    with patch('socket.socket',side_effect=AssertionError('NETWORK_FORBIDDEN')) as socket:
        result=build_files(directory,output=out,assignment_id='SYNTHETIC-FILES',source_fhir=fhir)
    assert socket.call_count==0 and result['model_api_calls']==0
    assert result['loaded_source_count']==2 and result['unloaded_source_count']==0
    assignment=parse_json_unique((out/'researcher-assignment.json').read_text())
    assert all(x['disposition']=='UNRESOLVED' for x in validate_assignment(assignment)['documents']['draft']['source_coverage_assessments'])
    assert {p.name:p.read_bytes() for p in directory.iterdir()}==before
    with pytest.raises(FileExistsError):build_files(directory,output=out,assignment_id='SYNTHETIC-FILES',source_fhir=fhir)
    fhir.write_text(fhir.read_text()+' ')
    with pytest.raises(ContractError,match='MRA_SOURCE_FHIR_INPUT_HASH_MISMATCH'):
        build_files(directory,output=tmp_path/'private'/'wrong',assignment_id='SYNTHETIC-FILES',source_fhir=fhir)


def test_CE_MRA001_forged_original_text_or_extra_scope_source_is_rejected():
    documents,sources,_=synthetic_inputs()
    wrong=deepcopy(sources);wrong[0]['text']+='改写'
    with pytest.raises(ContractError,match='MRA_COVERAGE_SOURCE_HASH_MISMATCH'):
        build_assignment(**documents,assignment_id='SYNTHETIC',coverage_sources=wrong)
    wrong=deepcopy(sources);wrong[1]['source_id']='NOT-IN-ASSIGNMENT'
    with pytest.raises(ContractError,match='MRA_COVERAGE_SOURCE_SCOPE_MISMATCH'):
        build_assignment(**documents,assignment_id='SYNTHETIC',coverage_sources=wrong)


def test_CE_MRA006_file_interfaces_require_private_and_refuse_to_overwrite_draft_or_response(tmp_path,monkeypatch):
    monkeypatch.setattr('scripts.cohort_construction.p2_checkpoint_material_review_app.build.PRIVATE_ROOT',tmp_path/'private')
    (tmp_path/'private').mkdir(mode=0o700)
    directory,_=files_fixture(tmp_path)
    with pytest.raises(ContractError,match='MRA_REAL_ASSIGNMENT_OUTPUT_MUST_BE_PRIVATE'):
        build_files(directory,output=tmp_path/'public',assignment_id='SYNTHETIC')
    assignment=synthetic_assignment();audit=pending(assignment);response=envelope(assignment,audit)
    ap=tmp_path/'assignment.json';rp=tmp_path/'response.json'
    ap.write_text(json.dumps(assignment,ensure_ascii=False));rp.write_text(json.dumps(response,ensure_ascii=False))
    with pytest.raises(ContractError,match='MRA_REAL_ASSIGNMENT_OUTPUT_MUST_BE_PRIVATE'):
        receive_files(ap,rp,output=tmp_path/'public.json')
    output=tmp_path/'private'/'human-audit.json'
    saved=receive_files(ap,rp,output=output)
    assert saved['assessment_status']=='PENDING' and saved['independence_authenticated'] is False
    assert saved['doctor_packet_created'] is False
    with pytest.raises(FileExistsError):receive_files(ap,rp,output=output)
    original=output.read_bytes()
    with pytest.raises(FileExistsError):receive_files(ap,rp,output=output)
    assert output.read_bytes()==original


def test_CE_MRA008_private_files_are_owner_only_on_relocated_storage(tmp_path,monkeypatch):
    from scripts.cohort_construction.p2_checkpoint_material_review_app.build import (
        private_mkdir, private_open,
    )
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import new_output, write, write_text
    # Exercise a physical private root without using the old project logical path.
    physical_root = tmp_path / 'external-volume' / 'project'
    private_root = physical_root / 'private'
    monkeypatch.setattr('scripts.cohort_construction.p2_checkpoint_material_review_app.build.PRIVATE_ROOT', private_root)
    private_mkdir(private_root, parents=True)
    assert private_root.stat().st_mode & 0o777 == 0o700
    output = new_output(private_root / 'run')
    write(output / 'run.json', {'synthetic': True})
    write_text(output / 'reviewer.html', '<html>synthetic</html>')
    for path in (output, output / 'run.json', output / 'reviewer.html'):
        assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match='EXCLUSIVE_CREATE'):
        private_open(output / 'run.json', 'w')
