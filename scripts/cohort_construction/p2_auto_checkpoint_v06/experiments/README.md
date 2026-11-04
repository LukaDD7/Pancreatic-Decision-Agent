# 固定 DeepSeek 模型的开发验证

本目录把“补全技术失败病例”“最终事件依赖检查”“LLM 根据机械报错修改提案”分开记录。任何一个程序状态或候选 Checkpoint 数量变化，都不代表医生已确认临床正确性。

`fixed_model.py prepare` 创建新目录，复制既有成功请求缓存，冻结输入原始字节、配置和代码，并记录旧文件 hash。`probe` 运行仅缓存预检；缺失请求只登记，不调用网络。缓存按实际 endpoint/body hash 读取，格式提醒后的请求保留自己的实际 hash。

技术补全由正式 v0.6 runner 执行，采用实验目录里的 `technical_completion_config.json`。该配置保留 DeepSeek 模型、温度及输出 token 上限，关闭支持文书扩展和两个语义反馈入口，只启用全部 LLM 复核后的事件依赖检查。输出目录必须不存在。实际请求和传输尝试保存在独立缓存的 `invocations`；旧缓存只读。

`fixed_model.py feedback` 在已技术完成的旧 DeepSeek 病例中，按病例和原始请求 hash 排序，预先登记两个有机械报错的复核请求组，每例最多一组。程序只把原证据池、原提案及机械报错交给同一模型修改一次，不读取开发 Reference 或增加资料。程序保留修改前后提案、证据池相等性及再校验结果。专家尚需独立判断修改是否符合临床含义。

`summarize.py summarize` 汇总新请求、失败类别及三例完成状态。`release` 仅在三例全部技术完成时建立十例开发结果：旧七例模型提案保持原样，仅离线执行最终事件依赖检查；另外三例采用技术补全结果。发布回执说明这两类来源，不把来源覆盖增加或结构变化写成准确性提升。该开发结果不是医生盲评许可包。

CE-EXP-003 修复要求发布 summary 的状态分布及全部可派生计数均由最终事件/分组对象重算。历史 v1 已冻结，`repair_summary_release` 新建 v2，仅链接字节不变的数据文件并写新 summary，逐例核验分布和数量；不覆盖 v1，不调用模型。

在项目根目录使用：

```sh
/opt/anaconda3/bin/python3 -m scripts.cohort_construction.p2_auto_checkpoint_v06.experiments.fixed_model prepare
/opt/anaconda3/bin/python3 -m scripts.cohort_construction.p2_auto_checkpoint_v06.experiments.fixed_model probe
# 随后按实验冻结配置调用 p2_auto_checkpoint_v06.run，写入新输出目录。
/opt/anaconda3/bin/python3 -m scripts.cohort_construction.p2_auto_checkpoint_v06.experiments.summarize summarize
/opt/anaconda3/bin/python3 -m scripts.cohort_construction.p2_auto_checkpoint_v06.experiments.fixed_model feedback
/opt/anaconda3/bin/python3 -m scripts.cohort_construction.p2_auto_checkpoint_v06.experiments.summarize release
/opt/anaconda3/bin/python3 -m scripts.cohort_construction.p2_auto_checkpoint_v06.experiments.summarize repair_summary_release
/opt/anaconda3/bin/python3 -m scripts.cohort_construction.p2_auto_checkpoint_v06.experiments.fixed_model verify
```

患者材料及模型原响应均写在 `private/p2_checkpoint_next_20261028/model_validation/`。合成测试位于本目录 `tests/`，不访问模型。
