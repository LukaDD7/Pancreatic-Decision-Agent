"""Stage 1: identify bounded candidate management-judgment expressions.

A reading unit is a program task, not a clinical owner or episode. Multiple
candidates in one document remain multiple located spans; this stage does not
merge, split, order, or turn them into confirmed clinical Decision Events.
"""
from __future__ import annotations

DISCOVERY_SYSTEM_PROMPT = """你在从真实病历中识别研究相关的候选管理判断表述，用于PDAC根治性切除路径的研究性重建。
你不评价治疗是否正确，不产生临床Reference，也不把程序标签当成临床事实。

核心任务：
1. 逐项找出临床团队针对当前患者做出、改变、暂缓、补充证据或不推进某项管理措施的表述。包括检查安排或调整、手术及术式或时机、减黄、分期腹腔镜、改换治疗方向，以及状态驱动的具体处置。是否实际手术、最终可切除或结局如何，不决定研究相关性。
2. 同一份文书有多个候选时逐项提交，各自保留能复现该管理判断的连续原文范围。不要判断它们应否合并、是否构成同一事件、属于哪一轮或哪个episode。组合表述和其中的具体表述都可以先保留，后续再组织。
3. scope=yes 表示这是围绕PDAC根治性切除路径的管理判断候选；scope=no 表示已识别为管理相关但明确不属于研究范围；scope=uncertain 表示无法确定。三态都只是你的发现判断，不是专家确认或最终episode纳入。action_id/action_type/target 仅是帮助理解候选的可选参考，可以全部留空，不是必答题，也不决定候选是否成立。可参考字典 action_id；字典未列出时用 OTHER_SPECIFIED 并自由填写 action_type/target。程序不审核动作集合完整性或临床正确性，最终动作由后续独立评审形成。
4. record_function 是你对该表述功能的分类：new_decision/reassessment/repeat/execution/generic/uncertain。执行或回述不会因为被记录就自动成为新决策。
5. 对本来已识别为管理相关、但明确排除的表述，用 scope=no、简短 reason 和同一原文定位记录。没有单独的 reject_mention 工具；不要求逐句提交整份文书所有无关句子。未运行、未识别与明确排除不是一回事。
6. 检查报告所见是证据，不要替它编造管理问题、检查选择原因或伪管理判断。程序会单独保留已交付检查报告的证据索引，供后续证据组织使用。

performed_test_report 的额外任务：
1. 仍须先读当前单位原文，再分别对 pre_exam_evaluation 和 evidence_arrival_reassessment 调用 review_diagnostic_candidate；两类都必须提交 disposition=yes/no/uncertain 和具体 reason，不能因程序已建立证据索引而跳过。
2. 以管理问题为中心筛选。pre_exam_evaluation 可参考报告类型填写增强CT、MR、EUS等动作，但不得捏造临床团队当初选择该检查的原因。只要该检查本身属于PDAC根治路径可评价的补充证据动作，就应 yes；不得因报告没有医嘱、选择时间、指征或部署句而判 uncertain，选择过程不明由程序保留为回溯性评价限制。报告后的 evidence_arrival_reassessment 只须根据新证据存在可作答的PDAC根治路径后续管理问题即可 yes，可另填 management_question；action_id/action_type/target 均可留空，字典未列出时用 OTHER_SPECIFIED 自由描述。该参考动作不要求报告原文已经写出管理决定，也不得冒充病历记载的临床团队决策。
3. PICC置管等输液通路安排不属于PDAC根治路径时，明确 disposition=no，并在 reason 写明范围外理由；不要靠程序关键词替你过滤。无法确定时用 uncertain 并说明缺什么。
4. review_diagnostic_candidate 是你对程序候选的筛选，不是专家真值，也不要求把报告所见写成管理句。只有原文确实另有针对当前患者的管理判断时，才另用 propose_candidate 提交其逐字片段。

工作方式：先看当前阅读任务原文，必要时用检索工具区分新判断、执行与回述。不要一次铺开全病历；未看到的材料不猜，检索未命中不等于没有发生。unit_id 只标识本次程序阅读任务，不是临床归属。quote 的 source_id 必须指向实际原文；程序按这个片段确定出处和检索时钟，不借用阅读任务的来源或时钟。

每个候选用 propose_candidate 提交。quote 必须是同一 source_id 内逐字、连续、非空的原文；包含足以复现该管理判断的指征、部署、条件或限制，不要只摘措施名。start/end 是 Python 字符偏移的半开区间；唯一出现可省略，由程序补全，重复出现必须给 start，end 必须与 quote 长度一致。不要拼接不同句子或不同文书。校验失败不会删除候选，修正后重提同一 local_id。这里不填 prechoice_quotes、postchoice_quotes、choice_time 或证据关系。完成后调用 finish。

先加载 discovery 技能了解完整要求与常见陷阱。"""

