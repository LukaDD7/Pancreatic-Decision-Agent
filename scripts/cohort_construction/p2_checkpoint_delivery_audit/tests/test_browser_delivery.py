"""Browser rendering, download and authoritative Python/FHIR response replay."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

from scripts.cohort_construction.p2_checkpoint_fhir import (
    response_bundle_from_submission, submission_from_response_bundle,
)
from scripts.cohort_construction.p2_checkpoint_review import validate_submission
from scripts.cohort_construction.p2_checkpoint_review_app.synthetic_demo import synthetic_packet, HIDDEN_SENTINEL
from scripts.cohort_construction.p2_checkpoint_review_app.tests.test_app import NODE
from scripts.cohort_construction.p2_checkpoint_delivery_audit.tests.test_delivery_audit import (
    DeliveryAuditTests, build_delivery_audit_request, inspect_delivery_artifacts,
)

NODE_MODULES = '/Users/lucachangretta/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'
BROWSER_CHECKS = Path(__file__).with_name('browser_delivery_checks.cjs')


def test_actual_chrome_delivery_read_download_and_fhir_replay(tmp_path):
    fixture = DeliveryAuditTests()
    material = fixture.material_set()
    delivery = fixture.staging_deliveries(material)[0]
    html = tmp_path / 'episode-staging.html'
    html.write_text(delivery['html'], encoding='utf-8')
    request = build_delivery_audit_request(material, fixture.staging_deliveries(material))
    run = subprocess.run([NODE, str(BROWSER_CHECKS), str(html), str(tmp_path)],
        env=dict(os.environ, NODE_PATH=NODE_MODULES), capture_output=True, text=True, timeout=90)
    assert run.returncode == 0, run.stderr
    receipt = json.loads((tmp_path / 'browser-receipt.json').read_text())
    assert receipt['status'] == 'PASS' and receipt['network_requests'] == 0
    assert not receipt['page_errors']
    assert receipt['visible_masked_choice_absent']
    assert receipt['hidden_decision_absent_from_dom']
    assert receipt['checkpoint_id'] == delivery['checkpoint_id']
    assert receipt['assignment_id'] == delivery['assignment_id']
    # Browser checks are a mirror only. The actual downloaded response is checked
    # against the externally supplied frozen packet and its assignment ID.
    submission = json.loads((tmp_path / 'browser-submission.json').read_text())
    assert validate_submission(submission, delivery['packet'])['status'] == 'PASS'
    bundle = response_bundle_from_submission(submission, delivery['packet'])
    assert submission_from_response_bundle(bundle, delivery['packet'],
        expected_reviewer_assignment_id=delivery['assignment_id']) == submission
    assert not inspect_delivery_artifacts([delivery])['blocking_findings']
    assert request['audit_scope']['actual_artifacts_not_preview']


def test_hidden_markup_cannot_be_reintroduced_into_actual_doctor_html(tmp_path):
    packet = synthetic_packet()
    from scripts.cohort_construction.p2_checkpoint_fhir import doctor_bundle_from_packet
    from scripts.cohort_construction.p2_checkpoint_review_app.build import build_reviewer_html
    html = build_reviewer_html(doctor_bundle_from_packet(packet),
        assignment_id='DEMO-REVIEW-001', expected_packet_sha256=packet['packet_sha256'])
    assert HIDDEN_SENTINEL not in html
    file = tmp_path / 'doctor.html'
    file.write_text(html, encoding='utf-8')
    run = subprocess.run([NODE, str(BROWSER_CHECKS), str(file), str(tmp_path)],
        env=dict(os.environ, NODE_PATH=NODE_MODULES), capture_output=True, text=True, timeout=90)
    assert run.returncode == 0, run.stderr
    receipt = json.loads((tmp_path / 'browser-receipt.json').read_text())
    assert receipt['status'] == 'PASS' and receipt['network_requests'] == 0
