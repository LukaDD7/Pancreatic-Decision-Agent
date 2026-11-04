# 四类临床记录模式探查 v0.1

先读 `docs/P2四类示例-NCCN核对与全P0模式探查-v0.1-20261021.md`。

本模块独立于旧 P2 批处理。从固定 P0 的全部 2,707 人中检索四类句内共现；不使用 P1 状态过滤，不产生临床 Reference、决策点或 episode，也不根据后续结局确认早期判断。四类窄规则不构成完整的 P2 检索词表，零命中不表示患者不相关或资料不可用。

文件分工：`rules.json` 是完整正则；`probe.py` 负责范围、来源核验、共现检索、旧角色附注、抽样和聚合；`tests/test_probe.py` 使用合成对照。`annotate()` 复用冻结的 P2 `Labeler` 与配置，仅给复核排序；报告已指出旧标记有误判/漏检，不能用 `current_opinion_hint=False` 删除命中。

依赖 Python 3.12、pyarrow，以及本机忽略追踪的输入：

- `local_artifacts/p1_quality_v0.2/results/patient_quality.jsonl`：取全部患者键与源行数，**不用 QC 状态**。
- `local_artifacts/source_cache_20261020/clinical_cache_manifest.json` 及其缓存文件：265 份原始临床 parquet；逐文件校验 SHA-256，逐患者核对原始行数。
- `local_artifacts/p2_localization_v0.3_20261020/semantic_annotations.json`：仅检查原四例能否找回，不作为独立验证集。

从版本库根目录运行：

```bash
python -m unittest discover -s scripts/cohort_construction/p2_four_pattern_probe/tests -v
python -m scripts.cohort_construction.p2_four_pattern_probe.probe --repo "$PWD"
```

`--repo` 指输入/输出所在仓库。清单中缓存绝对路径需可用；文件哈希不符、患者源行数不符时直接失败。正文 UTF-8 解码失败时用 GB18030；XML 标签转换为换行只用于索引视图，输出坐标相对于该视图，源文件和原行保留，不能把视图偏移冒充原始字节偏移。

本轮按文书名称排除护理、同意书、交接、核查、物品、告知、申请单、报告卡；没有研究期过滤，也没有正文“胰”字过滤。该范围本身可能漏掉相关记录，不宣称全库临床召回率。按 `。；;换行` 分隔，同片段词组共现不证明因果、当次性或操作对象。

输出目录 `local_artifacts/p2_four_pattern_probe_20261021/`：

- `hits.jsonl`：所有命中，含未被旧角色规则优先标记的条目；
- `matched_notes.jsonl`：匹配文书全文及来源；只有匹配文书，**不是这些患者的完整时间线**；
- `development_sample.json`：四类×标记/未标记分层，每层最多 6 条，种子 20261021；
- `development_review_notes.json`：本轮分析者对代表性片段的说明，非医生独立盲评；
- `summary.json`、`run_manifest.json`：聚合及输入/规则/脚本/依赖哈希。

原文、标识、样本不进 Git；仅聚合统计复制到 `docs/p2-four-pattern-probe-v0.1-20261021.json`。类别可重叠；“句子签名”仅是同一患者的空白归一化文字，不是临床去重算法。
