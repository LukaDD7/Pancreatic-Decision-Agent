# P2 FHIR R4 回放可行性原型

此模块实现来源封装和时点回放，不实现自动临床决策识别或正式episode划分。详见 `docs/FHIR决策Episode回放-可实施框架-v0.1-20261022.md`。

- `core.py`：不依赖FHIR服务的R4资源构建、转换Provenance、通用回放函数。
- `build_n01.py`：读取既有N01上下文和交付原始检验/影像，四个**人工核对**的开发锚点。
- `tests/test_replay.py`：14项合成回归案例；不接触患者资料。
- `release.json`：研究范围、依赖、问题编号和验证状态。

FHIR固定R4 4.0.1；研究时序字段保留在旁表，不伪装成FHIR核心字段。文书原文装入DocumentReference、报告原文装入DiagnosticReport、检验行装入Observation。没有真实结果状态时使用unknown；DocumentReference的current仅表示导入的文档引用状态，不宣称原文当时已定稿。采样时间缺失不填写effective；报告时间写issued，真实可见性仍需核验。检验使用valueString保留原始限定词，单位/参考值保留原文；暂不提供数值CQL计算。

## 运行

Python 3.12 + jsonschema（已在本机环境可用）。从独立版本库根目录执行：

```bash
/opt/anaconda3/bin/python3.12 -m unittest discover -s scripts/cohort_construction/p2_fhir_replay/tests -v
/opt/anaconda3/bin/python3.12 -m unittest discover -s scripts/cohort_construction/p2_checkpoint_casebook/tests -v
/opt/anaconda3/bin/python3.12 -m scripts.cohort_construction.p2_fhir_replay.build_n01 \
  --repo /Users/lucachangretta/Agent_Workspace/pdac-cohort-version-control \
  --project '/Users/lucachangretta/Documents/ChatGPT/不可逆胰腺癌根治术决策智能体' \
  --output local_artifacts/n01_fhir_replay_NEW_RUN \
  --schema local_artifacts/fhir_replay_research_20261022/fhir.schema.json
```

Schema来源：`https://hl7.org/fhir/R4/fhir.schema.json.zip`，解压后文件路径传给`--schema`；实际SHA256记录在回执。目录必须新建，已存在则报错，旧结果不覆盖。

源资料：`local_artifacts/p2_dynamic_pathway_probe_v0.1_20261021/{five_case_review.json,review_patient_context.jsonl}`及交付资料中的患者包/原始影像入口。本例31份上下文文书不代表全48份交付文书均已转换。

输出：完整研究者FHIR Bundle、事件账本、每个锚点的reconstruction/certified状态与隔离审计、build_receipt。完整Bundle含未来记录，只能保存在研究者侧。时点输出为可审查的原文视图，尚未去标识化，也未认证为正式Agent/盲评输入；当前模块没有对外接口或模型调用。

## 稳定问题编号与必要测试

| 编号 | 失败风险 | 必要测试 |
|---|---|---|
| FR-001 | 当前选择泄漏，或为了让Agent猜答案而删除既往计划 | 当前后缀隔离+既往计划保留 |
| FR-002 | 未来、同时间或无时间检验倒放 | 过去/同刻/未来/未知四对照 |
| FR-003 | 日期补午夜或无效日期被接受 | 日精度/无效日/精确时间对照 |
| FR-004 | 补记当成当时可见 | reconstruction保留并标记，certified不自动接受 |
| FR-005 | 展示裁剪修改原文 | 不变性+附件原文往返 |
| FR-006 | 含糊切分静默成功 | 缺失/空/重复锚点失败 |
| FR-007 | 跨患者、整包结局、研究标签泄漏 | 患者过滤+输出字段白名单 |
| FR-008 | 把issued、effective、lastUpdated混为一谈 | 原始值/status和时间字段测试 |
| FR-009 | 标准化时损坏否定或无法判断 | 原文往返完整性；不宣称已有语义解析器 |
| FR-010 | 后来的版本倒流 | 版本可见时间前后对照 |
| FR-011 | 导入时间伪装临床时间 | Provenance转换时钟与事件时钟分离 |
| FR-012 | 重复源/错位节点导致重复或错误切片 | 重复资源和anchor时间不一致失败 |
| FR-013 | 时区混用 | 本地时间与UTC等价 |
| FR-014 | 未知模式退化成宽松模式 | 未知模式必须报错 |

已有P1解析错误仍由其原回归守卫覆盖；本模块不替换或声称继承了一个尚未实现的语义解析器。未来实现决策抽取器须迁移相应反例并加新测试。

## 本轮差异

此前casebook仅组织一个精选N01前状态；本轮通用函数组织4个人工锚点，封装668条来源资源（1338个含溯源资源）。D03从7文书/2影像/138检验扩为10文书/4报告/138检验：增加3份入院/教育表、胸片和首次置管报告。增加的是来源覆盖，不是新增决策。

第一次构建断言仍按旧精选7文书预期，实际为10。已核对新增3项属于原始上下文的入院/教育表，修正范围断言；失败输出及原因保留在私有 `local_artifacts/fhir_replay_research_20261022/failed_build_01/`。成功运行另建冻结输出。
