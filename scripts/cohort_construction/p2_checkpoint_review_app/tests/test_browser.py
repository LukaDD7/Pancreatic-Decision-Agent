"""Browser interaction -> actual export -> independent Python/FHIR receiver."""
import json
import os
from pathlib import Path
import subprocess

from scripts.cohort_construction.p2_checkpoint_fhir import (
    doctor_bundle_from_packet,response_bundle_from_submission,submission_from_response_bundle,
)
from scripts.cohort_construction.p2_checkpoint_review import validate_submission
from scripts.cohort_construction.p2_checkpoint_review_app.build import build_reviewer_html
from scripts.cohort_construction.p2_checkpoint_review_app.synthetic_demo import synthetic_packet
from .test_app import NODE

NODE_MODULES='/Users/lucachangretta/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'


def test_CE_APP004_actual_browser_export_roundtrips_through_python_and_fhir(tmp_path):
    packet=synthetic_packet()
    for name,malicious in [('reader.html',False),('reader-xss.html',True)]:
        p=synthetic_packet(literal_markup=malicious)
        (tmp_path/name).write_text(build_reviewer_html(doctor_bundle_from_packet(p),assignment_id='DEMO-REVIEW-001',expected_packet_sha256=p['packet_sha256']))
    script=Path(__file__).with_name('browser_checks.cjs')
    run=subprocess.run([NODE,str(script),str(tmp_path/'reader.html'),str(tmp_path/'reader-xss.html'),str(tmp_path)],
        env=dict(os.environ,NODE_PATH=NODE_MODULES),capture_output=True,text=True,timeout=90)
    assert run.returncode==0,run.stderr
    result=json.loads((tmp_path/'browser_receipt.json').read_text())
    assert result['status']=='PASS' and result['network_requests']==0
    submission=json.loads((tmp_path/'browser-submission.json').read_text())
    assert validate_submission(submission,packet)['status']=='PASS'
    bundle=response_bundle_from_submission(submission,packet)
    restored=submission_from_response_bundle(bundle,packet,expected_reviewer_assignment_id='DEMO-REVIEW-001')
    assert restored==submission
