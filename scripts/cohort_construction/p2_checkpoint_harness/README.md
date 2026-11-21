# p2_checkpoint_harness

一个最小的 Agent Harness，用来替换 `p2_auto_checkpoint_v04` 的固定请求形态。
**不改动、不读取、不覆盖任何冻结批次及其缓存。** 新目录、新缓存、新会话日志。

## 当前发现合约：带原文边界的候选表述（2026-11-12）

以下 r1–r9 运行记录、计数和当时提示词缺陷是历史审查材料，不代表当前发现接口。
当前 discovery 不要求 Agent 判断或分配临床单位、拆合事件、组织证据链、填写选择时刻，
也不要求它决定最终 episode。`unit_id` 只是程序分配的阅读任务，`source_id` 是实际原文
定位锚点；下游保留的 `event_id` 是程序候选 ID，不是已确认独立临床事件或决策点。

发现阶段 LLM 对一份文书中的多个研究相关候选管理判断逐项调用 `propose_candidate`：

```json
{
  "local_id": "本阅读任务内的提案修订键",
  "scope": "yes | no | uncertain",
  "record_function": "new_decision | reassessment | repeat | execution | generic | uncertain",
  "source_id": "实际来源",
  "quote": "同一来源中逐字、连续、非空的原文",
  "start": 0,
  "end": 20,
  "rationale": "可选说明",
  "reason": "scope=no 时必填的简短排除理由",
  "uncertainty": "scope=uncertain 时必填的不确定性"
}
```

`start/end` 是 Python 字符偏移的半开区间 `[start,end)`。唯一出现的 quote 可由程序补全；
重复 quote 必须显式给 start，且 end 必须等于 `start + len(quote)`。不拼接不同句子或不同
文书。`ACCEPTED` 只表示候选片段可精确定位，不表示临床语义正确或专家接受。

- `scope=yes` 是发现阶段 LLM 对根治性切除路径相关管理判断的范围判断，与实际手术、
  最终可切除或结局无关；`scope=uncertain` 保留进入后续组织并记录不确定性。
- 对已识别为管理相关但明确排除的表述，使用 `scope=no + reason + span`。它保留在
  `out_of_scope_candidates`，不进入 ordering/episodes 的 active 候选；不是患者排除，
  也不是未运行或无法判断。没有独立的 `reject_mention`，不要求逐句提交所有无关句子。
- `record_function` 仍是发现阶段 LLM 的文书表述功能分类，不是已确认临床事实。
- 不在 discovery 收集 `management_question`、动作改写、`prechoice_quotes`、
  `postchoice_quotes`、`choice_time` 或证据关系。后续证据组织和可见时点另行处理。
- 同一份文书的组合表述与具体组成动作都先保留为候选，不在发现阶段强行拆合；后续
  `same_event` 只能表示严格身份候选，不能冒充包含、子动作或重叠关系。

程序对 `performed_test_report` 阅读任务建立 `evidence_inventory`：保存来源 ID、hash、
字符数、检索时钟与来源属性。它不让 LLM 为检查报告伪造管理问题、部署句或检查选择原因；
`evidence_selection_record_status=not_assessed_in_discovery` 明确其尚未做证据选择评价。
证据对象单列 `evidence_object_ids`，不形成伪 singleton episode，不能作为
`same_event/same_management_process` 端点；`evidence_comparison_previous` 只组织有原文
支持的既往证据—后续判断关系，不合并管理过程。

`tools/masking.py` 是后续呈现材料的程序投影消费方：显式选择候选 ID，重新核验实际原文与
精确区间，只替换这些区间；重叠区间、未定位候选、重复 ID 或未知 ID 明确失败，不静默挑选。
原 source 不变。若 source 提供 `source_sha256`，程序按 UTF-8 正文计算并核对该 hash；
hash 缺失时不声称已验证内容版本。`protocol_status=program_projection_not_frozen_blinding_protocol`：只 mask 措施名或
决策句不等于防止泄漏；并存决策、解释、结果和后文的范围及未来 Checkpoint 可见界限仍须按
最终盲评方案确定。本实现没有完成临床盲评适用性或真实防泄漏验证。

## 病例级组织：单次 Agent，而不是两轮全局组织（2026-11-13）

新的 `--stages case` 运行路径为 **discovery → case-level organization**。旧
`--stages legacy` 保留 `discovery → ordering → episodes`，供同一发现输出及旧反例比较；
旧入口不是新组织阶段的组成部分。`--stages discovery` 只运行发现，不创建 ordering、
episodes 或 case-organization 会话。三种 scope 分别绑定到运行契约及授权，不能用 r9 授权
替代本次代码和输入的授权。

