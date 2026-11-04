# P1 已知错误的提交保护

`known_cases.json` 为已知问题的稳定编号及必需测试清单；`active_release.json` 指向当前实现。运行器会拒绝遗漏编号、删除/改名后未迁移的必需测试或跳过这些测试，然后执行当前实现全部测试及冻结 v0.2 的51项历史回归。

```bash
/opt/anaconda3/bin/python3.12 scripts/cohort_construction/p1_source_quality/regression_guard/run_checks.py
```

可另运行 `check_mutations.py --receipt <输出路径>`，在临时副本恢复4个已知错误，验证对应回归测试确实失败；不修改实际生产代码或数据。

覆盖包括审计 W01–W12、读取顺序 D8、早期修复 R01–R11、引用核对 L01–L05 和全量运行保护 S01–S02。`test_pipeline_regressions.py` 用纯合成输入执行真实 CLI，检查元数据顺序不变性、版本差异/术后/来源标记并存、局部解码错误、空白模板、原文不写回及失败时不发布。

本独立版本库启用跟踪的 `.githooks/pre-commit`。普通 `git commit` 涉及 P1 代码、规则、测试、保护器或 AGENTS 时自动运行。保护器要求相关工作区内容与暂存内容一致，避免测了未提交的代码；新增实现版本必须同时迁移 active release 指针和回归覆盖，不能只让旧版本测试通过。

其他本地克隆启用方式：

```bash
git config --local core.hooksPath .githooks
git config --local pdac.testPython /opt/anaconda3/bin/python3.12
```

原项目旧 `.git` 不在本次配置范围。Git 提供的显式绕过选项仍然存在；这里保证正常提交流程会执行保护，不声称可以消除所有未来错误。新增已知问题应新增稳定编号、失败反例、阳性/阴性对照及相应必需测试，并在变更说明记录原因。
