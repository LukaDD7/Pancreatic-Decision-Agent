"""Require registered counterexamples plus migrated v0.1/v0.2 regression coverage."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite): yield from flatten(item)
        else: yield item


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--receipt',type=Path);a=p.parse_args()
    root=Path(__file__).resolve().parents[3];sys.path.insert(0,str(root))
    module=Path(__file__).parent
    legacy=json.loads((module.parent/'p2_decision_ir/test_receipt.json').read_text())
    names=[s for s in legacy['command'][3:] if not s.startswith('-')]
    names += ['scripts.cohort_construction.p2_relation_graph.tests.test_graph','scripts.cohort_construction.p2_relation_graph.tests.test_validation']
    registry=json.loads((module/'validation_regression_catalog_v0.3.json').read_text())
    inherited=json.loads((module/'regression_catalog.json').read_text())
    required=[r['required_test'] for r in inherited['cases']+registry['cases']]
    if len(required)!=len(set(required)):raise RuntimeError('duplicate registered test')
    suite=unittest.defaultTestLoader.loadTestsFromNames(names)
    tests=list(flatten(suite));ids={t.id() for t in tests}
    missing=set(required)-ids
    current={i for i in ids if '.p2_relation_graph.' in i}
    if missing or current-set(required):raise RuntimeError('required test missing or current test unregistered')
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    skipped=[t.id() for t,_ in result.skipped if t.id() in required]
    receipt={'version':'p2-validation-0.3','success':result.wasSuccessful() and not skipped,
             'tests_run':result.testsRun,'required_tests':len(required),'required_skipped':skipped,
             'modules':names,'log':log.getvalue(),
             'code_sha256':{str(x.relative_to(root)):hashlib.sha256(x.read_bytes()).hexdigest()
                 for x in module.rglob('*.py')},'scope':'synthetic regression tests; not clinical validation'}
    if a.receipt:
        a.receipt.parent.mkdir(parents=True,exist_ok=True)
        with a.receipt.open('x') as f:json.dump(receipt,f,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in receipt.items() if k not in {'log','code_sha256','modules'}},ensure_ascii=False))
    if not receipt['success']:print(log.getvalue())
    return 0 if receipt['success'] else 1


if __name__=='__main__':sys.exit(main())