`--stages trajectory` 运行 **discovery → 全病例证据演化裁决**。Discovery 仍宽召回；离线程序先形成
临时 CP 投影，再由病例级 Agent 逐项审核管理 CP。除首见锚点外，独立管理 CP 必须与最近保留状态
比较，并提供严格晚于该状态、不晚于当前 CP 的新增患者证据；新增即构成证据演化，不要求结果异常、
阳性或实际改变管理，常规检验、术前监测及正常/阴性结果也不例外。动作更具体、转科、复述和执行本身
不构成新证据。程序确认区间内存在新原始报告时拒绝 `SUPPORT_ONLY`，但比较诊断 CP 的目标报告及同 hash 副本不计入新区间报告。`SUPPORT_ONLY` 原文及遮蔽区间并入目标 CP，不删除记录。程序核验最近比较状态、
引文边界、时间方向和完整覆盖；同刻或并列最近状态必须待核，其他管理 CP 的动作 span 不能冒充
患者证据。未完成、部分裁决或没有裁决字段的旧结果不能按默认 trajectory 渲染为新版预览。程序
仍不能证明每段引文在语义上必然是患者证据，须保留研究审核。该阶段只产生研究候选，不是临床
Reference；渲染仍使用新目录，旧结果不改写。`HCP-LIVE-029` 登记本轮对抗审计发现的旧证据、
远跳比较、同刻首见、跨文书动作及 Store/投影器合同反例。

`--stages adjudication --discovery-result <既有case_result.json>` 只重新运行全病例裁决，contract 和
authorization 同时绑定 Discovery 结果 hash，不重跑发现。`HCP-LIVE-030` 登记分钟值已规范化为秒但
`raw` 在 Discovery 接缝丢失、导致全部时钟不可比较的反例；新运行保留 `raw`，投影器只在值和精度与
来源完全一致时恢复该来源证据。`HCP-LIVE-031` 登记裁决理由/引文绕过 MASK 的反例：默认页只显示
裁决结构和遮蔽后仍可见的引文，自由文本理由及完整引文仅在研究原文页展示。`HCP-LIVE-032` 登记
无 comparator 的待审核 CP 丢失及链式归并落点不一致反例；无法归并的待审核 CP 保留，迁移记录指向
最终承接 CP，原 comparator 仍保留在裁决对象中。

`stages/case_organization.py` 同时把管理候选与程序 evidence artifact 送入一次病例级 Agent：
- **Agent 只做关系记录**：为候选/证据节点连边，写关系类型、置信度、理由和必要的原文引文。
  `evidence_used` 为既往证据对象→后续管理候选；时间可比时拒绝反向，未知或同刻不推定临床选择时刻。
- `same_event/contains/overlap/same_management_process` 分别保留身份、组成和过程关系；
  `none/no_link` 只修正直接过程边。证据和组成关系不自动合并过程。
- 新路径只有 supported `same_management_process` 被程序用于合并；`context_only/uncertain`
  保留、不合并。legacy 的历史合并语义不变。
- **容器和孤立节点由程序派生**，不向 Agent 提供 preview、demote、ID 覆盖清单或单独
  evidence-check 任务。先验证核心连边能力，再考虑额外容器审阅职责。

`--stages organization --discovery-result <冻结case_result.json>` 直接读取已有 discovery，
只运行关系记录，不重跑发现；contract/authorization 同时绑定该结果 hash。`--stages case`
仍适用于从发现开始的新运行。容器文件名为 `candidate_containers.json`，不是医生材料。

`HCO-DEV-017` 修复 scope 授权固定成 legacy 值造成的前置拒绝；`HOR-DEV-018` 登记
单次组织的关系对象模型及合并约束。这些都是代码契约与合成反例，不是临床质量证明。
真实发现召回、模型组织语义与病例级容器边界仍须基于原始文字单独审查；Checkpoint 材料组织、
最终盲评方案和医生资料包不在本轮实现范围。

## 候选材料的可读投影（2026-11-14）

`stages/materials.py` 现在输出 `p2-candidate-checkpoint-material-0.2.0`：
把**发现阶段 LLM 提议、程序核验的候选原文区间**作为研究审核层单列，同时保留
**程序按时点纳入并遮蔽的来源投影**。两层不能混称：候选定位原文不是已确认临床决策，
按时点可见的历史来源也不是 Agent 已使用的证据。`target.source_title/source_clocks` 明示
当前候选所在文书和检索时钟；检索时钟仍不等于临床团队作出选择的时刻。

每个候选保留 `related_candidates`，其中含关系阶段 Agent 的关系类型、置信度、理由、
两端引文和相关候选定位原文；`episode_navigation` 仅提供 Episode 内上一个/下一个候选的
显示顺序。未知时钟排在后，绝不把显示顺序或关系方向冒充已确认临床先后。

离线 HTML 一次只显示一个候选，默认“本节点增量”按原文上下文、Agent 候选定位与理由、
拟遮蔽前后对照、前后相邻候选直接关系四步呈现；未记录直接边不等于临床上无关系。
“定位 Agent 选定原文”可展开精确字符边界；另有目标文书完整遮蔽投影、时点内完整资料、
全部 Agent 关系、已记录证据引用四层。顶部下拉框、上一/下一候选按钮及关系卡均可切换候选。
草稿切换不互相覆盖，导出绑定 checkpoint、packet hash、manifest hash、表单与 renderer 版本。

