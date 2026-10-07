# 本机 MaaS 连通性检查

本脚本只发送固定的“你是谁”，不读取病例、不执行临床决策策略。调用从本机发起，推理仍在第三方 MaaS 服务端完成，不是本地模型部署。

配置依据：用户提供的 [MaaS 平台操作手册](https://sii-czxy.feishu.cn/wiki/Y8eLw60KKiNgQzk4mpUcHJItnth) 及其链接的 [商用 API 接口文档](https://sii-czxy.feishu.cn/wiki/UyImw9WYdiZHVHk9GrIcvO4lnYe?table=ldxiMHTGiLjIUx6h)。文档指定 `https://apicz.boyuerichdata.com/v1`，支持通用 `chat/completions` 端点，并建议遇到临时错误时重试。SDK 客户端写法参考 [OpenAI Python 官方文档](https://developers.openai.com/api/reference/python)；第三方模型权限和参数支持须实测。

项目根目录执行：

```sh
python3 scripts/maas_smoke_test.py
```

按提示在终端输入该平台的 key，不回显，也不保存 key。也可在本机 `.env.local` 中填入 `BOYU_API_KEY=...`；此文件已被 Git 忽略。脚本不使用机器上来源不明的 `OPENAI_API_KEY`，服务地址固定为用户指定的平台。

默认请求与用户示例一致：`kimi-k3`、`max_tokens=200`、`temperature=1`。临时网络错误、429 和可重试的服务错误默认最多重试五次，间隔 30 秒；认证和权限错误不会重复尝试。调用与响应记录写入被忽略的 `outputs/connection_tests/`，不记录 key 或授权头。若 token 限制导致没有最终文本，可用 `--max-tokens 1024` 明确扩大生成限额再测。

2026-10-07 检查结果：本机 Python 3.14.3，已安装 OpenAI SDK 2.37.0。无认证访问 `/v1/models` 返回 HTTP 401；配置用户提供的令牌后，`kimi-k3` 对固定的“你是谁”成功返回最终文本。真实病例未发送。模型返回的别名及自我介绍不独立证明代理平台底层权重的身份或版本。

用户级持久环境已配置：令牌位于项目之外的 `~/.config/pancreatic-decision-agent/credentials.zsh`，目录权限 700、文件权限 600；`~/.zshenv` 引用该文件，用户 LaunchAgent 在登录时恢复桌面进程环境。使用 `BOYU_API_KEY` 和 `BOYU_API_BASE`，没有覆盖已有 `OPENAI_API_KEY`。不把令牌值写入 Git、源码、manifest 或 LaunchAgent plist。环境变量本身不是加密保管库，同一用户的程序可以读取它。
