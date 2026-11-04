"""Fail on missing/skipped known-defect tests, then run active and legacy suites."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

HERE=Path(__file__).resolve().parent
SOURCE=HERE.parent
REPO=HERE.parents[3]
REQUIRED_CASES=({f'W{i:02d}' for i in range(1,13)} | {'D8'} |
                {f'R{i:02d}' for i in range(1,12)} |
                {f'L{i:02d}' for i in range(1,6)} | {'S01','S02'})


def flatten(suite):
    for item in suite:
        if isinstance(item,unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def validate_staged(active):
    files=subprocess.check_output(['git','diff','--cached','--name-only','-z'],cwd=REPO).decode().split('\0')
    for name in files:
        match=re.match(r'scripts/cohort_construction/p1_source_quality/(p1_quality_v[^/]+)/.*\.(?:py|json)$',name)
        if match and match[1]!=active:
            raise RuntimeError('Staged parser snapshot is not active release: '+name+
                               '. Preserve frozen releases; point active_release.json at the new tested snapshot.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged',action='store_true')
    parser.add_argument('--receipt',type=Path)
    args=parser.parse_args()
    sys.path.insert(0,str(REPO))
    config=json.loads((HERE/'active_release.json').read_text())
    if args.staged:
        validate_staged(config['package_directory'])
    registry=json.loads((HERE/config['known_cases']).read_text())['cases']
    ids=[c['id'] for c in registry]
    if len(ids)!=len(set(ids)) or not REQUIRED_CASES<=set(ids):
        raise RuntimeError('Known-case registry is incomplete or has duplicate IDs')
    loader=unittest.TestLoader()
    suite=loader.discover(str(SOURCE/config['package_directory']/'tests'))
    if loader.errors:
        raise RuntimeError('\n'.join(loader.errors))
    found={t.id() for t in flatten(suite)}
    required={t for c in registry for t in c['required_tests']}
    if not required<=found:
        raise RuntimeError('Required regression tests missing/renamed: '+str(sorted(required-found)))
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    skipped_required=[test.id() for test,reason in result.skipped if test.id() in required]
    if not result.wasSuccessful() or skipped_required:
        raise RuntimeError('Active suite failed or required known-defect tests were skipped')
    legacy=subprocess.run([sys.executable,'-m','unittest','discover','-s',
                           str(SOURCE/config['legacy_test_directory']),'-p','test_*.py'],cwd=REPO,capture_output=True,text=True)
    sys.stderr.write(legacy.stdout+legacy.stderr)
    count=re.search(r'Ran (\d+) tests?',legacy.stdout+legacy.stderr)
    if legacy.returncode or not count or int(count[1])!=51:
        raise RuntimeError('Frozen v0.2 legacy regression suite failed/changed')
    receipt={'version':config['version'],'known_case_count':len(registry),'required_test_methods':len(required),
             'active_tests':result.testsRun,'legacy_tests':int(count[1]),'total_tests':result.testsRun+int(count[1]),
             'required_tests_skipped':skipped_required,'success':True,
             'scope':'Synthetic tests, CLI fixture and frozen legacy baseline; not clinical validation.'}
    if args.receipt:
        args.receipt.parent.mkdir(parents=True,exist_ok=True)
        args.receipt.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(receipt,ensure_ascii=False))


if __name__=='__main__':
    main()