```sh
python -m scripts.cohort_construction.p2_checkpoint_harness.render_materials \
  --case-result <run>/case_result.json \
  --sources <case>/source_fhir_r4_bundle.json --output <新的私有目录>
```

只读取既有运行结果，不调用 provider、不请求网络、不改写冻结输入或旧版报告。
**这份 HTML 含全例候选和研究审核专用原文，是私有研究预览，不是医生盲评包。**
最终医生包须按冻结方案另行派生并限制每个专家可见的候选和原文，不得用 CSS 隐藏全例内容。
真实 SCALE-01 v3 由同一离线程序生成 38 个候选、17 个 Episode；v2 保持不改。
渲染层的改进不验证候选的临床语义、Episode 独立性、证据实际使用或最终盲评完整性。
对应问题登记为 `HCM-LIVE-025`，合成反例 `HCM004–HCM008` 及真实报告浏览器回执覆盖导航、
原文/遮蔽分层、来源筛选、草稿绑定及手机无横向溢出；它们不是临床质量认证。

## 为什么要有它

0.8.0 批次实测暴露的是机制问题，不是参数问题：

| 观测 | 数值 | 根因 |
|---|---:|---|
| 发现阶段用户消息最大 | 186,482 字符 | 配置上限 80,000，但只统计正文 |
| 目录元数据占用户字符 | 57.1% | 一次性发送全部来源目录 |
| 被 budget 统计的正文占 | 10.0% | 同上 |
| 复核池脚手架占池字节 | 83.6% | 每块重复 20 个字段，块原文中位 146 字符 |
| 跨请求块重复发送 | 94.0% | 每单位各自携带池副本 |
| 零事件重查 | 225 次，新增事件 0，5.96M prompt token | 同输入重发 |
| `choice_time` 非空的事件 | 0 / 463 | 语料里没有“决定时间：”写法 |
| 因 cutoff 空而无法建立前轮的事件 | 113 | `BEFORE` 对 null 恒不成立 |
| 被阻断的图依赖 | 146，其中 106 属上一条 | 同上 |
| review 覆盖 discovery 的 `record_function` | 40 个事件 | 两阶段重复判断 |

这些都不是“把上限调大”能解决的。上限本身测错了东西。

## 它是什么

以下部件取自 DeepSeek Harness（MIT）与 `anthropics/single-agent-loop-kit`（Apache-2.0）的契约，
只保留对本任务有实际作用的部分：

```
session.py      append-only 日志；模型历史由日志投影，压缩是表面替换
compaction.py   阈值触发、保留尾部、括号事务、工具配对边界
tools/          注册表 + 读工具（目录/检索/读取）+ 写工具（提案，带门）
skills/         SKILL.md，一行描述进提示，全文按需加载
spill.py        超大工具结果换成预览 + 定位符，不静默截断
agent.py        turn → step → tool calls → 下一 step；预算硬停
transport.py    缓存、回执、usage；4xx 不重试，长度拒绝单独标记；
                每个请求最多 3 次 HTTP 传输尝试，与失败单位的独立恢复是两层机制
pipeline.py     三段串联：发现 → 组织 → episode，任何一段都不静默丢内容
run_case.py     单病例入口：计划 / 契约 / 授权 / 实跑
eval.py         新旧两套产出的可核对计数比较（不是临床准确性比较）
```

明确**没有**引入：Cordis 运行时、子 Agent、Agent 团队、PTY/终端、浏览器、
插件市场、后台作业、计划模式。这些对本任务都是净增失败面。

## 为什么不用 Cordis / dsh 运行时

不是因为不能用，而是因为**引入它们要付的代价大于省下的工**：

1. dsh 是 TypeScript + Cordis 插件树。我们的管线、回执链、脱敏、FHIR 投影、
   三个 hash 域全在 Python 里。跨语言调用意味着每个工具都要过一次进程边界，
   而回执和引文核验正是必须留在同一个进程里的部分。
2. dsh 是 developer preview，明示会有破坏性变更。作为参考实现读它、抄它的
   **契约**（压缩事务、surface replace、tool pairing、spill locator），
   比把我们的审计要求迁就它的迭代节奏更稳。
3. 真正需要的四件事——append-only 日志、压缩、spill、按需技能——都不大，
   这里合计约 1,500 行，可以逐行读懂、逐行测试。

要抄的是**契约**，不是包。若将来需要它的 `web` UI 或 `acp` 协议，可以单独接。

## 从 dsh 搬过来的具体契约

