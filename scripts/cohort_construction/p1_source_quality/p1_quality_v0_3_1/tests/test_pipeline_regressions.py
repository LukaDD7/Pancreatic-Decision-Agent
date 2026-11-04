"""Exercise the real CLI with a tiny synthetic-content, full-size P0 fixture.

2707 empty/small files preserve the frozen cohort-size contract. No clinical
data/cache from the user's cohort is read. Fixtures are deleted after tests.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import pyarrow as pa
import pyarrow.parquet as pq
from scripts.cohort_construction.p1_source_quality.p1_quality_v0_3_1.labels import normalize

MODULE='scripts.cohort_construction.p1_source_quality.p1_quality_v0_3_1.run'
REPO=Path(__file__).resolve().parents[5]


def dump(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False)+'\n')


def dump_lines(path,values):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(v,ensure_ascii=False)+'\n' for v in values))


def read_lines(path):
    return [json.loads(s) for s in path.read_text().splitlines() if s.strip()]


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory(prefix='pdac-synthetic-regression-') as tmp:
        root=Path(tmp)
        old=root/'local_artifacts/p1_quality_v0.2/results'
        old.mkdir(parents=True)
        (root/'docs').mkdir();(root/'receipts').mkdir()
        manifest={}; patients=[]; image_path=None
        for i in range(2707):
            uid=f'SYNTHETIC-{i:04d}'
            p=root/'synthetic_input/batch_fixture/patients'/uid/'imaging_reports_all_sources.jsonl'
            p.parent.mkdir(parents=True);p.write_bytes(b'')
            if i==0:image_path=p
            manifest[str(p)]={'bytes':0,'sha256':hashlib.sha256(b'').hexdigest()}
            patients.append({'patient_uid':uid,'alias':f'S{i:04d}','state':'NOT_MET_V2','identity_index_ok':True,
                             'clinical_source_rows':2 if i==1 else 1 if i==2 else 0})
        rows=[]; proofs=[]
        for i,(text,method,verified,origin) in enumerate([
            ('胰头肿块20×18mm。SMA无接触。胰十二指肠切除术后。','胰腺CT',False,'synthetic_excerpt'),
            ('胰头肿块20×18mm。SMA无接触。胰十二指肠切除术后。','胰腺CT增强',True,'synthetic_original'),
            ('胰头肿块本次20×18mm，较前25×22mm缩小。SMA受侵。胰十二指肠切除术后。','胰腺CT增强',True,'synthetic_original'),
            ('胰头肿块。SMA关系难以判断。胰十二指肠切除术后。','胰腺CT增强',False,'synthetic_excerpt')]):
            row={'patient_uid':'SYNTHETIC-0000','exam_datetime':'2024-01-01','exam_method':method,
                 'original_findings':text,'original_impression':'','exam_no_raw':'123456.0',
                 'index_report_uid':'SYNTHETIC-REPORT-1','index_origin':origin,'source_file_name':'synthetic.csv',
                 'source_sheet':None,'source_row':i,'record_cluster_id':i}
            rows.append(row)
            ref={k:row[k] for k in ['index_report_uid','index_origin','source_file_name','source_sheet','source_row','record_cluster_id']}
            ref['source_verification']={'verified':verified,'reason':'SYNTHETIC_PROOF'}
            content_hash=hashlib.sha256((normalize(text)+'\n').encode()).hexdigest()
            proofs.append({'patient_uid':row['patient_uid'],'parsed':{'normalized_text_sha256':content_hash},'source_refs':[ref]})
        dump_lines(image_path,rows)
        manifest[str(image_path)]={'bytes':image_path.stat().st_size,'sha256':hashlib.sha256(image_path.read_bytes()).hexdigest()}
        clinical=root/'synthetic_input/clinical.parquet'
        raw_notes=[
            {'patient_uid':'SYNTHETIC-0001','文书名称':'首次病程记录','文书内容':'PDAC。CA19-9：120。ECOG 1。拟完善腹部增强CT后评估。'.encode(),
             'source_record_id':None,'source_file':'synthetic.xml','source_row':1,'create_time':'2024-01-01','admission_time':'2024-01-01'},
            {'patient_uid':'SYNTHETIC-0001','文书名称':'首次病程记录','文书内容':bytes([255]),
             'source_record_id':'synthetic-bad-encoding','source_file':'synthetic.xml','source_row':2,'create_time':'2024-01-01','admission_time':'2024-01-01'},
            {'patient_uid':'SYNTHETIC-0002','文书名称':'首次病程记录','文书内容':'诊断：PDAC。\n手术指征：\n远处转移：\n'.encode(),
             'source_record_id':'synthetic-empty-template','source_file':'synthetic.xml','source_row':3,'create_time':'2024-01-01','admission_time':'2024-01-01'}]
        pq.write_table(pa.Table.from_pylist(raw_notes),clinical)
        metadata={'cached_path':str(clinical),'bytes':clinical.stat().st_size,'sha256':hashlib.sha256(clinical.read_bytes()).hexdigest()}
        manifest[str(clinical)]={k:metadata[k] for k in ['bytes','sha256']}
        dump(root/'local_artifacts/source_cache_20261020/clinical_cache_manifest.json',{'files':{str(clinical):metadata}})
        dump(old/'input_manifest.json',manifest)
        dump(old/'summary.json',{'imaging_input_rows':len(rows)})
        dump_lines(old/'patient_quality.jsonl',patients)
        dump_lines(old/'report_quality.jsonl',proofs)
        yield root,image_path,rows,manifest,patients


def run_cli(root):
    env=os.environ.copy();env['PYTHONPATH']=str(REPO)
    return subprocess.run([sys.executable,'-m',MODULE,'--repo',str(root)],cwd=REPO,env=env,
                          capture_output=True,text=True,timeout=90)


class PipelineRegressionTests(unittest.TestCase):
    def test_W11_W12_D8_actual_pipeline_and_order_invariance(self):
        with fixture() as (root,image_path,rows,manifest,patients):
            before={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in manifest}
            result=run_cli(root)
            self.assertEqual(result.returncode,0,result.stderr)
            out=root/'local_artifacts/p1_quality_v0.3.1/results'
            first=read_lines(out/'image_annotations.jsonl')
            p={r['patient_uid']:r for r in read_lines(out/'patient_quality.jsonl')}
            self.assertEqual(p['SYNTHETIC-0000']['state'],'INCLUDE_V3')
            self.assertGreater(p['SYNTHETIC-0000']['flags']['SAME_REPORT_UID_DIFFERENT_CONTENT_REVIEW'],0)
            self.assertGreater(p['SYNTHETIC-0000']['flags']['IMAGE_ORIGINAL_NOT_INDEPENDENTLY_RECONCILED'],0)
            self.assertGreater(p['SYNTHETIC-0000']['flags']['POSTOPERATIVE_CONTEXT_CLUE_ONLY'],0)
            self.assertEqual(p['SYNTHETIC-0001']['state'],'INCLUDE_V3')
            self.assertFalse(p['SYNTHETIC-0001']['source_coverage_complete'])
            self.assertEqual(p['SYNTHETIC-0001']['flags']['CLINICAL_DECODE_ERROR'],1)
            self.assertEqual(p['SYNTHETIC-0002']['state'],'REVIEW_V3')
            self.assertEqual(p['SYNTHETIC-0003']['state'],'REVIEW_V3')
            self.assertTrue(all(r['p2_access']=='NOT_DETERMINED_BY_P1_STATE' for r in p.values()))
            # A run must not rewrite any source file.
            self.assertEqual(before,{k:hashlib.sha256(Path(k).read_bytes()).hexdigest() for k in before})
            dump_lines(image_path,list(reversed(rows)))
            manifest[str(image_path)]['sha256']=hashlib.sha256(image_path.read_bytes()).hexdigest()
            dump(root/'local_artifacts/p1_quality_v0.2/results/input_manifest.json',manifest)
            again=run_cli(root)
            self.assertEqual(again.returncode,0,again.stderr)
            self.assertEqual(first,read_lines(out/'image_annotations.jsonl'))
            self.assertEqual(p,{r['patient_uid']:r for r in read_lines(out/'patient_quality.jsonl')})

    def test_source_hash_mismatch_aborts_instead_of_publishing(self):
        with fixture() as (root,image_path,rows,manifest,patients):
            manifest[str(image_path)]['sha256']='0'*64
            dump(root/'local_artifacts/p1_quality_v0.2/results/input_manifest.json',manifest)
            result=run_cli(root)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Frozen imaging input changed',result.stderr)
            self.assertFalse((root/'docs/p1-relevance-usability-v0.3.1-20261021.json').exists())

    def test_incomplete_source_row_coverage_aborts(self):
        with fixture() as (root,image_path,rows,manifest,patients):
            patients[1]['clinical_source_rows']+=1
            dump_lines(root/'local_artifacts/p1_quality_v0.2/results/patient_quality.jsonl',patients)
            result=run_cli(root)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Full-P0 source row coverage',result.stderr)
            self.assertFalse((root/'docs/p1-relevance-usability-v0.3.1-20261021.json').exists())


if __name__=='__main__':
    unittest.main()
