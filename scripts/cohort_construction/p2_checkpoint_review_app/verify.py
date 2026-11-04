"""Run inherited/new P2 regression and verify protected historical artifacts."""
import argparse
from datetime import datetime,timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

from scripts.cohort_construction.p2_checkpoint_material_governance.run import _json_exclusive
from scripts.cohort_construction.p2_checkpoint_material_review_app.build import private_mkdir, private_open, require_private_output

ROOT=Path(__file__).resolve().parents[3]
PACKAGES=['p2_auto_checkpoint_v04','p2_auto_checkpoint_v05_refine','p2_auto_checkpoint_model_rerun',
 'p2_auto_checkpoint_shape_retry','p2_auto_checkpoint_v06','p2_checkpoint_packet',
 'p2_checkpoint_review','p2_checkpoint_fhir','p2_checkpoint_review_app','p2_checkpoint_materials',
 'p2_checkpoint_material_review_app','p2_checkpoint_material_governance',
 'p2_checkpoint_reconstruction_audit','p2_checkpoint_delivery_audit',
 'p2_checkpoint_ready_pipeline','p2_checkpoint_real_validation']
TEST_ROOTS=[
 'scripts/cohort_construction/p2_auto_checkpoint_v04/tests',
 'scripts/cohort_construction/p2_auto_checkpoint_v05_refine',
 'scripts/cohort_construction/p2_auto_checkpoint_model_rerun',
 'scripts/cohort_construction/p2_auto_checkpoint_shape_retry',
 'scripts/cohort_construction/p2_auto_checkpoint_v06/tests',
 'scripts/cohort_construction/p2_auto_checkpoint_v06/experiments/tests',
 'scripts/cohort_construction/p2_auto_checkpoint_v06/independent_validation/tests',
 *['scripts/cohort_construction/'+p+'/tests' for p in PACKAGES[5:]],
]


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value):_json_exclusive(path,value)


def source_hashes():
    return {str(p.relative_to(ROOT)):sha(p) for package in PACKAGES
        for p in sorted((ROOT/'scripts/cohort_construction'/package).rglob('*'))
        if p.is_file() and p.suffix in ('.py','.cjs','.js','.css','.html','.json','.md')
        and '__pycache__' not in p.parts and not p.name.startswith(('validation_receipt','differential_receipt'))}


