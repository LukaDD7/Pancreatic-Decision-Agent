# 本机兼容接口连通性检查

本脚本只发送固定的“你是谁”，不读取病例、不执行临床决策策略。它面向任意 OpenAI-compatible `chat/completions` 接口，不在仓库中固定服务商、端点或模型。

运行前在本机环境或被 Git 忽略的 `.env.local` 中配置：

```text
LLM_API_KEY=...
LLM_API_URL=https://example.invalid/v1/
LLM_MODEL=example-model
```

项目根目录执行：

```sh
python3 scripts/maas_smoke_test.py
```

也可以用 `--base-url` 和 `--model` 临时覆盖非敏感设置。脚本不会读取其他通用默认凭证，不回显 key；调用与响应记录写入被忽略的 `outputs/connection_tests/`。临时网络错误、429 和可重试服务错误默认最多重试五次，认证和权限错误不会重复尝试。

连通性成功只说明兼容接口能够返回文本，不证明代理平台底层模型的真实身份或版本。真实病例是否允许发送必须另行完成数据合规审查。
