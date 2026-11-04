# P1 v0.2 对抗性审计（2026-10-21）

这是审计程序，不是新版 P1 筛选器。它导入冻结 v0.2 实现，以 12 个纯合成反例复现设计错位或解析错误，再只读统计冻结队列中相关规则的覆盖范围。不会修改原文、P1 成员或 P2 结果。

在独立本地版本库根目录运行：

```bash
/opt/anaconda3/bin/python3.12 scripts/cohort_construction/p1_source_quality/adversarial_audit_20261021/audit.py --output docs/p1-adversarial-audit-v0.1-20261021.json
```

需要本地忽略的 `local_artifacts/p1_quality_v0.2/results/` 中患者、报告和汇总文件；这些不是公开仓库输入。输出只包含聚合数、合成文字、冻结代码及输入哈希，无真实患者标识和病历文字。

12 个反例的“复现成功”说明当前代码确实存在相应行为，不代表行为正确，更不是临床错误率。规则覆盖人数也不代表修订后新增纳入人数。

报告见 `docs/P1规则对抗审计-v0.1-20261021.md`。新版规则尚未实现，本次应单独编号为审计版本，不给旧筛选器冒标 v0.3。