def collect_test_nodes():
    """Use pytest's actual collector; AST method names are not execution evidence."""
    command=[sys.executable,'-m','pytest','--collect-only','-q',*TEST_ROOTS]
    result=subprocess.run(command,cwd=ROOT,env=dict(os.environ,PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'),
        capture_output=True,text=True)
    nodes=[line.strip() for line in result.stdout.splitlines()
           if re.match(r'^[^\s]+\.py::',line)]
    log=result.stdout+result.stderr
    return dict(format='pytest-collection-receipt-0.2.0',
        status='PASS' if result.returncode==0 and nodes and len(nodes)==len(set(nodes)) else 'FAIL',
        command=command,exit_code=result.returncode,node_ids=nodes,collected_count=len(nodes),
        log_sha256=hashlib.sha256(log.encode()).hexdigest(),log=log)


def _test_aliases(node):
    # Pytest IDs may contain hierarchy punctuation inside parameter IDs. Only
    # split the collection hierarchy before its first parameter suffix, then
    # append that suffix byte-for-byte to the method name used by JUnit.
    base,marker,parameter=node.partition('[')
    suffix=marker+parameter
    path,*names=base.split('::')
    module=path[:-3].replace('/','.')
    return dict(node=node,base=base,path=path,
        dotted=module+'.'+'.'.join(names)+suffix,dotted_base=module+'.'+'.'.join(names),
        names='::'.join(names)+suffix,base_names='::'.join(names),
        leaf=names[-1],leaf_full=names[-1]+suffix)


def resolve_test_reference(reference,node_ids):
    """Resolve module/class/method exactly, allowing documented legacy styles."""
    aliases=[_test_aliases(node) for node in node_ids]
    reference_base=reference.split('[',1)[0]
    if reference.startswith('scripts.'):
        matches=[a for a in aliases if reference in (a['dotted'],a['dotted_base'])]
    elif '::' in reference_base:
        path,suffix=reference.split('::',1)
        matches=[a for a in aliases if (a['path']==path or a['path'].endswith('/'+path))
                 and suffix in (a['names'],a['base_names'])]
    elif '.' in reference_base:
        # A short module.method is accepted only for the precise collected
        # function, never by throwing away an asserted class or module.
        matches=[a for a in aliases if a['dotted']==reference or a['dotted_base']==reference
                 or a['dotted'].endswith('.'+reference) or a['dotted_base'].endswith('.'+reference)]
    else:
        matches=[a for a in aliases if reference in (a['leaf'],a['leaf_full'])]
    if not matches or len({a['base'] for a in matches})!=1:
        return []
    return [a['node'] for a in matches]


def junit_execution_receipt(path,node_ids):
    """Bind passed JUnit testcases to exact collected nodes, including classes."""
    aliases={a['dotted']:a['node'] for a in map(_test_aliases,node_ids)}
    passed=set(); failed=set(); skipped=set(); unknown=[]
    try:
        root=ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return dict(format='pytest-execution-receipt-0.2.0',status='FAIL',
            passed_node_ids=[],failed_node_ids=[],skipped_node_ids=[],
            not_executed_node_ids=list(node_ids),unknown_testcase_ids=[],
            junit_sha256=sha(path) if Path(path).is_file() else None,
            error='JUNIT_MISSING_OR_UNREADABLE')
    for case in root.findall('.//testcase'):
        identity=case.get('classname','')+'.'+case.get('name','')
        node=aliases.get(identity)
        if node is None:
            unknown.append(identity)
            continue
        if case.find('failure') is not None or case.find('error') is not None:
            failed.add(node)
        elif case.find('skipped') is not None:
            skipped.add(node)
        else:
            passed.add(node)
    # A duplicate pass must never erase a failed or skipped instance.
    passed-=failed|skipped
    missing=set(node_ids)-passed-failed-skipped
    return dict(format='pytest-execution-receipt-0.2.0',
        status='PASS' if not failed and not skipped and not missing and not unknown else 'FAIL',
        passed_node_ids=sorted(passed),failed_node_ids=sorted(failed),
        skipped_node_ids=sorted(skipped),not_executed_node_ids=sorted(missing),
        unknown_testcase_ids=unknown,junit_sha256=sha(path))


def required_test_registry(collection=None, execution=None):
    # Tests inject tiny collection receipts; this function does not recursively
    # start pytest when it is itself exercised by the regression suite.
    if collection is None:collection=collect_test_nodes()
    nodes=collection.get('node_ids',[])
    def required(value):
        if isinstance(value,dict):
            for key,child in value.items():
                if key in ('required_tests','tests') and isinstance(child,list):
                    for name in child:
                        if isinstance(name,str):
                            method=name.split('[',1)[0].split('::')[-1].split('.')[-1]
                            if key=='required_tests' or method.startswith('test_'):yield name
                        elif key=='required_tests':yield 'INVALID_NONSTRING_REQUIRED_TEST_REFERENCE'
                else:yield from required(child)
        elif isinstance(value,list):
            for child in value:yield from required(child)
    records=[]
    passed=set(execution.get('passed_node_ids',[])) if execution is not None else set()
    for package in PACKAGES:
        for p in sorted((ROOT/'scripts/cohort_construction'/package).rglob('*issue_map*.json')):
            refs=list(required(json.loads(p.read_text())))
            resolved={r:resolve_test_reference(r,nodes) for r in refs}
            records.append(dict(path=str(p.relative_to(ROOT)),sha256=sha(p),required_test_count=len(refs),
                missing=[r for r in refs if not resolved[r]],resolved_node_ids=resolved,
                not_executed=[] if execution is None else [r for r in refs
                    if resolved[r] and not set(resolved[r]).issubset(passed)]))
    return dict(status='PASS' if collection.get('status')=='PASS' and
        all(not r['missing'] and not r['not_executed'] for r in records) else 'FAIL',
        collection_status=collection.get('status'),execution_checked=execution is not None,
        registry_count=len(records),required_test_reference_count=sum(r['required_test_count'] for r in records),registries=records)


def protected_integrity(expected):
    failures=[]
    for name,value in expected.items():
        p=Path(name);p=p if p.is_absolute() else ROOT/p
        actual=sha(p) if p.is_file() else None
        if actual!=value:failures.append(dict(path=name,expected=value,actual=actual))
    return dict(status='PASS' if not failures else 'FAIL',file_count=len(expected),failures=failures)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output=require_private_output(args.output,
        private_root=ROOT/'private/p2_checkpoint_next_20261028')
    if args.output.exists():raise ValueError('VALIDATION_REQUIRES_NEW_OUTPUT_DIRECTORY')
    old_receipt=ROOT/'private/p2_checkpoint_v06_20261028/validation_release_retry/validation_receipt.json'
    old=json.loads(old_receipt.read_text())
    expected=json.loads((ROOT/'private/p2_checkpoint_next_20261028/model_validation/protected_old_hashes.json').read_text())
    before=protected_integrity(expected);collection=collect_test_nodes();registry=required_test_registry(collection);code=source_hashes()
    private_mkdir(args.output,parents=True)
    with private_open(args.output/'test_collection.log','x') as stream:stream.write(collection.pop('log'))
    write(args.output/'test_collection_receipt.json',collection)
    if before['status']!='PASS' or registry['status']!='PASS':
        receipt=dict(status='FAIL',phase='PRECHECK',protected_before=before,registry=registry,
            collection_receipt_sha256=sha(args.output/'test_collection_receipt.json'),
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            clinical_model_calls_in_regression=0,clinical_accuracy_measured=False,p1_files_modified=False)
        write(args.output/'validation_receipt.json',receipt)
        print(json.dumps(dict(status='FAIL',phase='PRECHECK'),ensure_ascii=False))
        raise SystemExit(1)
    changes=[];diff=[]
    baseline=ROOT/'private/p2_checkpoint_v06_20261028/baseline/source_snapshot'
    for name,value in code.items():
        old_hash=old['source_hashes'].get(name)
        if old_hash==value:continue
        changes.append(dict(path=name,before_sha256=old_hash,after_sha256=value,
                            kind='added' if old_hash is None else 'modified'))
        # Older baseline source is available for inherited files; additions are
        # complete in this diff. Never copy patient records into source diffs.
        previous=(baseline/name).read_text() if (baseline/name).is_file() else ''
        diff.extend(difflib.unified_diff(previous.splitlines(keepends=True),(ROOT/name).read_text().splitlines(keepends=True),
            fromfile='baseline/'+name,tofile='next/'+name))
    with private_open(args.output/'code_changes.diff','x') as stream:stream.write(''.join(diff))
    command=[sys.executable,'-m','pytest','-q',*TEST_ROOTS,'--junitxml='+str(args.output.resolve()/'tests.xml')]
    result=subprocess.run(command,cwd=ROOT,env=dict(os.environ,PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'),
        capture_output=True,text=True)
    log=result.stdout+result.stderr
    with private_open(args.output/'tests.log','x') as stream:stream.write(log)
    after=protected_integrity(expected);stable=source_hashes()==code
    execution=junit_execution_receipt(args.output/'tests.xml',collection['node_ids'])
    write(args.output/'test_execution_receipt.json',execution)
    registry=required_test_registry(collection,execution)
    counts=re.search(r'(\d+) passed(?:, (\d+) subtests passed)?',log)
    receipt=dict(created_at_utc=datetime.now(timezone.utc).isoformat(),
        status='PASS' if result.returncode==0 and after['status']=='PASS' and stable and execution['status']=='PASS' and registry['status']=='PASS' else 'FAIL',
        command=command,python=sys.version,exit_code=result.returncode,
        passed=int(counts[1]) if counts else None,subtests_passed=int(counts[2] or 0) if counts else None,
        protected_before=before,protected_after=after,registry=registry,source_hashes=code,
        collection_receipt_sha256=sha(args.output/'test_collection_receipt.json'),
        execution_receipt_sha256=sha(args.output/'test_execution_receipt.json'),
        source_files_unchanged_during_tests=stable,changes=changes,code_diff_sha256=sha(args.output/'code_changes.diff'),
        inherited_validation_receipt_sha256=sha(old_receipt),
        clinical_model_calls_in_regression=0,clinical_accuracy_measured=False,p1_files_modified=False)
    write(args.output/'validation_receipt.json',receipt)
    print(json.dumps({k:receipt[k] for k in ('status','passed','subtests_passed','exit_code')},ensure_ascii=False))
    if receipt['status']!='PASS':raise SystemExit(1)


if __name__=='__main__':main()