DISCOVERY_SKILL = "discovery"
INLINE_UNIT_TOKEN_BUDGET = 60_000


def finish_tool():
    from .common import finish_tool as _finish
    return _finish(label="本次阅读任务")


def build_prompt(unit, *, profile_note="", inline_token_budget=None):
    """Show exactly which text was sent and which ranges remain to be read."""
    lines = [
        "## 本次阅读任务（程序分片，不是临床归属）",
        "unit_id: %s" % unit["unit_id"],
        "kind: %s" % unit.get("kind"),
        "document_profile: %s" % unit.get("document_profile"),
        "clock（资料检索时钟，不是临床选择时刻）: %s" % (
            (unit.get("clock") or {}).get("value") or "unknown"),
        "clock_basis: %s" % (unit.get("clock_basis") or "unknown"),
        "source_ids: %s" % ", ".join(unit.get("source_ids") or ()),
    ]
    if unit.get("type_prior"):
        lines.append("type_prior（只是先验，不是结论）: %s" % unit["type_prior"])
    if profile_note:
        lines.append(profile_note)
    lines.append("\n## 本次处理单位的原文")
    budget = INLINE_UNIT_TOKEN_BUDGET if inline_token_budget is None \
        else int(inline_token_budget)
    for text in unit.get("original_texts") or ():
        source_id = text.get("source_id") or ",".join(unit.get("source_ids") or ())
        body = text.get("text") or ""
        shown, complete = _inline_span(body, budget)
        if complete:
            lines.append("### source_id=%s（共 %d 字符，以下为本处理单位中该来源的完整原文）" % (
                source_id, len(body)))
        else:
            lines.append(
                "### source_id=%s（共 %d 字符；以下仅为第 0–%d 字符，"
                "其余未随本请求发送。判断前用 read_source 从 start=%d 继续读，"
                "不要假定未显示部分没有内容。）" % (
                    source_id, len(body), len(shown), len(shown)))
        lines.append(shown)
    lines.append(
        "\n## 任务\n逐项识别本处理单位中的候选管理判断表述；同一份文书可有多个，"
        "不要判断事件拆合、组织证据或episode。每个候选用 propose_candidate 提交"
        "实际 source_id、连续逐字 quote、scope 和 record_function；"
        "action_id/action_type/target 仅是可选参考，可全部留空；字典外动作用 "
        "OTHER_SPECIFIED 自由描述，不做动作完整性、抽象词或阶段校验。"
        "必要时检索既往记录辅助区分新判断、执行和回述。"
        "已识别但明确排除的管理相关表述用 scope=no 加简短 reason，"
        "无需提交所有无关句子。检查报告所见由程序证据索引保留，不伪造管理判断。"
        "原文片段会供后续按方案遮蔽并存决策及可能泄漏的内容；本阶段不改写原文，"
        "也不决定最终盲评范围。")
    if unit.get("kind") == "performed_test_report":
        lines.append(
            "本处理单位还必须分别审核 pre_exam_evaluation 与 "
            "evidence_arrival_reassessment，各调用一次 review_diagnostic_candidate，"
            "两类都给 disposition 和 reason，缺一不可。检查前候选可依据报告类型选择具体 action_id，"
            "但不得编造检查选择原因。检查属于根治路径可评价的补证动作即可 yes，"
            "不得因缺少医嘱、指征或部署句而 uncertain。报告后候选只要能根据新证据提出"
            "可作答的PDAC根治路径后续管理问题即可 yes，不要求报告已写明管理决定；动作只是"
            "可选参考，不是病历记载的决策。动作可留空或用 OTHER_SPECIFIED 自由描述，"
            "可选 management_question；"
            "否则给 no 或 uncertain 及理由。PICC等输液通路若不属于根治路径，明确 no，"
            "reason 写明‘输液通路不属于PDAC根治路径’等实际理由。不要把报告所见编造成"
            "管理判断，也不要另传 unit_id；工具只审核程序绑定的当前单位。")
    lines.append("最后调用 finish。")
    return "\n".join(lines)


def _inline_span(body, budget):
    """Return ``(text_to_show, complete_flag)`` for one document."""
    from ..tokens import estimate_text

    if not body:
        return body, True
    if budget <= 0:
        return "", False
    if estimate_text(body) <= budget:
        return body, True
    low, high = 0, len(body)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text(body[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    while low > 0 and 0xDC00 <= ord(body[low]) <= 0xDFFF:
        low -= 1
    return body[:low], False
