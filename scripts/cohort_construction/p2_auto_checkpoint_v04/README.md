# v0.4 文书整体语义重建

研究范围和两条原则不变：先判断是否关联 PDAC 根治路径管理，再判断有无决策相关证据演化。初始可见安排单列为过程锚点，不声称它已经证明两轮演化。

程序将完整临床文书或同次检查的报告版本交给 LLM。LLM 可以返回零个、一个或多个事件，可以把术前小结的指征、风险和术式组织为一个判断，也可以识别关键词未覆盖的拔管、输血或会诊安排。文书类型是先验，不能自动确认或否决事件。

第一轮发现事件并逐字标记动作、选择前事实、执行后结果和时间依据。第二轮将同篇事件一起复核，提供多个此前事件及按时点组织的原文资料；LLM 决定 scope、是否复述、checkpoint、真正前轮和管理过程连接。两轮输出和改判理由均保存。

程序只约束来源、原文 hash/span、时间精度、允许资料范围和输出结构。无效引用逐条隔离，重复文字保留全部位置；不能因一条引用出错吞掉其余有效判断。未经来源支持的实际时间、未来事实和当前动作不能变成选择前证据。语义输出与这些约束冲突时保留原输出，并把候选降为有具体原因的不确定。

术中按“探查后、具体选择前”的阶段组织事实；开始时间不是实际选择分钟。术前安排和术中选择可属于同一管理过程，不要求具体子问题相同。只有有据的同事件判断才作身份去重。

病例原文先以 FHIR `DocumentReference` / `DiagnosticReport` / `Observation` 载入；研究事件和过程写为明确标记的 `Basic` 与 `Provenance`。检查报告不伪造为 `ServiceRequest`，研究节点也不作为临床医嘱。

运行入口：

```sh
python -m scripts.cohort_construction.p2_auto_checkpoint_v04.run \
  --sources private/p2_auto_checkpoint_v0.3_20261025/release_fixed10 \
  --manifest private/p2_pathway_checkpoint_scale10_v0.1_20261023/sampling_manifest.json \
  --output <new-run-directory> --cache <cache-directory>
```

只有来源包和固定病例清单进入上述程序。开发参考仅由独立的 `evaluate.py` / `contract_scorecard.py` 在运行后读取，不作为重建答案。参考为非穷尽 AI 原文开发挑战集，不能据此报告全病例 precision/F1 或 clinical validation。

`evidence_context.blocks` 与 `actual_model_evidence` 记录真正进入该事件复核的原文。全文以 reconstruction-only 形式另行提供，不能直接称为医生/Agent 盲评包。预算遗漏、发布时间代理和未知选择时刻均有记录；`blind_replay_ready` 保持 false。

模块职责：`context.py` 管全文单位与资料时间范围；`prompts.py` 管语义契约；`validation.py` 管逐条引用；`temporal.py` 管实际选择时间依据；`engine.py` 管两轮语义与过程组织；`client.py` 管已授权服务调用和缓存；`run.py` 管固定病例与 FHIR；`evaluate.py` 与 `contract_scorecard.py` 只管开发评价。

## v0.5（2026-10-27）

当前入口默认读取 `config/p2_auto_checkpoint_v05.json`，通过本轮版本配置显式请求 `qwen3.8-max`，不修改共享 provider 默认模型；缓存键包含完整模型名称和请求。v0.4.3 冻结源码与结果保留在旧 private 目录。

新增 `choice_boundary_quotes`（完整选择子句）与 action 节点证明分离，完整原文中前置状态和多个选择子句之间的状态可保留，当前选择子句和明确后续事实扣除。边界无效或歧义时回退保守范围并记待核，不声称盲评材料已认证。

`source_time.py` 仅为原 clock 缺失的消化内镜申请单识别医师署名日期，形成有原文跨度的记录日期代理；不把预约日期、检查时间或签署日期变成实际选择时间。

