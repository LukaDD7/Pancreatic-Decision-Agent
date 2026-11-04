"""Exact pytest collection and execution checks use tiny fictional fixtures.

Do not recursively collect this project's complete suite from a registry test.
The release verifier collects the complete suite once and persists its receipt.
"""
import json
from pathlib import Path

import pytest

from scripts.cohort_construction.p2_checkpoint_review_app import verify


@pytest.fixture
def registry_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, 'ROOT', tmp_path)
    monkeypatch.setattr(verify, 'PACKAGES', ['fictional'])
    monkeypatch.setattr(verify, 'TEST_ROOTS', ['scripts/cohort_construction/fictional/tests'])
    directory=tmp_path/'scripts/cohort_construction/fictional'
    directory.mkdir(parents=True)
    path=directory/'regression_issue_map.json'
    nodes=['scripts/cohort_construction/fictional/tests/test_fixture.py::test_function',
           'scripts/cohort_construction/fictional/tests/test_fixture.py::TestReal::test_method',
           'scripts/cohort_construction/fictional/tests/test_fixture.py::test_param[a]',
           'scripts/cohort_construction/fictional/tests/test_fixture.py::test_param[b]']
    collection=dict(status='PASS', node_ids=nodes)
    def set_refs(refs):
        path.write_text(json.dumps({'CE-FICTIONAL':{'required_tests':refs}}))
    return path, collection, set_refs


def test_issue_registry_resolves_pytest_and_unittest_reference_styles(registry_fixture):
    _,collection,set_refs=registry_fixture
    set_refs(['scripts.cohort_construction.fictional.tests.test_fixture.test_function',
              'scripts.cohort_construction.fictional.tests.test_fixture.TestReal.test_method',
              'test_fixture.py::TestReal::test_method',
              'test_fixture.py::test_function', 'test_fixture.test_function', 'test_param'])
    registry=verify.required_test_registry(collection)
    assert registry['status']=='PASS'
    assert registry['required_test_reference_count']==6
    assert registry['registries'][0]['missing']==[]
    assert len(registry['registries'][0]['resolved_node_ids']['test_param'])==2


def test_issue_registry_rejects_unknown_test_names(registry_fixture):
    _,collection,set_refs=registry_fixture
    set_refs(['test_fixture.py::test_nonexistent_counterexample'])
    registry=verify.required_test_registry(collection)
    assert registry['status']=='FAIL'
    assert registry['registries'][0]['missing']==['test_fixture.py::test_nonexistent_counterexample']


@pytest.mark.parametrize('reference', [
    'scripts.cohort_construction.fictional.tests.test_fixture.WrongClass.test_method',
    'scripts.cohort_construction.fictional.tests.wrong_module.TestReal.test_method',
    'test_fixture.py::WrongClass::test_method',
    'wrong_file.py::TestReal::test_method',
])
def test_AUD002_registry_rejects_wrong_class_or_module_even_when_method_exists(registry_fixture, reference):
    _,collection,set_refs=registry_fixture
    set_refs([reference])
    result=verify.required_test_registry(collection)
    assert result['status']=='FAIL'
    assert result['registries'][0]['missing']==[reference]


def test_AUD002_actual_pytest_collection_does_not_count_uncollected_class(registry_fixture):
    path,_,set_refs=registry_fixture
    test_directory=path.parent/'tests'
    test_directory.mkdir()
    (test_directory/'test_fixture.py').write_text(
        'class HiddenClass:\n    def test_method(self): pass\n\n'
        'class TestReal:\n    def test_method(self): pass\n')
    collection=verify.collect_test_nodes()
    assert collection['status']=='PASS'
    assert len(collection['node_ids'])==1
    assert '::TestReal::test_method' in collection['node_ids'][0]
    set_refs(['scripts.cohort_construction.fictional.tests.test_fixture.HiddenClass.test_method'])
    result=verify.required_test_registry(collection)
    assert result['status']=='FAIL'
    set_refs(['scripts.cohort_construction.fictional.tests.test_fixture.TestReal.test_method'])
    assert verify.required_test_registry(collection)['status']=='PASS'


def test_AUD002_required_test_must_have_actual_passed_execution(registry_fixture):
    _,collection,set_refs=registry_fixture
    set_refs(['scripts.cohort_construction.fictional.tests.test_fixture.TestReal.test_method', 'test_param'])
    passed=[collection['node_ids'][1],collection['node_ids'][2]]
    registry=verify.required_test_registry(collection,dict(passed_node_ids=passed))
    assert registry['status']=='FAIL'
    assert registry['registries'][0]['not_executed']==['test_param']
    passed.append(collection['node_ids'][3])
    registry=verify.required_test_registry(collection,dict(passed_node_ids=passed))
    assert registry['status']=='PASS'
    assert registry['execution_checked'] is True


