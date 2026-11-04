# P2 自动候选与证据演化 v0.3 开发修订

默认配置为`config/p2_auto_checkpoint_v03.json`。v0.3增加具体文书/段落用途、同文手术安排的临时输入分组、更早安排原文、早期临床原文基线及重复文字核对。全部原句候选保留；只有明确表单/通用未来套话及已核对逐字术后复述改作旁证。术前小结、诊断依据中的安排不能按标题/段落直接排除。

两条研究原则不变，分组不确认same_event；相同动作有新证据仍可形成新轮，文字相同不证明同一次测量。约定见`docs/P2-文书模式与证据演化优化-contract-v0.3-20261025.md`，快照、测试、实际调用和开发版本diff在`private/p2_auto_checkpoint_v0.3_20261025/`。完成状态以该目录`CURRENT_RUN.json`为准。

以下为v0.2及v0.1.1历史记录。

默认配置现为 `config/p2_auto_checkpoint_v02.json`。诊断报告同时产生报告证据和“此前选择做检查”的间接候选，时间未知则只保存检查时间上界。历史检查不再自动跳过证据演化；同文历史顺序可提供待核的前后轮比较。scope包括未承诺手术、补充证据和最终不推进手术。role只解释记录如何支持候选，不是排除历史检查的门槛。

新版实际定向试跑和完整请求：`private/p2_auto_checkpoint_v0.2_20261024/targeted_actual_calls_v02/`。同固定10例已重跑规则提取；没有把定向模型调用称作10例完整语义重跑。完整提示词、两次事件对象和输入原文导出在该版本目录。未生成医生/Agent盲评包。

v0.1.1的冻结源码仍在 `private/p2_auto_checkpoint_v0.1_20261024/code_snapshot_v0.1.1/`，以下是历史说明，不代表v0.2结果。

当前开发试跑：`private/p2_auto_checkpoint_v0.1_20261024/release_v0.1.1/report.md`；机器可读约定在同目录`algorithm_contract.json`。44项针对性测试通过，同固定10例已自动运行。v0.1源码与结果另存，当前修订补足“限期手术”“根治术”等通用表达，不使用病例专用CP清单。

原始文书/影像/检验 → FHIR来源资源与原文索引 → 文书类型×动作/主题词提出候选 → 真实LLM逐项归类 → 截至该点的累计原文及两轮间变化 → 真实LLM判断证据演化 → 候选CP和管理过程。

不读取旧CP、graph、模型proposal或参考答案来生成新候选。病例成员可来自固定抽样清单。没有病例专用source ID、CP编号或人工锚点。输出不是gold，不生成临床正确性答案。

算法约定：[contract](../../../docs/P2-自动候选与证据演化-algorithm-contract-v0.1-20261024.md)。配置：`config/p2_auto_checkpoint_v01.json`。

## 运行

```bash
python -m unittest discover -s scripts/cohort_construction/p2_auto_checkpoint/tests -v
python -m scripts.cohort_construction.p2_auto_checkpoint.run \
  --manifest private/p2_pathway_checkpoint_scale10_v0.1_20261023/sampling_manifest.json \
  --output private/new_auto_checkpoint_run \
  --schema private/p2_auto_checkpoint_v0.1_20261024/standards/fhir.schema.json \
  --workers 3
```

加`--stage extract`只运行规则候选，不调用模型。`--cases`可缩小固定清单的执行范围。主程序可接受同样字段的其他病例清单，不依赖上述10例的旧结果。

也可将`--manifest ...`替换为`--fhir-bundle 单患者来源Bundle.json --case-id FHIR-CASE`。输入中的研究Basic不会成为新一轮算法依据；不支持的临床资源类型明确报错。源资料生成FHIR并读回后才提取候选，原文/hash/时钟必须一致。

模型沿用`model_api.load_boyu_settings()`和`config/model_provider.json`。每个候选单独调用，避免较晚材料出现在较早候选的归类请求里；并发请求数受全局6槽限制。第三方接口采用JSON mode + 本地JSON Schema及逐字引用验证，不能宣称strict schema由服务端保证。

## 文件

- `extract.py`：原始来源映射、正文时间、报告分组、规则候选与截至当前阶段的来源窗口。
- `semantic.py`：身份字段遮蔽、真实调用、内容寻址缓存、输出与原文校验。
- `run.py`：逐患者自动执行、候选状态和episode组织；失败不改成阴性。
- `fhir_io.py`：FHIR R4来源资源与Basic研究注释；官方JSON Schema和本地引用检查。
- `regression_catalog.json`：已知失败反例与必需测试。

## 适用边界

FHIR资源是标准数据载体，转换不自动产生研究DecisionEvent。报告本身不是检查选择，研究episode不等于行政EpisodeOfCare。本版对研究注释使用Basic扩展，不伪装临床医嘱。

历史检查可只得到“相关但缺选择前资料”的候选。影像仅有检查时间时使用显式代理，不认证发布时间。术中判断用叙事阶段，手术期间新报告若无法排序则待核。没有自动产出可交给医生/Agent的正式盲评包。

候选上下文是局部原文，窗口有预算上限；多并行管理任务的前一轮比较、未知文书类型、不同写法的历史复述仍需测量。原始临床资料只按白名单读取；全部输出及调用缓存位于private。API输入屏蔽已识别身份字段，但不把此称完整匿名化认证。
