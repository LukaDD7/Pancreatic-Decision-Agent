# 可复用的四问独立测试

契约：[决策接口 v0.1](../contracts/v0.4/05_decision_interface.md)。输入：[HEWV v0.4](../contracts/v0.4/01_algorithm_contract.md)。接口为建议层，不执行临床动作。

## 代码结构

| 文件 | 职责 |
|---|---|
| pancreatic_agent/episode_extract.py | 本院episode、完整来源可见性与四份固定context |
| pancreatic_agent/evaluation/resources/ | 版本化system prompt、动作表与输出schema，打包为Python资源 |
| pancreatic_agent/evaluation/protocol.py | 组装两条消息；严格JSON、mode-action、可见source_id校验 |
| pancreatic_agent/evaluation/client.py | OpenAI-compatible请求、显式凭证环境名、传输重试及精简元数据 |
| pancreatic_agent/evaluation/runner.py | 独立问题/重复调度、输入校验、逐次保存、技术统计 |
| pancreatic_agent/evaluation/__main__.py | CLI参数；默认预览，--execute才调用 |
| tests/test_checkpoint_evaluation.py | 完全虚构资料和mock transport验证 |

原序贯状态机继续保留。本入口没有动态观察或跨问记忆，不需要LangGraph；以后可复用transport、协议资源、校验与存储，序贯环境另设计。

测试入口校验并复用packet的完整context和问题，按当前版本重建system message；不直接照搬packet里可能属于旧提示词版本的messages。因此调整提示词不需要重新做数据提取，实际请求和当前协议hash均另行保存。

## 配置与执行

密钥只通过 `--key-env` 指定的环境变量读取，默认 `BOYU_API_KEY`；如换平台可显式设 `--key-env LLM_API_KEY`。不会回退到其他平台的OPENAI_API_KEY，不读取历史对话里的令牌。端点和模型用参数或 LLM_API_URL、LLM_MODEL 指定。示例使用虚构地址和模型，实际运行替换二者。

先生成完全虚构的输入包：

```sh
.venv/bin/python -m pancreatic_agent.episode_extract \
  --input examples/hewv_fictional_bundle.json --alias S001 \
  --reviews examples/hewv_fictional_review.json --output private/hewv_fictional_trial
```

预览实际会发送的完整请求，默认零调用：

```sh
.venv/bin/python -m pancreatic_agent.evaluation \
  --packets private/hewv_fictional_trial/S001/packets.json \
  --output outputs/checkpoint_evaluations \
  --base-url https://example.invalid/v1/ --model example-model \
  --allow-exploratory-draft
```

执行已准备的资料包时，在同一命令加 `--execute`，真实资料只用经授权的端点。探索性标记记录当前数据质量约定，不声称完成全部来源认证。`--stages 1 4` 可选择问题；`--repeats 5` 对每问独立重复五次。首轮默认四问各一次，不批量运行全队列。

默认temperature=1、max_tokens=8000、timeout=300秒、传输retries=0。首轮实测发现推理token也消耗生成预算，5000上限发生过截断，60秒上限发生过超时；故当前默认留足等待与生成预算，且关闭自动重试。可按模型调整参数并单独建run。超时是SDK读/连接等待设置，不是严格总运行时限。

如显式开启 `--retries 1`，只对连接错误、408/409/429/5xx重试，并保留每次尝试；认证、权限、400参数错误不重试。网络重试可能增加计费，manifest与summary记录实际尝试数。无效临床回答不重采。

输出模式默认 `--response-format prompt_only`：提示词要求严格JSON，程序本地Schema校验，不假定兼容端点支持格式约束参数。可显式选择 `json_object` 或已确认支持当前schema的 `json_schema`，不支持时记录错误，不悄悄回退。当前平台实测出现json_object下不合法JSON/字段异常，当前json_schema探针返回400；普通文本模式的一次虚构探针通过校验，因此默认选择兼容性更广的prompt_only，但不宣称它保证格式成功。

JSON mode与Schema约束的区别参考 [OpenAI Docs：Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)。第三方兼容模型不等于OpenAI官方模型，返回model字段也不认证真实底层身份。平台格式探针、不同格式参数的临床测试分别保存，不合并成一个成功率。

## 结果

每次创建新的 `outputs/checkpoint_evaluations/S001/<UTC时间_随机ID>/`：

- manifest.json：版本、输入/代码/提示词/schema/action hash、模型与参数、原资料质量标记。没有密钥值。
- requests/stage_01_repeat_01.json等：实际发送的两条消息及生成参数。完整保留患者context，仅本地忽略目录。
- results/stage_01_repeat_01.json等：原始最终回答、验证通过的decision或无效candidate、错误码、finish_reason、用量和返回model。不收集隐藏reasoning_content。
- summary.json：技术指标与各问分布；临床准确率/效用默认null。
- summary.md：便于直接阅读的各问mode、首选动作、判断和证据。

CLI退出码：0为预览成功或所有请求样本有效；1为执行完成但有技术无效样本；2为输入/配置错误；130为人工中断。每次请求启动即写入尝试日志，中断时标记INTERRUPTED且不产生临床mode，manifest记INTERRUPTED_OR_FAILED。未实现自动resume或自动补跑，重新运行会生成新run。

“引用编号有效”和“结论确实被原文支持”是不同检查。可先逐问检查原文、临床推断、取证是否必要及是否过早EXIT/CONTINUE，再制定盲法专家评分；不把所有PAUSE视为正确，也不将术后结局反填为预期答案。
