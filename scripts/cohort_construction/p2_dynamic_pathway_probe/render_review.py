"""Render analyst-selected source cards from private, provenance-bearing annotations."""
from pathlib import Path
import argparse
import json


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[3])
    args=ap.parse_args()
    out=args.repo/'local_artifacts/p2_dynamic_pathway_probe_v0.1_20261021'
    cases=json.loads((out/'five_case_review.json').read_text())
    source={n['record_key']:n for n in map(json.loads,(out/'review_patient_context.jsonl').read_text().splitlines())}
    cards=['# 动态手术路径：五个新患者的开发复核卡\n',
           '仅用于研究开发复核；非盲评包、非医生Reference。来源按文书分开，不拼成新的临床事实。\n']
    for case in cases:
        n=source[case['record_key']]
        assert n==case['source_note']
        assert n['text'][case['entry_start']:case['entry_end']]==case['entry_quote']
        cards += [f'\n## {case["case_id"]}：{case["title"]}\n',
                  f'入口：{case["entry_id"]}；患者键：{n["patient_uid"]}；文书键：{n["record_key"]}\n',
                  f'文书：{n["document_type"]}；正文头：{n["body_time"]}；系统：{n["system_time"]}；时间标记：{n["time_quality_flags"]}\n',
                  f'来源：{n["source_parquet"]}；source_file={n["source_file"]}；source_row={n["source_row"]}\n',
                  f'解释：{case["interpretation"]}\n','### 命中原文\n',case['entry_quote']+'\n',
                  '### 该份文书完整原文\n',n['text']+'\n','### 同一源就诊的相关记录索引与原句\n']
        for r in case['same_visit_inspection_only']:
            cards += [f'\n- {r["document_type"]}；正文头{r["body_time"]}；系统{r["system_time"]}；键{r["record_key"]}；标记{r["time_quality_flags"]}\n']
            for m in r['excerpts'][:3]:
                assert source[r['record_key']]['text'][m['start']:m['end']]==m['quote']
                cards.append('  '+m['quote']+'\n')
    (out/'五个新患者-动态手术路径原文卡-v0.1-20261021.md').write_text('\n'.join(cards))
    print(f'Five source cards rendered; source spans verified for {len(cases)} cases.')


if __name__=='__main__':main()
