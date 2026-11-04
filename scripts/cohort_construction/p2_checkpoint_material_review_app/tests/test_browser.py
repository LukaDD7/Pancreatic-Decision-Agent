"""Actual fictional Chrome export is independently accepted by Python."""
import json
import os
from pathlib import Path
import subprocess

from scripts.cohort_construction.p2_checkpoint_material_review_app.build import build_researcher_html
from scripts.cohort_construction.p2_checkpoint_material_review_app.receive import receive_submission
from scripts.cohort_construction.p2_checkpoint_material_review_app.synthetic_demo import synthetic_assignment
from .test_app import NODE

NODE_MODULES='/Users/lucachangretta/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'


def test_CE_MRA007_actual_chrome_export_to_python_receiver_and_offline_negative_controls(tmp_path):
    assignment=synthetic_assignment()
    for name,value in [('researcher.html',assignment),('xss.html',synthetic_assignment(literal_markup=True)),
                       ('unloaded.html',synthetic_assignment(load_excluded=False))]:
        (tmp_path/name).write_text(build_researcher_html(value),encoding='utf-8')
    html=build_researcher_html(assignment).replace(assignment['assignment_sha256'],'0'*64)
    (tmp_path/'tampered.html').write_text(html,encoding='utf-8')
    command=[NODE,str(Path(__file__).with_name('browser_checks.cjs')),
             *[str(tmp_path/name) for name in ['researcher.html','xss.html','unloaded.html','tampered.html']],str(tmp_path)]
    run=subprocess.run(command,env=dict(os.environ,NODE_PATH=NODE_MODULES),capture_output=True,text=True,timeout=120)
    assert run.returncode==0,run.stderr
    receipt=json.loads((tmp_path/'browser_receipt.json').read_text())
    assert receipt['status']=='PASS' and receipt['network_requests']==0
    response=json.loads((tmp_path/'browser-response.json').read_text())
    audit=receive_submission(assignment,response)
    assert audit['status']=='PENDING' and audit['reviewer_type']=='human'
    assert audit['independence_attested'] is False
    assert audit['segment_assessments'][0]['exact_quote']=='虚构观察甲。'