| dsh 部件 | 本包落点 | 照抄的常量/结构 |
|---|---|---|
| `compaction` 阈值 | `compaction.py::CompactionPolicy.trigger` | `floor(min(W×0.8, W−O−65536))` |
| `compaction` 保留 | `retain_tokens` | `0.16 × (W − O)` 逐字保留 |
| `compaction` 事务 | `compact()` | start → summary → `surfaceOp.replace` → end |
| `compaction` 配对 | `balance_edges()` | 两端外扩，绝不相交 tool call/result |
| `spill` | `spill.py` | 预览 + `locator:`，读回失败时保留原文 |
| `skill` / `tool-skill` | `skills.py` + `agent.py` | 一行目录进 system，正文按需加载 |
| `agent-loop` | `agent.py` | turn/step 模型；预算硬停是 stop reason |
| “model-visible means logged” | `session.py` | 每次请求的字节都能从日志复原 |

未照抄：Cordis 的插件生命周期、`web`、`acp`、子 Agent。它们服务的是 IDE 产品，
不是单病例重建。

## 与九步改造的对应

| 原步骤 | 本包 |
|---|---|
| 1 分组 | 未改，直接复用 `build_units` |
| 2 边标签 | 未移植；无读取方的标签不再出现 |
| 3 发现 | `stages/discovery.py` + `skills/discovery`，工具按需检索 |
| 4 编译 | 历史职责：旧 `tools/proposals.py` 的引文与时间依据核验；当前只核验候选精确 span，不在发现阶段收集选择时刻 |
| 5 复核 | `stages/ordering.py`：组织候选表述的前后与过程，降级须引用推翻原文 |
| 6 状态 | 不再有第二套分类；状态由提案 + 核验结果直接确定 |
| 7 许可 | `compare_clocks` 返回 `unknown`，不再静默拒绝 |
| 8 图审计 | `stages/episodes.py::audit`，报告覆盖缺口，不自动作临床判断 |
| 9 episode | `stages/episodes.py` + `pipeline.py::run_episodes`：独立 Agent session 审查病例级容器 |

## “尽力重建、不制造烂摊子”是怎么落实的

原则落到三个具体约束上，每个都有测试：

1. **无候选是记录，不是重发。** 单位没有候选时写入 `zero_event_units`；明确排除的表述
   保存为 `scope=no` 的 `out_of_scope_candidates`，不把未运行、未识别与排除混为一谈。
   （`HPL001`、`HPL034`）
2. **引文不合格不等于事件不存在。** `PROPOSED_WITH_PROBLEMS` 的事件带着问题
   清单进入下一阶段，不被删除。（`HPL002`）
3. **降级是标注，不是抹除。** `demote_event` 必须给出推翻原文；事件保留，
   `demotion.discovery_status` 记下它原来的判定。（`HPL003`）
4. **没有依据的关系不会消失。** `uncertain` / `no_link` 进 `unresolved_relations`，
   孤立事件逐条报告。（`HPL004`、`HPL005`）
5. **没跑的单位如实列出。** `coverage.units_not_run` 明确“未运行”不等于“无事件”。
   （`HPL006`）
6. **阶段停在哪一步要说出来。** `stage_stops` 保留每个阶段的 stop reason，
   预算耗尽与格式失败不会被当成正常完成；提供 `finish` 后只有文字、未声明结束的
   单位记为 `not_finished`。（`HPL007`、`HPL010`）
7. **网络失败不是全例结束。** 单个单位的 provider 传输失败保留已提交提案，记
   `request_failed`，继续处理其他单位；它仍使整例为 `INCOMPLETE`，不是零事件单位。
   本地程序异常不沿这条路径吞掉。（`HPL011`）
8. **失败单位要有界恢复，不只是绕开。** r6 后，discovery 的传输失败会在新 session
   中重试；ordering 也有相同的有界传输恢复。默认最多 2 次恢复，配置为
   `max_transport_recovery_attempts`。所有尝试仍共享病例总预算，预算不足不会另开恢复。
   discovery 保留旧提案，相同 `local_id` 记为 revision，不重复生成事件。
   `transport_recoveries` 是恢复尝试数，`transport_recovered_units` 是成功恢复的单位数；
   `recovery.exhausted` 表示因尝试上限或病例预算而无法继续恢复。`HPL-LIVE-008`、
   `HPL012`–`HPL014`；它们不是 r6 contract 的一部分。
9. **实际引文决定出处，阅读任务不决定临床归属。** `unit_id` 只用于分配发现任务和
   覆盖审计；候选按其唯一 span 的实际文书得到 `origin_source_ids` 和检索时钟。
   候选只提交一份实际来源的连续原文，不再借用当前阅读任务 clock；跨文书关系在后续阶段
   单独组织。没有实际出处时明确记录 `attribution_problems`，不回退借用当前单位。
   `discovery_unit_id/discovery_source_ids` 只保留提案产生路径。候选列表区分实际出处、
   精确 span 和发现任务。（`HPL018`、`HPL018a`–`HPL020`）
