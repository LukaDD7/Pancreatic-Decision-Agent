# 医生评审表单校验接口

这是项目自定、失败时拒绝处理的 FHIR R4 表单子集校验器，不能代替完整 FHIR Validator，也不定义实际临床方案、Mode 或 acceptable action 字典。

```python
from scripts.cohort_construction.p2_checkpoint_review import (
    ReviewValidationError, validate_questionnaire,
    validate_questionnaire_response, validate_submission,
)

validate_questionnaire(raw_questionnaire_resource)
validate_questionnaire_response(raw_questionnaire_resource, response)
validate_submission(submission, frozen_checkpoint_packet)
```

成功返回 `status=PASS` 的可序列化回执。表单或答案失败抛 `ReviewValidationError`，`code` 和 `path` 可用于界面提示和审计；错误信息不包含答案原文。`validate_submission` 首先调用既有 `packet.validate_review_submission` 核验资料包 hash、资料包绑定及冻结表单绑定，其错误类型仍为原有 `ContractError`；随后才检查真实答案。函数均不修改输入对象。

## 受支持的表单

- 必须 `resourceType=Questionnaire`、`status=active`，提供绝对 URI `url` 和明确 `version`。`active` 仅是传输生命周期要求，不代表研究临床方案已经冻结。
- 题型：`group`、`display`、`choice`、`string`、`text`、`boolean`、`integer`、`decimal`、`date`。
- `group` 可以嵌套，必须包含子项，不能重复。required group 至少包含一个可作答的后代问题；不能仅含 display。只有 `choice` 允许 `repeats=true`；相应响应使用一个 `item` 的多个 `answer`。问题下嵌套子题、重复非 choice 题及 `open-choice` 均拒绝。
- `choice` 必须携带非空 `answerOption`，同题仅一种选项类型：`valueCoding` 或 `valueString`。Coding 身份按 `system`、`code`、可选 `version` 精确匹配；`display` 不充当代码身份。未知选项、重复选项或重复作答均拒绝。
- `initial`、`initialSelected`（包括 false）、`answerValueSet`、`definition`、扩展、动态取值及未知字段出现即拒绝。`readOnly=true` 拒绝；`readOnly=false` 可用于问题或 group，但 display 禁止该字段。
- `maxLength` 仅支持 string/text 的正整数，按 Unicode codepoint 计数。Boolean 必须为真正布尔值，integer 为 FHIR 32 位整数；`1.0` 或 `1e0` 不充当 integer。decimal 为有限 JSON 数字且绝对值不超过 `9007199254740991`。date 限完整有效 `YYYY-MM-DD`。资源 `date`/响应 `authored` 限完整带时区 dateTime，偏移范围不得超过 ±14:00，拒绝无效偏移归一化及闰秒。
- `enableWhen` 可以引用深度优先顺序中已出现的叶问题；拒绝未来、自身、group、display 引用。支持 `exists`、`=`、`!=`；`>`、`<`、`>=`、`<=` 限数字或完整日期。条件类型必须与引用问题一致；多个条件必须明确 `enableBehavior=all|any`。前序依赖限制是本应用的安全子集要求。

多个答案时，`=` 和大小比较按“至少一个答案满足该运算”计算；`!=` 按“不存在等于目标的答案”计算，例如 `{A,B} != A` 为假，`{B} != A` 为真，无答案时 `!= A` 也为真。`false` 和 `0` 均为真实已答值。禁用父 group 会禁用全部后代；禁用项不得出现在响应中，界面必须在条件变化后清掉对应答案。空白字符串和空 `answer` 数组不能充当答案。

本应用另加两条严格提交规则：省略父 group 不能绕过已启用的必答后代问题；已启用的 required group 至少有一个实际作答的后代问题，display 或空子节点不能满足。后一条比 FHIR 通用的“group 至少含一个子元素”要求更严格；它不将 group 下所有 optional 问题改成必答。临床方案冻结时需接受这两条子集约束。

纯空白字符串的判定使用明确的跨语言集合：Python Unicode `isspace()` 字符加 U+FEFF。普通 JS `trim()` 与 Python `strip()` 的集合不同，不能直接替代本规则。URI 必须具有非空的冒号后部分，并排除同一集合中的空白/BOM。

响应必须绑定 `url|version`，提交默认只接收 `completed`。`require_completed=False` 供草稿结构检查使用；其 PASS 不代表可进入正式盲评台账。回答必须保留表单树结构及兄弟项顺序，可省略未答可选问题。未知 linkId、重复 item、显示文本作答、group 作答、一个答案包含多种 value[x]、额外附件或字段均拒绝。

## 完整字段白名单

|对象|字段|
|---|---|
|Questionnaire|resourceType, id, url, version, status, experimental, name, title, description, purpose, publisher, date, language, item|
|Questionnaire.item|linkId, prefix, text, type, required, repeats, readOnly, maxLength, answerOption, enableWhen, enableBehavior, item|
|QuestionnaireResponse|resourceType, id, questionnaire, status, authored, item|
|QuestionnaireResponse.item|linkId, text, answer, item|
|Coding|system, version, code, display|

子结构中的 `answerOption` 仅允许一个 `valueCoding` 或 `valueString`；`enableWhen` 仅允许 `question`、`operator` 和一个受支持的 `answer[x]`；实际 answer 仅允许该题对应的一个 `value[x]`。响应可附题目 `text`，但必须与冻结题目原文完全一致。

## 规范依据与测试

资源和答案层级以 [HL7 FHIR R4 Questionnaire](https://hl7.org/fhir/R4/questionnaire-definitions.html)、[QuestionnaireResponse](https://hl7.org/fhir/R4/questionnaireresponse-definitions.html) 及 [enable operator 定义](https://hl7.org/fhir/R4/codesystem-questionnaire-enable-operator.html) 为依据。R4 的 `!=` 定义含语病，因此参照 [HL7 R5 对同一运算符的明确说明](https://hl7.org/fhir/R5/codesystem-questionnaire-enable-operator.html) 将其解释为“不存在相等答案”，并固定上述本地契约和反例。本应用明确缩小了可执行范围；遇到范围外特性拒绝整张表，不静默忽略或转换。外部 FHIR 完整规范验证和研究方案审批仍需独立完成。

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/anaconda3/bin/python3 -m pytest scripts/cohort_construction/p2_checkpoint_review/tests -q
```

测试全为虚构资料和虚构选项；稳定问题编号和必需反例/对照登记于 `regression_issue_map.json`。

另外可运行 `python -m scripts.cohort_construction.p2_checkpoint_review.tests.differential_review`，对 325 个固定虚构案例执行 Python 与 HTML 所用 JS 校验器的独立对照。涵盖嵌套 group、禁用父项、组内前序引用、日期、Unicode、非有限数字及固定 seed 变异。传输反例保留 `1.0`/`1e0` 原始 JSON token：浏览器入口必须使用 `parseJsonUnique` 保留相应词法信息，不能先通过 `JSON.parse`/`JSON.stringify` 擦掉小数点，再声称已核验 FHIR integer。直接构造的 JS 数字对象无法保留这一词法区别；HTML 正常录入的整数输出使用整数字面量。
