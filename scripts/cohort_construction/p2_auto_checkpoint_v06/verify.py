"""Offline P2 regression, version diff and frozen-output integrity receipts."""
import argparse
import ast
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT=Path(__file__).resolve().parents[3]
TEST_ROOTS=[
    'scripts/cohort_construction/p2_auto_checkpoint_v04/tests',
    'scripts/cohort_construction/p2_auto_checkpoint_v05_refine',
    'scripts/cohort_construction/p2_auto_checkpoint_model_rerun',
    'scripts/cohort_construction/p2_auto_checkpoint_shape_retry',
    'scripts/cohort_construction/p2_checkpoint_packet/tests',
    'scripts/cohort_construction/p2_auto_checkpoint_v06/tests',
]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def integrity(hashes):
    records=[]
    for name,expected in sorted(hashes.items()):
        path=ROOT/name
        actual=sha(path) if path.is_file() else None
        records.append(dict(path=name,expected_sha256=expected,actual_sha256=actual,unchanged=actual==expected))
    return dict(file_count=len(records),all_unchanged=all(r['unchanged'] for r in records),files=records)


def registry_check():
    test_files=set()
    for directory in TEST_ROOTS:
        test_files.update(p for p in (ROOT/directory).rglob('test_*.py') if '__pycache__' not in p.parts)
    definitions=set()
    for path in test_files:
        definitions.update(n.name for n in ast.walk(ast.parse(path.read_text()))
                           if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)))
    maps=list((ROOT/'scripts/cohort_construction/p2_auto_checkpoint_v04').rglob('*issue_map*.json'))
    maps+=list((ROOT/'scripts/cohort_construction/p2_auto_checkpoint_v06').rglob('*issue_map*.json'))
    maps+=[ROOT/'scripts/cohort_construction/p2_checkpoint_packet/regression_issue_map.json']
    records=[]
    def required(value):
        if isinstance(value,dict):
            for key,child in value.items():
                if key in ('required_tests','tests') and isinstance(child,list):
                    for name in child:
                        if isinstance(name,str) and name.split('.')[-1].startswith('test_'):
                            yield name
                else:
                    yield from required(child)
        elif isinstance(value,list):
            for child in value:yield from required(child)
    for path in sorted(set(maps)):
        refs=list(required(json.loads(path.read_text())))
        missing=[name for name in refs if name.split('.')[-1] not in definitions]
        records.append(dict(path=str(path.relative_to(ROOT)),sha256=sha(path),required_test_count=len(refs),missing=missing))
    return dict(all_required_tests_present=all(not r['missing'] for r in records),
                registry_count=len(records),required_test_references=sum(r['required_test_count'] for r in records),registries=records)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    baseline=args.baseline.resolve();output=args.output.resolve()
    if output.exists():raise ValueError('VALIDATION_REQUIRES_NEW_OUTPUT_DIRECTORY')
    manifest=json.loads((baseline/'baseline_manifest.json').read_text())
    # The source snapshots themselves must remain exactly as frozen.
    for name,expected in manifest['source_snapshot_hashes'].items():
        if sha(baseline/'source_snapshot'/name)!=expected:
            raise ValueError('BASELINE_SOURCE_SNAPSHOT_CHANGED:'+name)
    before=integrity(manifest['protected_existing_result_hashes'])
    registries=registry_check()
    if not before['all_unchanged']:raise ValueError('PROTECTED_RESULTS_ALREADY_CHANGED')
    if not registries['all_required_tests_present']:raise ValueError('REQUIRED_REGRESSION_TEST_MISSING')
    output.mkdir(parents=True,exist_ok=False)
    current=set(manifest['source_snapshot_hashes'])
    packages=('p2_auto_checkpoint_v04','p2_auto_checkpoint_v06','p2_auto_checkpoint_model_rerun',
              'p2_auto_checkpoint_shape_retry','p2_checkpoint_packet','p2_auto_checkpoint_v05_refine')
    for package in packages:
        current.update(str(p.relative_to(ROOT)) for p in (ROOT/'scripts/cohort_construction'/package).rglob('*')
                       if p.is_file() and p.suffix in ('.py','.json','.md') and '__pycache__' not in p.parts)
    current.update(str(p.relative_to(ROOT)) for p in (ROOT/'config').glob('p2_auto_checkpoint_v06*.json'))
    source_hashes={name:sha(ROOT/name) for name in sorted(current)}
    changed=[];diffs=[]
    for name in sorted(current):
        old=baseline/'source_snapshot'/name;new=ROOT/name
        old_hash=manifest['source_snapshot_hashes'].get(name)
        if old_hash==source_hashes[name]:continue
        changed.append(dict(path=name,before_sha256=old_hash,after_sha256=source_hashes[name],change='modified' if old.exists() else 'added'))
        diffs.extend(difflib.unified_diff(old.read_text().splitlines(keepends=True) if old.exists() else [],
            new.read_text().splitlines(keepends=True),fromfile='baseline/'+name,tofile='v06/'+name))
    (output/'source_changes.diff').write_text(''.join(diffs))
    command=[sys.executable,'-m','pytest','-q',*TEST_ROOTS,'--junitxml='+str(output/'tests.xml')]
    env=dict(os.environ,PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    run=subprocess.run(command,cwd=ROOT,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    (output/'tests.log').write_text(run.stdout)
    after=integrity(manifest['protected_existing_result_hashes'])
    unchanged_during_tests=all(sha(ROOT/name)==value for name,value in source_hashes.items())
    suites=ET.parse(output/'tests.xml').getroot() if (output/'tests.xml').exists() else None
    failed_tests=[]
    if suites is not None:
        failed_tests=[dict(classname=t.attrib.get('classname'),name=t.attrib.get('name'))
                      for t in suites.iter('testcase') if t.find('failure') is not None or t.find('error') is not None]
    counts=re.search(r'(\d+) passed(?:, (\d+) subtests passed)?',run.stdout)
    receipt=dict(created_at_utc=datetime.now(timezone.utc).isoformat(),
        status='PASS' if run.returncode==0 and after['all_unchanged'] and unchanged_during_tests else 'FAIL',
        command=command,python=sys.version,pytest_exit_code=run.returncode,
        passed=int(counts[1]) if counts else None,subtests_passed=int(counts[2] or 0) if counts else None,
        failed_tests=failed_tests,registry_check=registries,
        protected_results_before=before,protected_results_after=after,
        source_hashes=source_hashes,source_code_unchanged_during_tests=unchanged_during_tests,
        changes=changed,source_diff_sha256=sha(output/'source_changes.diff'),
        baseline_manifest_sha256=sha(baseline/'baseline_manifest.json'),
        clinical_model_run_started=False,clinical_accuracy_measured=False,
        p1_files_modified=False,p1_regression_guard_required=False,
        p1_guard_reason='Only P2 files were modified in this implementation; P1 rules, parsing, cohort outputs and source checks are unchanged.')
    write(output/'validation_receipt.json',receipt)
    print(json.dumps({k:receipt[k] for k in ('status','passed','subtests_passed','pytest_exit_code')},ensure_ascii=False))
    if receipt['status']!='PASS':raise SystemExit(1)


if __name__=='__main__':main()
