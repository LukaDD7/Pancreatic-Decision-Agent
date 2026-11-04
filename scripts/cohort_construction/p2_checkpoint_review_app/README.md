# 医生专用单文件阅读与评审应用

2026-10-28。应用只读取通过Python预验证的医生许可FHIR Bundle，显示当前可见的原文，并按外部冻结Questionnaire生成表单。实际Mode/动作字典由研究方案提供；代码与演示表单不定义临床Reference。

## 已接通的接口

```text
原始来源 + 分割提案 → CompiledPartition（研究者）
  + 独立材料语义审核 + 冻结Questionnaire → FrozenCheckpointPacket
  → 医生专用FHIR Bundle → 单文件HTML
  → 医生实际多选/文字答案 → ReviewSubmission（内含QuestionnaireResponse）
  → Python表单/版本/分配核验 → 最小评审响应FHIR Bundle → 台账
```

HTML没有读取研究者原文后再隐藏的入口。生成程序只嵌入医生FHIR合同内的当前Checkpoint、许可正文和冻结问卷。研究者Episode关系图、未来Checkpoint、被遮原文及原文附件链接不进入该文件。临床正文、来源ID、题干和选项均按文本呈现，原文中的HTML样文字不会成为可执行元素。

实际FHIR资源与扩展定义见 `p2_checkpoint_fhir/`；表单支持子集与权威回收校验见 `p2_checkpoint_review/`。本应用镜像该子集用于即时校验，不是通用FHIR浏览器，也没有运行完整FHIRPath/profile/术语服务校验。Python生成入口先运行完整R4 JSON Schema及本地合同核验，之后浏览器重新检查资源集合、原文hash、问卷hash、资料包hash和可信分配绑定。

## 生成

研究协调者提供已经审核、冻结并分配的医生Bundle及可信packet hash：

```bash
python3 -m scripts.cohort_construction.p2_checkpoint_review_app.build \
  --bundle doctor-bundle.json --assignment-id REVIEW-001 \
  --expected-packet-sha256 TRUSTED_PACKET_SHA256 \
  --output reviewer-001.html
```

输出要求新文件，不覆盖已有交付。医生用现代Chrome打开HTML，即可阅读全文、填写/多选意见并导出JSON；不需要服务器。文件不自动上传意见，也不静默保存草稿；导出前关闭会提示有未导出修改。页面展示的来源顺序是许可包顺序，实际时间以原文为准。

研究协调者用 `p2_checkpoint_review.validate_submission(submission, trusted_packet)`核验回收数据，再通过 `p2_checkpoint_fhir.response_bundle_from_submission`转换成响应FHIR Bundle。回收时还应以外部分配台账核验`reviewer_assignment_id`，FHIR响应导入函数提供`expected_reviewer_assignment_id`参数。hash提供一致性绑定，不是数字签名，也不能认证医生身份、盲评接触史或材料审定的真实性。

## 表单操作

- `choice.repeats=true`产生多选；单选和布尔题可以清除，恢复未作答。所有题目初始不填答案。
- 依赖题依据冻结问卷的`enableWhen/enableBehavior`显示；条件不成立时，清空旧答案并从导出中省略。
- 必填、选项代码/系统/版本、类型、组结构、字符数与表单版本均校验。`false`、整数0与空答案分别处理。
- 纯空白不算已答；字符数按Unicode code point，保留原文换行和BOM，不做NFC/NFKC规范化。
- 原始JSON拒绝重复key、非有限数和未配对surrogate。整数题及整数约束保留文件内原数字词法，拒绝`1.0`或`1e0`冒充整数；普通JavaScript对象自身无法追溯其原始数字拼写，因此文件入口必须使用`parseJsonUnique`。
- Python和JavaScript对小数的JSON序列化不同。FHIR包带有只含许可问卷的精确序列化helper；浏览器核它的UTF-8 hash，避免把合法的`1.0`或`1e-7`误判为资料改变。

浏览器API仅用于该已预验证合同与测试，不应宣称其所有格式拒绝规则都与Python完整R4验证等价。若将来增加任意外部FHIR文件导入，仍需Python预验证或补齐相应验证。

## 虚构演示与验收

`synthetic_demo.py`只生成虚构患者文字和演示Mode/动作选项，`experimental=true`且页面有明确演示标识。其中语义审核声明也是虚构机制fixture，不代表真实医生做过审核。

`tests/test_browser.py`在实际Chrome中检查无预填、必填、多选、条件切换清除、恢复未作答、实际下载、移动端排版、原文HTML样文字不执行及零HTTP请求；下载结果再经Python问卷核验与响应FHIR往返。

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/anaconda3/bin/python3 -m pytest -q \
  scripts/cohort_construction/p2_checkpoint_review_app/tests
```

Node/Playwright使用桌面内置运行时；`REVIEW_CHROME_PATH`可以指定用于离线验收的Chrome可执行文件。实际医生交付只有一个HTML，不依赖Node或Playwright。
