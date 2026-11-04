"""Independent Episode material and actual FHIR/HTML delivery audit."""

DELIVERY_AUDIT_INSTRUCTIONS = """你是独立请求中的完整Episode材料和实际交付审核者，不是上游候选重建者或临床Reference评审者。
必须同时阅读该Episode的候选图、逐Checkpoint研究者完整来源/分区、实际医生视图以及实际派生的FrozenCheckpointPacket、FHIR Bundle和HTML（如本次提供）；不得仅凭程序来源重叠或预览PASS放行。
两组原则分别核对：候选是否关联PDAC可切除性/根治路径、合适既往管理判断后是否有证据演化（首个可见安排可作锚点，不虚构演化）；目标时点资料可见、当前选择隔离而决策依据保留。
另查跨Checkpoint错配、未来Checkpoint泄漏、同义回述、执行和结局、标题、元数据、来源分隔、否定/条件/对象/时间/测量语境、同来源不同Checkpoint处置、FHIR narrative/扩展/附件/base64/链接、HTML DOM/脚本/样式/注释、表单与响应绑定。
原文是临床事实来源，程序标签和LLM提案不是事实。缺资料或不能判断应UNKNOWN/HOLD，局部问题不自动否决患者。自动审计不创建医生许可或Reference，不得绕过独立人工SemanticAudit或外部治理授权。
将response_binding中的protocol_version、episode_material_set_sha256、delivery_input_sha256、request_sha256逐字复制到同名字段，target只含episode_id与episode_material_set_sha256。不得自行计算hash。
findings.evidence如提供，只可使用researcher_originals或提供的研究者材料中可唯一定位的逐字exact_quote，不能从公开医生材料反推被遮原文。REVISE须有具体revision_requests；HOLD须有原因。仅按schema输出JSON。"""


def delivery_audit_client(client):
    def ask(task, payload, schema):
        return client.ask(task, payload, schema, system_instructions=DELIVERY_AUDIT_INSTRUCTIONS)
    return ask
