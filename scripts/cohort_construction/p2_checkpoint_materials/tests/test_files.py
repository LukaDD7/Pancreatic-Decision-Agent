import json
from pathlib import Path
from unittest.mock import patch
import pytest

from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import build
from scripts.cohort_construction.p2_checkpoint_materials.__main__ import prepare_files
from scripts.cohort_construction.p2_checkpoint_materials.tests.test_prepare import source,event


def inputs(tmp_path):
    s=source();e=event(s)
    s.update(kind='document',family='synthetic',title='Synthetic source',clocks={},clock={'value':None},clock_basis='unknown',provenance={})
    events=tmp_path/'events.json';events.write_text(json.dumps([e],ensure_ascii=False))
    fhir=tmp_path/'source.json';fhir.write_text(json.dumps(build([s],'synthetic-patient',[],[]),ensure_ascii=False))
    return events,fhir


def call(tmp_path,events,fhir):
    return prepare_files(events,fhir,event_id='candidate-e1',decision_task_id='task',visibility_policy_version='draft-1',output=tmp_path/'out')


def test_file_bridge_records_hashes_and_creates_no_socket(tmp_path):
    events,fhir=inputs(tmp_path);before=(events.read_bytes(),fhir.read_bytes())
    with patch('socket.socket',side_effect=AssertionError('NETWORK_FORBIDDEN')) as socket:
        result=call(tmp_path,events,fhir)
    assert socket.call_count==0
    assert result['model_api_calls']==0
    receipt=json.loads((tmp_path/'out/input_receipt.json').read_text())
    assert receipt['status']=='PREPARED_RESEARCHER_REVIEW_REQUIRED'
    assert receipt['event_input_sha256'] and receipt['source_fhir_input_sha256']
    assert (events.read_bytes(),fhir.read_bytes())==before
    assert not list((tmp_path/'out').glob('*packet*'))


def test_file_bridge_refuses_existing_output(tmp_path):
    events,fhir=inputs(tmp_path);call(tmp_path,events,fhir)
    with pytest.raises(FileExistsError):call(tmp_path,events,fhir)


def test_input_read_failure_retains_rejection_receipt(tmp_path):
    with pytest.raises(FileNotFoundError):call(tmp_path,tmp_path/'absent',tmp_path/'absent-too')
    receipt=json.loads((tmp_path/'out/input_receipt.json').read_text())
    assert receipt['status']=='REJECTED' and receipt['model_api_calls']==0
