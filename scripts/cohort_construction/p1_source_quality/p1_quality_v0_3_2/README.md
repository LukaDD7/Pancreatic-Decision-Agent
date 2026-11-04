# P1 v0.3.2：存储迁移与安全输出

本版本仅迁移 v0.3.1 的存储与运行保护；纳入规则、提取正则、质量标记、P1/P2 关系和 Episode 规则均未改变。P1 仍负责研究相关性与记录可用性；程序标签不是临床 Reference，状态不构成 P2 绝对门槛。

## 迁移范围与版本边界

- 冻结 v0.2 的结果、证明和输入 manifest 继续只读使用，位置为 `private/p1_quality_v0.2/`。输入文件的存在不改变历史结果的证据等级。
- 影像输入仍以 v0.2 manifest 的 SHA-256 核验；共享的字节相同缓存位于 `private/p1_quality_v0.3/source_cache/`，文件名绑定预期 hash。缓存不存在或不是预期 hash 时读取并核验冻结原件，不凭存在复用；不同版本内容不覆盖同一缓存文件。
- 临床 parquet 直接读取原件并核验 SHA-256，不再要求不存在的 `local_artifacts/source_cache_20261020/clinical_cache_manifest.json`。为复现旧版临床/引用输出顺序，`--prior-run private/p1_quality_v0.3.1/results` 仅恢复其 `input_hashes` 已绑定的输入顺序；两版输入集合和 hash 必须一致，不用旧输出充当临床真值。若冻结 manifest 内保留 `/local_artifacts/` 绝对路径，只在相对后缀的 `private/` 文件实际存在且 hash 一致时采用；不因此推断原文件内容或临床事实改变。
- 每次运行创建新目录 `private/p1_quality_v0.3.2/<run-id>/`，目录 0700、新文件 0600、独占创建。已有目录和公共发布 JSON 不得覆盖；开发重复运行使用 `--no-public-release` 和新的 `--run-id`。
- 本次已完成本地无模型重放与坐标核验；不包含临床算法修复、临床 Reference 创建或真实远程模型运行。旧存储文件未删除，历史结果继续保存在各版本目录。

## 文件职责

- `rules.json`：继承 v0.3.1 规则；仅版本及新输出名改变。
- `labels.py`、`references.py`：继承提取和引用核对逻辑，不是临床 GT。
- `run.py`：读取冻结输入并输出新运行；`verify.py`：对新运行核验坐标与输入 hash，分别保留回执。
- `tests/`：原有 54 项提取、引用与 CLI 回归，加 `S03` 迁移正对照及不可覆盖反例。

在项目虚拟环境中运行：

```bash
.venv/bin/python -m unittest discover -s scripts/cohort_construction/p1_source_quality/p1_quality_v0_3_2/tests -v
.venv/bin/python -m scripts.cohort_construction.p1_source_quality.p1_quality_v0_3_2.run \
  --repo "$PWD" --run-id results \
  --prior-run private/p1_quality_v0.3.1/results
.venv/bin/python -m scripts.cohort_construction.p1_source_quality.p1_quality_v0_3_2.verify \
  --repo "$PWD" --run-id results --no-public-receipt
```

正式默认 `run-id=results` 只适用于该目录尚不存在时。重复开发运行保留旧公共 aggregate/receipt，使用 `--no-public-release` 和 `--no-public-receipt`；这些选项不跳过私有坐标与输入核验。未先通过新实现完整合成守卫和输入核验，不应启动真实队列运行。

## 核验强度与保留限制

影像原始来源核验仍沿用 v0.2 已保存证明，仅在索引输入字节 hash 完全一致时继承，不是新一轮原始记录簇独立核对。临床记录使用完整 L1 源文件、患者关联、物理行和原文 hash 回溯。处理视图不替换原文；ABC 是全历史检索覆盖，不是同一时点状态；差异、未检出、无法判断、未找到和未来计划均保留各自语义。

统一保护入口为 `scripts/cohort_construction/p1_source_quality/regression_guard/run_checks.py`；它同时运行冻结 v0.2 的 51 项历史测试。新增存储问题 `S03` 必须登记并执行，不能只运行旧快照。