10. **检查证据与候选管理判断分开。** 程序建立 `evidence_inventory`，不让发现阶段 LLM
   伪造检查选择原因或管理判断。证据对象单列 `evidence_object_ids`，不组装成 singleton
   episode，也不能充当 `same_event`/`same_management_process` 端点。
   `evidence_comparison_previous` 只记录有原文支持的证据—后续判断关系，不合并过程。
   （`HPL023`、`HPL025`、`HPL026`）
11. **局部边须先经过病例级容器预览。** episodes 现在有独立 session 和与 ordering 相同
   层次的有界传输恢复；正常 `finish` 前必须实际调用 `preview_episodes`，并声明覆盖全部
   active 对象和相应证据检查。新增关系进入最终图，恢复保留此前关系。（`HPL021`、
   `HPL024`、`HPL026`）`none/no_link` 只否定精确事件对的直接过程边，保留历史及其他
   路径和证据引用，不删除事件。（`HPL022`）

这些门是程序结构约束，不是临床语义裁判：preview 已执行、ID 已列全或引文可定位，都不
证明模型真正读完了必要原文、正确划分了容器，或获得了专家接受。当前“完成”仍须区分
流程 `answered/assembled` 与实际语义覆盖。

### r9 原始过程审查与本轮实现状态

r9 的 frozen 文件保持不改。它的原始 summary 因恢复历史状态门而写成 `INCOMPLETE`；最终
实际记录为 37/37 单位 answered、25 个候选事件、16 条 ordering 关系、13 个候选 episode
（10 个 singleton）、226 个 receipts、2,572,590 total tokens。ordering 为 answered，旧
episodes 直接组装；这仍不是临床重建成功。

从原始 session 可见的工具参数、理由、解释与 finish.note 审查到以下原因链。这里读取的
是模型显式提交的判断痕迹，**不是隐藏思维链**：

| 原始过程观察 | Agent 可见判断/程序机制 | 最终问题 |
|---|---|---|
| 停鼻空肠管与补白蛋白的解释同时列出“营养支持与管路管理”，却以“同一次术后查房管理”连接；拔腹腔引流管与加奥美拉唑仅以“同一句查房意见”连接 | ordering skill 已有“同一份术后记录里的拔管和输血不是同一过程”反例；程序当时只核验引文位置和端点，不能核验管理目标 | 不同管理目标被标为 supported `same_management_process`；不是单纯缺少提示 |
| 16 条关系记录后未调用 `preview_episodes`；旧 pipeline 没有创建 episodes Agent session，虽然 `EpisodeBuilder.tools()` 已有预览工具 | 局部关系未经病例级容器检查就由 `EpisodeBuilder` 传递并查 | EP001 从术前检查/手术意向延伸到术中及术后支持，共 9 个事件；局部可解释不等于容器边界适当 |
| 入院单位 `DU5aa9774b58725b58c191` 只含入院记录，但其发现 session 读取了相邻首次病程；`EV-ef6d94f730e14564` 的 action/time 引文都来自相邻单位，finish.note 也明确承认 | 全病例 quote 查找接受跨单位 action/time；clock 沿用入院单位；`pipeline._event()` 未生成列表使用的 `source_ids`，25 个事件列表均为空 | 跨单位动作与时间来源错配以 `ACCEPTED` 通过，且来源在列表视图中被隐藏 |
| 组合入院提案同时含术前检查和手术意向，却分别与两个原子提案标为 supported `same_event` | `same_event` 被用于重复候选身份，也混入“组合提案包含子动作” | 身份组混淆 strict identity、contains/subaction 和 overlap；本轮**尚未**实现后两者的对象模型 |
| 12 月 29 日 CT 的发现 finish.note 以“未见谁、何时、为何安排”而排除；12 月 30 日处置的 prechoice_quotes 和 ordering explanation 又明确引用该 CT | 缺少检查选择记录被误当成证据对象不保留；ordering 未检索/read 该报告，且当时没有证据对象接口；`evidence_comparison_previous=0` | 已知触发证据未组织进证据链，反而把另一次 CT 与处置连成 uncertain 同过程 |
| 其他影像所见以 `historical_unknown` 管理事件保留，术前增强 CT 的“包绕肠系膜上动脉”在 read_event 结果中已可见 | 未读取完整影像和术前讨论单位；read_unit=7、search_events=0、preview_episodes=0 | 同一套发现流程对象口径不一致，关键解剖证据孤立；证据可见不等于已正确组织 |
| 出院带药/复诊以“常规模板”排除，其他单位的术后白蛋白、肝素、奥美拉唑、伤口换药却保留；不同查房内动作有时拆开、有时合成 | 独立单位发现缺少病例级一致的粒度/范围定义；执行记录也可被赋予未记载的选择问题 | 组合/原子粒度不一致、执行与决策混用；`scope=no` 对象仍可保持 active，尚无系统性修复 |

