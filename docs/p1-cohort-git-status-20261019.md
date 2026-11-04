# 队列处理 Git 状态：2026-10-19 更新

此前“尚未提交”的记录对应原项目仓库，其 `.git/HEAD`、索引与历史对象仍为 iCloud 云端占位，原历史不可读。未重建、覆盖或改写原 `.git`。

已在不受 iCloud 管理的本地路径建立独立队列处理版本库：

`/Users/lucachangretta/Agent_Workspace/pdac-cohort-version-control`

以下为根据既有冻结研究运行补建的真实 Git 提交，提交日期为 2026-10-19，不冒充历史运行日期；它们属于独立本地历史，尚未接入原项目 Git。

| 版本标签 | Commit | 队列状态 |
|---|---|---|
| p1-source-quality-v0.1 | `41c68ac3a2cfd155cb30b90753ff9252444ff69d` | 通过 1941 / 待核 672 / 当前不满足 94 |
| p1-source-quality-v0.2 | `abbbad1efdefe4bbb7bfebc71d6a8f477d5568f0` | 通过 2004 / 待核 620 / 当前不满足 83 |
| p1-pass-audit-20261019 | `9ae1fe8f79069345b5b133d0afe01da73e38c19e` | 审计 2004 名通过者；纳入状态未改 |

两版回归测试在新库分别通过 32 和 51 项。Git 对象完整性检查通过、工作区干净；未配置远程，也未推送。

代码、规则、版本差异、聚合报告及冻结文件校验凭据进入 Git。既有 P1 v0.1/v0.2 运行目录及通过者审计目录共 64 个文件，已逐文件核对 SHA-256 并复制到本库忽略的 `local_artifacts/`；患者级表、索引、原文和样本包没有进入 Git。原始数据库批次仍在原项目，其既有输入清单及哈希随运行制品保留。

本地 Git bundle 已生成并通过 `git bundle verify`，位置为本地库 `local_artifacts/p1-cohort-history-20261019.bundle`。机器可读凭据见原项目 `private/p1_local_git_receipt_20261019.json`。

当前不需要用户操作即可继续队列处理及提交版本。如需接回原项目历史，需先恢复原 Git 云端文件，再检查暂存区并导入明确范围的代码和凭据。不能将独立库 `.git` 覆盖到原项目。