复核另有 previous-known/current-information 比较、明确再解释依据、证据到判断连接、逐条观察可见性及七维不确定性。程序只核验这些声明的来源、引用、时序和结构；语义正确性仍需独立评估。`evaluate_layers.py` 与原评价并存，新旧运行按同一新定义重算，原冻结数值和参考不改。

## 2026-10-28：显式支持型文书入口与一次同源发现反馈

以下能力由新配置显式启用，旧配置缺省保持关闭，不改写冻结的v0.5请求或输出：

```json
{
  "include_support_documents": true,
  "discovery_feedback_max_rounds": 1
}
```

`include_support_documents`只接受布尔值。启用时，程序把每份非空支持型文书的完整原文组织为独立发现处理单位，不要求肿瘤、手术或动作关键词命中。该单位的`type_prior`明确说明它是文书类型提示，不能确认或否决研究相关性和患者管理判断；LLM仍可返回零事件。空或仅空白文书不发起发现请求。`build_units(..., routing_audit=list)`可逐来源记录纳入、支持型文书仅保留上下文、空文书及其他来源路由，并保存对应处理单位ID。旧默认支持型文书提示词未修改。

`discovery_feedback.py`提供：

```python
final_compiled, audit = run_discovery_feedback(
    initial_compiled, initial_response, original_payload,
    compile_response, client, config,
)
```

- `initial_response`是发现阶段LLM的原始JSON提案；`initial_compiled`是程序完成引文定位、事件ID和时间核验的结果。
- `original_payload`必须是实际送出的、已按既有规则脱敏的发现请求；`compile_response(response, receipt)`由调用者绑定原处理单位、原来源和姓名映射，采用同一编译规则。
- `discovery_feedback_max_rounds`严格只接受整数0或1，布尔值、字符串或更大次数均拒绝。0为关闭；1也只有满足触发条件才增加一次发现请求。
- 触发条件为：该单位零事件、原始提案`local_id`重复、动作证明引文机械核验失败、目标临床决策子句引文机械失败或出现定位歧义。新的请求保持原始处理单位、完整原文、来源ID和历史上下文，仅增加原始提案和具体报错说明。
- 零事件重查后仍可返回零。目标临床决策子句引文为空、而LLM明确保留分割不确定性时，不强迫LLM补出子句；空引文不等于盲评材料已通过审核。其他患者信息/时间引文仍接受原有核验，本模块不把它们增加为新的发现准入门槛。
- 修订提案含重复`local_id`、引用未提供正文的来源、把历史文书动作冒充当前单位事件、当前原文输入被改动、重新编译仍有动作/临床决策子句机械错误，或服务/Schema/编译异常时，程序保留初始编译结果并记录失败；不因失败增加候选Checkpoint或制造阳性。
- 服务返回合法修订后，程序重新生成全部发现事件ID、动作/临床决策子句坐标和时间依据。`engine.discover_unit`已接入该函数，之后才构造逐事件候选原文资料范围和复核请求；不会把旧事件ID或旧允许引用区间接到修订对象上。

`discovered[].discovery_feedback_audit`保存原始与修订JSON、触发条件、初始请求hash、原始资料payload hash、反馈payload hash和实际反馈请求hash、最终采用哪份提案以及失败类别。payload hash与模型服务请求hash含义不同，不能混称。异常路径不读取共享客户端最后一条回执，也不把服务异常原文写入审计。语义正确性始终标记为`NOT_INDEPENDENTLY_VALIDATED`。

稳定问题ID及必需正负例见`tests/discovery_feedback_issue_map_v06.json`：CE-AUD-003对应支持型文书发现入口；CE-AUD-007对应一次同源发现反馈。测试全部使用虚构原文和假客户端；无临床调用，不证明重查后临床召回率提高。原始发现阶段LLM遗漏而又返回了其他合法事件的情况，仍需要独立语义核查；本模块不是完整漏发现检测器。
