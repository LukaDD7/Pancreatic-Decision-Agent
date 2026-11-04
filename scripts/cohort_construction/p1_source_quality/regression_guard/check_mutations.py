"""Prove selected guards detect reintroduced bugs in a temporary source copy.

Never edits live code or patient data. A caught mutation must be an assertion
failure in the named regression, not an import/syntax/environment error.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt',type=Path,required=True)
    args=parser.parse_args()
    active=json.loads((HERE/'active_release.json').read_text())['package_directory']
    source=HERE.parent/active
    original=(source/'labels.py').read_text()
    before=hashlib.sha256((source/'labels.py').read_bytes()).hexdigest()
    bline=next(line for line in original.splitlines() if 'b = b or bool(re.search(' in line)
    cases=[
        ('W02_front_negation','test_rules.ScopeRegressionTests.test_front_negation',
         "local_prefix = local_prefix_match.group() if local_prefix_match else ''", "local_prefix = ''"),
        ('R08_template_context','test_known_regressions.KnownDefectContracts.test_R08_structured_lesion_can_omit_repeated_noun',
         'if tumour_section and re.search', 'if False and re.search'),
        ('R02_branch_parent','test_known_regressions.KnownDefectContracts.test_R02_branch_only_in_contact_summary_never_fills_parent',
         "summary=heading == '与血管接触' and not branch_only", "summary=heading == '与血管接触'"),
        ('W10_narrative_B','test_known_regressions.KnownDefectContracts.test_W10_narrative_metastatic_domain_is_represented',
         bline,'    b = b')]
    records=[]
    with tempfile.TemporaryDirectory(prefix='pdac-synthetic-mutation-') as tmp:
        root=Path(tmp)
        dest=root/'scripts/cohort_construction/p1_source_quality'/active
        shutil.copytree(source,dest,ignore=shutil.ignore_patterns('__pycache__','release.json'))
        env=os.environ.copy();env['PYTHONPATH']=str(root)+os.pathsep+str(dest/'tests');env['PYTHONDONTWRITEBYTECODE']='1'
        baseline=subprocess.run([sys.executable,'-m','unittest',*[c[1] for c in cases]],cwd=root,env=env,capture_output=True,text=True,timeout=30)
        assert baseline.returncode==0,baseline.stderr
        for name,test,old,new in cases:
            assert original.count(old)==1,(name,'mutation anchor not unique')
            (dest/'labels.py').write_text(original.replace(old,new,1))
            result=subprocess.run([sys.executable,'-m','unittest',test],cwd=root,env=env,capture_output=True,text=True,timeout=30)
            caught=result.returncode!=0 and 'FAIL:' in result.stderr and 'ERROR:' not in result.stderr and test.split('.')[-1] in result.stderr
            assert caught,(name,result.stderr)
            records.append({'mutation':name,'test':test,'baseline_passed':True,'reintroduced_bug_caught':True})
    assert before==hashlib.sha256((source/'labels.py').read_bytes()).hexdigest()
    receipt={'active_package':active,'mutation_checks':records,'caught':len(records),'live_source_unchanged':True,'source_sha256':before}
    args.receipt.parent.mkdir(parents=True,exist_ok=True)
    args.receipt.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'mutations_caught':len(records),'live_source_unchanged':True}))


if __name__=='__main__':
    main()
