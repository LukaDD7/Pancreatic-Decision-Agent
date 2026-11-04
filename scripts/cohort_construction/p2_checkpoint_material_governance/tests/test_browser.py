"""Actual Chrome reads automated opinions without automating the human response."""
import json
import os
from pathlib import Path
import subprocess

from scripts.cohort_construction.p2_checkpoint_material_governance.review import run_review_chain
from scripts.cohort_construction.p2_checkpoint_material_governance.workbench import build_auto_review_assignment, render_auto_opinion
from scripts.cohort_construction.p2_checkpoint_material_review_app.build import build_researcher_html
from scripts.cohort_construction.p2_checkpoint_material_review_app.receive import receive_submission
from scripts.cohort_construction.p2_checkpoint_material_review_app.synthetic_demo import synthetic_inputs
from scripts.cohort_construction.p2_checkpoint_material_review_app.tests.test_app import NODE
from scripts.cohort_construction.p2_checkpoint_materials.review import create_review_draft

from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_material_governance import review_for

NODE_MODULES='/Users/lucachangretta/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'


def test_automated_opinion_visible_but_human_export_remains_independent(tmp_path):
    documents,sources,_=synthetic_inputs()
    manifest,compiled=documents['manifest'],documents['compiled']
    answer=review_for(manifest,compiled,status='AUTO_REVIEW_HOLD',blocking=True,
        operations=[dict(operation_id='O1',operation='REQUEST_HUMAN_REVIEW',
                         source_id='S-fictional-included',reason='虚构材料待实际人员复核')])
    chain=run_review_chain(manifest,compiled,lambda *args:answer)
    draft=create_review_draft(compiled,manifest,documents['preparation_audit'],reviewer_id='AI-DRAFT-NOT-HUMAN')
    package=build_auto_review_assignment(chain,draft,documents['preparation_audit'],
        assignment_id='SYNTHETIC-AUTO-BROWSER',coverage_sources=sources)
    html=render_auto_opinion(build_researcher_html(package['assignment']),package['assignment'],package['auto_review'])
    (tmp_path/'researcher.html').write_text(html,encoding='utf-8')
    command=[NODE,str(Path(__file__).with_name('browser_checks.cjs')),str(tmp_path/'researcher.html'),str(tmp_path)]
    run=subprocess.run(command,env=dict(os.environ,NODE_PATH=NODE_MODULES),capture_output=True,text=True,timeout=120)
    assert run.returncode==0,run.stderr
    receipt=json.loads((tmp_path/'browser_receipt.json').read_text())
    assert receipt['status']=='PASS' and receipt['network_requests']==0
    response=json.loads((tmp_path/'browser-response.json').read_text())
    audit=receive_submission(package['assignment'],response)
    assert audit['status']=='PENDING' and audit['reviewer_type']=='human'
    assert audit['independence_attested'] is False


def test_actual_revisions_new_source_and_rehashed_bad_history_rejected_in_chrome(tmp_path):
    from copy import deepcopy
    from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_chain_validation import fictional_chain
    from scripts.cohort_construction.p2_checkpoint_material_governance.workbench import (
        _canonical_chain_documents, receive_auto_review_submission,
    )
    from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
    documents,sources,chain,_=fictional_chain(include_source=True,graph=True)
    package=build_auto_review_assignment(chain,documents['draft'],documents['preparation_audit'],
        assignment_id='SYNTHETIC-REVISED-AUTO-BROWSER',selected_event=documents['selected_event'],coverage_sources=sources)
    html=render_auto_opinion(build_researcher_html(package['assignment']),package['assignment'],package['auto_review'])
    (tmp_path/'researcher.html').write_text(html,encoding='utf-8')
    counterexamples=[]
    for name in ('input_hash','wrong_target','empty_pass','cleared_ledger','rewritten_operation','pending_actions'):
        opinion=deepcopy(package['auto_review']);broken=opinion['chain']
        if name=='input_hash': broken['rounds'][0]['input_compiled_sha256']='0'*64
        elif name=='wrong_target': broken['rounds'][0]['review']['target']['checkpoint_id']='other-checkpoint'
        elif name=='empty_pass': broken['rounds']=[];broken['final_binding']['round_count']=0
        elif name=='cleared_ledger': broken['rounds'][0]['ledger']['operations']=[]
        elif name=='rewritten_operation': broken['rounds'][0]['ledger']['operations'][0]['operation']['reason']='tampered ledger reason'
        else: broken['rounds'][-1]['review']['required_actions']=['待实际人员完成的虚构阻断项']
        # Recompute every helper checksum: semantic history checks must still reject.
        opinion['canonical_documents']=_canonical_chain_documents(broken)
        opinion['validation_receipt']['chain_sha256']=canonical_sha256(broken)
        counterexamples.append(dict(name=name,opinion=opinion))
    (tmp_path/'bad-opinions.json').write_text(json.dumps(counterexamples,ensure_ascii=False),encoding='utf-8')
    command=[NODE,str(Path(__file__).with_name('browser_checks.cjs')),str(tmp_path/'researcher.html'),str(tmp_path),'AUTO_REVIEW_PASS']
    run=subprocess.run(command,env=dict(os.environ,NODE_PATH=NODE_MODULES),capture_output=True,text=True,timeout=120)
    assert run.returncode==0,run.stderr
    response=json.loads((tmp_path/'browser-response.json').read_text())
    audit=receive_auto_review_submission(package,response)
    assert audit['compiled_sha256']==chain['compiled']['compiled_sha256']
    assert audit['compiled_sha256']!=documents['compiled']['compiled_sha256']
    assert audit['status']=='PENDING' and audit['independence_attested'] is False


def test_tampered_history_boot_is_rejected_by_chrome(tmp_path):
    from copy import deepcopy
    import re
    from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_chain_validation import fictional_chain
    from scripts.cohort_construction.p2_checkpoint_material_governance.workbench import _canonical_chain_documents
    from scripts.cohort_construction.p2_checkpoint_material_review_app.build import canonical_json
    from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
    documents,sources,chain,_=fictional_chain()
    package=build_auto_review_assignment(chain,documents['draft'],documents['preparation_audit'],
        assignment_id='SYNTHETIC-BAD-HISTORY-BOOT',coverage_sources=sources)
    html=render_auto_opinion(build_researcher_html(package['assignment']),package['assignment'],package['auto_review'])
    forged=deepcopy(package['auto_review']);forged['chain']['rounds'][0]['review']['target']['checkpoint_id']='another-checkpoint'
    forged['canonical_documents']=_canonical_chain_documents(forged['chain'])
    forged['validation_receipt']['chain_sha256']=canonical_sha256(forged['chain'])
    raw=canonical_json(forged).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    html=re.sub(r'(<script type="application/json" id="auto-opinion-data">).*?(</script>)',lambda match:match[1]+raw+match[2],html,flags=re.S)
    (tmp_path/'forged.html').write_text(html,encoding='utf-8')
    command=[NODE,str(Path(__file__).with_name('browser_checks.cjs')),str(tmp_path/'forged.html'),str(tmp_path),'INVALID']
    run=subprocess.run(command,env=dict(os.environ,NODE_PATH=NODE_MODULES),capture_output=True,text=True,timeout=120)
    assert run.returncode==0,run.stderr
    assert json.loads((tmp_path/'browser_receipt.json').read_text())['status']=='PASS'