本轮已把发现接口收敛为精确定位候选 span，统一 `scope=no` 排除索引，并把程序检查证据
索引与管理 episode 容器分开；ordering 消费实际 span，`same_event` 不再冒充组合/子动作。
unit 顶层 `clock_basis` 的关联传播由 `HCT-LIVE-015` 覆盖。稳定问题登记包括
`HPL-LIVE-011`、`HOR-LIVE-012`、`HEV-LIVE-013`、`HPL-LIVE-014`、`HCT-LIVE-015`、
`HAG-LIVE-016`。其状态是 `program_contract_implemented_semantics_not_live_validated`：
代码合约和合成反例已实现/通过，真实模型的范围一致性、执行/回述语义及组合/原子候选组织
仍待验证；不通过特定药物、管路或拔管硬编码掩盖通用机制问题。

### span-discovery-v1 最终离线回执（2026-11-12）

- 合成 discovery-only 断点：同一文书提交 2 个候选并精确定位，分别 mask 后另一候选
  原文仍保留；ordering/episodes 未运行，`provider_called=false`。回执见
  `private/p2_harness_discovery_breakpoint_20261111/span-discovery-v4/breakpoint_result.json`。
  早期 v1/v2 断点因候选 ID 排序断言和私有路径拼接失败，v3 通过了旧遮蔽入口；保留各目录。
  v4 使用真实合成正文 hash 验证最终遮蔽入口，不能解释为模型或临床失败。
- 最终 Harness：**140 tests passed**，包含恢复 HPL022 独立命名后的两项独立反例，以及
  内容 hash、偏移与候选 ID 集合的遮蔽反例；
  v04 context：**26 tests passed**；P1 guard：**55 active + 51 legacy = 106 passed**。
  测试回执与 registry 校验见同目录 `span-discovery-v1-final/`。这些是合成反例、
  stub 流程与既有覆盖迁移，不是临床准确性验证。本轮没有运行真实模型，没有改写 r9，也没有证明真实发现召回、候选语义、
  新 episodes Agent 的临床容器质量或最终盲评防泄漏效果。任何真实重跑都需要新 contract、
  授权、输出目录和缓存；r9 的授权不覆盖这些代码变更。

### 真实运行记录（SCALE-01，均保留原目录）

r1–r5 是**工程调试运行**，不是临床质量验证。每次用新输出和新缓存；r1–r4 的输入
与 r5/r6 的关系见各 contract，不能把中途失败的计数当成重建结果。

| 运行 | 观测 | 处理与证据 |
|---|---|---|
| r1 | 新缓存第一次响应因 `invocations/` 不存在而未保存 | 先建父目录；`HTR001`。r1 缓存不完整，不能作为比较基线 |
| r2 | 内部工具 schema 直接上 wire，provider HTTP 400 | transport 转 chat-completions function schema；`HTR003`、`HTR004` |
| r3 | transport 对不可解析 JSON 自发重复请求，绕过 agent repair | transport 原样交付内容；agent 最多一次格式修复；`HTR005`、`HTR006` |
| r4 | 43 条回复仅 3 条标准 `tool_calls`；35 个单位未执行工具却被记 answered，事件 0 | 明示回复协议；仅归一化无歧义调用；不可读回复一次修复；`not_finished` 不冒充完成。`HAG018`–`HAG021`、`HPL010` |
| r5 | 协议生效：34 条成功回复中 31 条标准 `tool_calls`、1 条 `final`、2 条原文经修复；8 个单位调用 finish、24 条排除记录 | 第 10 个单位三次网络超时后整例中止：35 次请求、207,642 tokens、9/37 单位有记录、28 未运行、事件 0；因此仍不能作新旧产出比较 |
| r6 | 单位级超时隔离生效：37/37 单位均有记录；19 answered、18 request_failed，未运行单位为 0。保留 14 个事件提案、38 条排除记录 | **INCOMPLETE**：18 个单位发生 SSL/ConnectionError 传输失败；ordering 一次输出包含 162 个完整 compare_clocks 调用及未完成尾部，超过批次上限 16，format_failed；0 关系、14 个单事件 episode。143 次请求、1,325,044 tokens。不是完整重建或效果证据 |

| r7 | **首个流程完成的全例真实重建**：37/37 answered，24 个候选事件、14 条关系、14 个候选 episode，228 次请求、2,626,292 tokens；无传输失败、0 次恢复 | 12 个单事件容器、1 条 uncertain 拔管关系、6 个无任何关系的事件。24/24 无原文明示的选择时刻。`ACCEPTED` 仅表示引文/时间依据程序核验，不是临床 Reference；发现重复入院计划候选被标为 same_event，r7 时尚未保留独立重复组 |

r7 的同口径流程计数比较 `SCALE-01-r7-vs-frozen-r1.eval.json` 为 `comparable=true`：
prompt tokens 3,110,854 → 2,586,899（−523,955），但请求数 74 → 228，事件 37 → 24、
关系 23 → 14、episode 11 → 14。这是计数差异，不是准确率、减量效果或临床改善证明；
旧版事件计数还含 repeat/execution，不能直接当成分期决策 Reference。

