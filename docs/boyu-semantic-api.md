# 项目语义 API：博约 / kimi-k3

2026-10-22 用户指定本项目及后续需要模型的自动化使用博约 API、Kimi-3。已通过该服务的 `/models` 核对模型标识为 `kimi-k3`；简短 JSON 实际请求返回同一模型字段。这确认接口可用及服务声明的模型名称，不独立证明底层模型身份。

版本化默认值在 `config/model_provider.json`。新自动化应复用 `scripts.cohort_construction.model_api.load_boyu_settings()`，不要另建密钥副本或改用其他模型作为静默回退。过程环境 `BOYU_MODEL` 可显式覆盖默认值，运行回执必须保留实际请求模型。

设置优先级：过程环境 `BOYU_*` → 本项目被忽略的 `.env.local` → `~/.config/pancreatic-decision-agent/credentials.zsh`。可用 `BOYU_CREDENTIAL_FILE` 显式指定其他凭证文件。当前项目已有 `BOYU_API_KEY`；已有外部文件提供 `BOYU_API_BASE`。隔壁日程项目也使用该外部配置；本次没有修改隔壁项目或复制密钥。

读取器只解析字面赋值，不执行 shell，不展开命令替换。端点必须是 HTTPS，不接受 URL 内用户名密码、query 或 fragment；HTTP 不跟随重定向。配置对象 repr 不打印密钥。

```python
from pathlib import Path
from scripts.cohort_construction.p2_relation_graph.model import SemanticClient

client = SemanticClient(Path("private/my_semantic_run/cache"))
# client.ask(task, payload, schema)；由具体任务定义源材料和 schema。
```

适配器验证 JSON schema、拒绝截断输出，按完整请求/模型/端点哈希缓存。模型响应、用量与请求哈希留在被忽略目录；错误只记录安全代码，不回显供应商异常正文或密钥。默认响应超时180秒、瞬时失败至多重试一次。长任务应在后台运行并持续记录进度。

本轮默认发送 `thinking={"type":"disabled"}`，该参数保存在版本化profile中并进入请求哈希。已观察到服务返回 `reasoning_tokens=0`，两个完整合成规范化请求分别约23秒、35秒完成。未关闭时曾出现75秒以上响应和180秒超时；另有一次返回不符合JSON约定。因此这只是已验证可执行的调用配置，不是可靠性或模型质量保证。参数改变应另作实验，不能混合计算性能。

现有 `load_model_api_settings()` 的 `LLM_*` 显式配置契约保持不变，避免悄悄改变旧实验。新入口默认本地准备；`--execute` 才发送输入材料。此次实际远程调用仅用了虚构文本；49人资料仅做本地准备与来源核对。真实研究资料的外发仍遵循项目既有数据流转要求。

模型请求可重现不意味着语义结果已经临床验证。API失败、schema失败、引用失败、无法判断分别进入审计记录，不从源资料池删除。