def test_AUD002_junit_receipt_binds_class_parameter_and_success(registry_fixture, tmp_path):
    _,collection,_=registry_fixture
    xml=tmp_path/'tests.xml'
    xml.write_text('<testsuites><testsuite>'
        '<testcase classname="scripts.cohort_construction.fictional.tests.test_fixture" name="test_function"/>'
        '<testcase classname="scripts.cohort_construction.fictional.tests.test_fixture.TestReal" name="test_method"/>'
        '<testcase classname="scripts.cohort_construction.fictional.tests.test_fixture" name="test_param[a]"/>'
        '<testcase classname="scripts.cohort_construction.fictional.tests.test_fixture" name="test_param[b]"/>'
        '</testsuite></testsuites>')
    receipt=verify.junit_execution_receipt(xml,collection['node_ids'])
    assert receipt['status']=='PASS'
    assert set(receipt['passed_node_ids'])==set(collection['node_ids'])
    xml.write_text(xml.read_text().replace('name="test_param[b]"/>',
                   'name="test_param[b]"><skipped/></testcase>'))
    receipt=verify.junit_execution_receipt(xml,collection['node_ids'])
    assert receipt['status']=='FAIL'
    assert receipt['skipped_node_ids']==[collection['node_ids'][3]]
    assert collection['node_ids'][3] not in receipt['passed_node_ids']


def test_AUD002_ambiguous_short_reference_cannot_bind_different_module(registry_fixture):
    _,collection,set_refs=registry_fixture
    collection['node_ids'].append('scripts/cohort_construction/fictional/tests/test_other.py::test_function')
    set_refs(['test_function'])
    assert verify.required_test_registry(collection)['status']=='FAIL'
    set_refs(['test_fixture.py::test_function'])
    assert verify.required_test_registry(collection)['status']=='PASS'


@pytest.mark.parametrize('parameter', ['test_fixture.py::WrongClass::test_method',
                                       'wrong_file.py::TestReal::test_method',
                                       'scripts.module::Class.test_method[inner]'])
def test_AUD002_parameter_punctuation_preserved_in_junit_and_registry(registry_fixture, tmp_path, parameter):
    _,collection,set_refs=registry_fixture
    base='scripts/cohort_construction/fictional/tests/test_fixture.py::TestReal::test_parameter'
    node=base+'['+parameter+']'
    collection['node_ids']=[node]
    # Exact parameterized references and all-variant references use the same
    # real class; dots and :: inside the parameter never become hierarchy.
    set_refs(['scripts.cohort_construction.fictional.tests.test_fixture.TestReal.test_parameter',
              'scripts.cohort_construction.fictional.tests.test_fixture.TestReal.test_parameter['+parameter+']',
              'test_fixture.py::TestReal::test_parameter['+parameter+']',
              'test_fixture.TestReal.test_parameter['+parameter+']',
              'test_parameter['+parameter+']'])
    assert verify.required_test_registry(collection)['status']=='PASS'
    import xml.etree.ElementTree as ET
    xml=tmp_path/'punctuation.xml'
    root=ET.Element('testsuites');suite=ET.SubElement(root,'testsuite')
    ET.SubElement(suite,'testcase',classname='scripts.cohort_construction.fictional.tests.test_fixture.TestReal',
                  name='test_parameter['+parameter+']')
    ET.ElementTree(root).write(xml,encoding='unicode')
    execution=verify.junit_execution_receipt(xml,collection['node_ids'])
    assert execution['status']=='PASS'
    assert execution['passed_node_ids']==[node]
    assert verify.required_test_registry(collection,execution)['status']=='PASS'
    # A wrong actual class cannot be legitimized by a class-shaped parameter.
    set_refs(['test_fixture.py::WrongClass::test_parameter['+parameter+']'])
    assert verify.required_test_registry(collection,execution)['status']=='FAIL'
    root.find('.//testcase').set('classname','scripts.cohort_construction.fictional.tests.test_fixture.WrongClass')
    ET.ElementTree(root).write(xml,encoding='unicode')
    execution=verify.junit_execution_receipt(xml,collection['node_ids'])
    assert execution['status']=='FAIL'
    assert execution['passed_node_ids']==[]
    assert execution['not_executed_node_ids']==[node]