`HOR-LIVE-009` 登记 r7 暴露的关系证据缺口：关系与降级引文此前只保存未核验，ordering 的
`read_event` 没有提供 `time_verdict`；`same_event` 也没有独立的重复候选分组。r7 后新增
`read_unit` 供核对同一处理单位原文，支持型同一事件/过程关系必须提供两端可定位引文，
第三份文书不能冒充端点证据。`event_groups` 保留当前过程容器内的重复候选；全局
`duplicate_candidate_groups` 另保留跨过程容器的身份连接，不删除、不改写原提案。
只有 `same_management_process` 合并候选过程容器。增量组织或网络恢复时保留既有关系，
不能把日志中仍存在的旧关系从新结果里静默丢掉（`HPL015`–`HPL017`）。
此修复不属于 r7 contract。r8 前的 Harness 为 **118 passed**，P1 guard 为 106 passed。
跨模块 scoped suite 为 **783 passed、97 subtests passed、1 failed**；唯一失败仍是外置归档
未恢复导致的旧 FHIR schema fixture 不可读，属于**环境不可访问**，不是新增程序反例。
这些合成反例与流程核验不是临床金标准。
失败单位的 stop 仍为 `request_failed`，整例不能为 COMPLETE。18 次失败各含 3 次传输重试，
本次 provider 回执的异常类型为 SSLError / ConnectionError；没有将端点、凭据或异常原文写入报告。
14 个事件中 `record_function=new_decision` 10 个、`generic` 3 个、`reassessment` 1 个，
均为模型提议的文书功能分类，不是已确认临床事实。

针对 r6，已登记 `HAG-LIVE-006`：完整但未执行的调用前缀须与未完成尾部区分，超限批次不得
直接执行或猜测未写完的调用，修复回复最多 16 个调用且仍受病例预算限制。模型修复请求使用
明确的程序回执说明，不伪造 provider 的 tool-call/result 配对。`HEV-LIVE-007` 修复评估器：
未完成运行即使文件齐全也 `comparable=false`，差值置空。r5/r6 的新复核保存在
`SCALE-01-r5-vs-frozen-r1.recheck.json`、`SCALE-01-r6-vs-frozen-r1.recheck.json`；
旧 `SCALE-01-r5-vs-frozen-r1.eval.json` 保持不改，其中历史 `comparable:true` 不可沿用。
上述修复不属于 r6 的 contract，必须在新运行中验证。

r5 的真实证明是**回复协议缺陷已修复且工具实际执行**，不是“已证明零事件”或
“临床重建成功”。r5 对冻结 r1 的表面计数（37 事件、11 episode）比较保存在
`SCALE-01-r5-vs-frozen-r1.eval.json`，但因 28 个单位未运行，**不可用于效果结论**。

2026-11-09 的外置归档一度不可访问，使一个旧 FHIR schema fixture 测试失败；
按 `AGENTS.md` §10 用已核验 UUID 的挂载脚本恢复后，五组 scoped suite 为
**800 passed、83 subtests passed**。P1 guard 为 **55 active + 51 legacy = 106
passed**。Harness 在 r5 修复后为 107 项通过；r6 暴露的批次修复与比较门槛回归后为
112 项通过，最新回执 `private/p2_harness_live_20261109/harness-r6-fix.log`。
r7 预检的 Harness 为 **115 passed**（`harness-r7-preflight.log`），包含 discovery 与
ordering 的有界传输恢复；P1 guard 仍为 106 项通过
（`p1-regression-r7-preflight.json`）。这些合成反例与流程核验不是临床金标准。

## 记忆/上下文

没有字符上限。只有：

```
触发 = floor(min(W × 0.8, W − O − 65,536))
保留 = 最新 16% × (W − O) 逐字不压
```

压力按**实际请求体**计：投影消息 + 工具 schema + tool_calls 参数。
旧实现只计正文，目录与 schema 不计，这正是那个 80,000 限不住的原因。

压不动时不写日志、不改表面——宁可这次不压，也不假装压缩成功。
崩溃留下的 `compaction/start` 会被 `abandoned_compactions()` 报出来，
必须显式关闭，不会伪装成完成。

## 用法

离线（不花钱、不读凭据）：

```sh
python -m scripts.cohort_construction.p2_checkpoint_harness.run_case \
  --sources <case>/source_fhir_r4_bundle.json \
  --output /tmp/p2h-dry --cache /tmp/p2h-dry-cache \
  --case-id SCALE-02 --dry-run
```

dry-run 打印 **run contract**：来源 hash、配置 hash、整个包的代码 hash、单位计划。
实跑必须提供一份绑定该 contract 的授权：

```json
{
  "format": "p2-harness-execution-authorization-0.1.0",
  "remote_execution_authorized": true,
  "execution_scope": "SOURCE_DISCOVERY_ORDERING_EPISODE_CANDIDATE_RECONSTRUCTION",
  "source_fhir_candidate_reconstruction_authorized": true,
  "doctor_packet_release": false,
  "real_doctor_assignment_creation": false,
  "clinical_release_permitted": false,
  "case_id": "SCALE-02",
  "sources_sha256": "<from contract>",
  "config_sha256": "<from contract>",
  "code_sha256": "<sha256 of canonical JSON of contract.code_hashes>",
  "limits": {"max_requests": 80, "max_total_tokens": 6000000}
}
```

授权只能放宽到 `limits` 指定的上限之内；`clinical_release_permitted` 等
治理字段必须为 `false`，否则在构造任何 provider 客户端之前就被拒绝。

实跑写入的目录必须是空的；`--case-receipt` 指向一个 `status=COMPLETE` 的
既有病例回执时，拒绝重跑。

## 候选 CP 与检查双节点重投影（2026-11-15）

`HCP-LIVE-026～028` 已进入离线实现和真实发现验证阶段。新增
`diagnostic_events.py`、`candidate_checkpoints.py`、`candidate_materials.py` 及
`candidate_materials_html.py`；旧 `render_materials.py` 和旧运行保持可用，不被覆盖。
新入口：

```bash
.venv/bin/python -m scripts.cohort_construction.p2_checkpoint_harness.render_candidate_materials \
  --case-result RUN/case_result.json --sources CASE/source_fhir_r4_bundle.json \
  --output NEW_PRIVATE_DIRECTORY
```

检查报告默认以 `report_time` 为可见性代理，缺失时才降级到 `verified_time`；检查前
候选以 `exam_time` 引导，报告后候选显示当前报告。程序从明确手术时间字段提取主任务
边界候选，排除术后诊断节点；这不是完整治疗阶段重建。无时间点或无可见临床上下文的
候选不生成空 CP。同一管理 source 合并为一个候选 CP，38 个旧 span 逐项保留迁移记录。

SCALE-01 离线 `candidate-cp-v4` 形成 9 个候选 CP：5 个管理决策、MR/增强CT两个检查前
节点、两个术前报告后节点；另有 9 个支持记录、2 个无时钟待核、术后及空上下文节点
迁移记录。独立审计曾发现术后节点、空 CP、跨文书泄漏、坐标错切、过度遮蔽和
hash 失配，均已针对真实产物复核修复。

真实 `SCALE-01-discovery-v2-r1` 使用新授权和空缓存完成 37/37 单位 discovery：172 次
真实 provider 调用、788,914 tokens、33 个候选、5 个证据对象，无传输恢复。最终
`SCALE-01-discovery-v2-r1-demo-r3` 形成 9 个候选 CP，33 项迁移守恒。真实结果反例
促使补充 discovery-only 空关系入口兼容和跨来源“手术指征”动作子句遮蔽；最终全部
200 项 Harness 测试和 106 项 P1 guard 通过。跨文书遮蔽仍是开发投影，不是冻结盲评
方案；HTML包含研究原文和未来节点，不是医生包，也不代表临床准确性已验证。

## 尚未具备

- **已有 r7 完整全例流程，但尚没有临床质量验证。** r1–r6 是上述
  工程调试；r6 虽为 37/37 个单位留下记录，但 18 个单位 `request_failed`，
  ordering `format_failed`，故 status=INCOMPLETE。十例真实 bundle 的
  离线 stub 预检均通过，但 stub 的事件不是模型判断，也不证明临床质量。
- **影像报告已合并到新目录。** `p2_imaging_report_merge_20261109_r2` 覆盖
  102/102 个冻结影像来源：58 个文本未变来源复现原 `source_id`，44 个更正正文后
  身份按配方改变；审核时间直接写入 `verified_time` 并作为可见时间。冻结输入未改。
- `eval.py` 只比较流程计数；r5/r6 都未完成，复核文件均
  `comparable=false`，不能据此声称减量或重建效果。
- 一份登记文档虽已补 SCALE-06 回执，但两份文档均需按十例实际完成状态重算。

## 十例离线预检（2026-11-09，stub 而非模型）

把十份真实 bundle 依次导入、分组、跑完发现→组织→episode，stub 只在第一个
单位提交一个事件、其余一律 `finish`：

| 项目 | 结果 |
|---|---|
| 可读取的病例 | 10 / 10 |
| 单位合计 | 522（28–97 / 例） |
| 单个单位正文最大 | 1,961 字符；中位 388–553 |
| 首个请求最大估算 | 2,223 token |
| 单位正文超过 60,000 上限的 | 0 |
| 每例会话数 | 单位数 + 2（组织、episode 各有独立会话）；单位之间不共享历史 |
| 段停原因 | 全部 `answered`，无预算/格式失败 |
| 目录翻页成本 | 40 行页 5.1–6.0k 字符；全量列出需 4–9 页 |
| `read_source` 取回的片段与原文逐字一致 | 是（含 `source_sha256`，截断已声明） |

这张表证明的是**管线在真实数据形状上不崩、不越界、不静默丢内容**，
不证明任何临床判断。事件数 1 是 stub 写的，不是模型写的。