"""Private offline HTML for reprojected candidate Checkpoint research review.

Every packet, including later Checkpoints and researcher-only originals, is
embedded in this file. It is not a clinician distribution or a blinded doctor
packet. The renderer performs no provider or network requests.
"""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path

HTML_FORMAT = "p2-candidate-checkpoint-private-preview-0.5.0"
RESPONSE_FORMAT = "p2-candidate-checkpoint-action-draft-0.4.0"
FORM_VERSION = "candidate-action-plan-development-0.4"
KIND_LABELS = {
    "BEFORE_ACQUISITION": "证据获取前状态",
    "AFTER_EVIDENCE_ARRIVAL": "新证据到达后状态",
    "MANAGEMENT_REASSESSMENT": "管理再评估状态",
}


def _h(value):
    return html.escape("" if value is None else str(value), quote=True)


def _node(tag, attributes=None, *children):
    attrs = "".join(
        f' {key}="{_h(value)}"' for key, value in (attributes or {}).items()
        if value is not None
    )
    return f"<{tag}{attrs}>{''.join(children)}</{tag}>"


def _pre(value, attributes=None):
    return _node("pre", attributes, _h(value))


def _clock_text(clock):
    if isinstance(clock, dict):
        value = clock.get("value") or "未知"
        precision = clock.get("precision")
        return f"{value}（{precision}）" if precision else str(value)
    return str(clock or "未知")


def _source_clocks(source):
    clocks = source.get("clocks") or {}
    items = []
    used = set()
    for keys, label in [
        (("exam_time", "exam"), "exam"),
        (("report_time", "report"), "report"),
        (("verified_time", "verified"), "verified"),
    ]:
        key = next((key for key in keys if key in clocks), None)
        item = clocks.get(key) if key else None
        if key:
            used.add(key)
        value = _clock_text(item) if item is not None else "未知"
        items.append(_node("li", {}, _h(f"{label}: {value}")))
    for key, item in clocks.items():
        if key not in used:
            items.append(_node("li", {}, _h(f"{key}: {_clock_text(item)}")))
    return _node("ul", {"class": "source-clocks"}, *items)


def _source_card(source, *, researcher=False, role=None):
    classes = "source researcher-source" if researcher else "source"
    if role:
        classes += f" {role}-source"
    title = source.get("title") or "无标题来源"
    role_label = {"target": "目标来源", "context": "上下文来源"}.get(role)
    return _node(
        "article", {"class": classes},
        *([_node("p", {"class": "source-role"}, _h(role_label))] if role_label else []),
        _node("h4", {}, _h(title)),
        _node("p", {"class": "meta"}, _h(
            f"来源 {source.get('source_id') or '未标来源'} · "
            f"类型 {source.get('kind') or '未分类'} · "
            f"检索时钟 {_clock_text(source.get('clock'))}"
        )),
        _node("details", {"class": "clock-details"},
              _node("summary", {}, "exam / report / verified 来源时钟"),
              _source_clocks(source)),
        _node("p", {"class": "small"}, _h(
            f"呈现范围：{source.get('presentation_scope') or 'researcher_original' if researcher else source.get('presentation_scope') or '未标注'}"
        )),
        _pre(source.get("text") or ""),
    )


def _visible_source_groups(sources, target_ids, evaluation_clock):
    evaluation_value = (evaluation_clock or {}).get("value") or ""
    evaluation_day = evaluation_value[:10] if len(evaluation_value) >= 10 else ""

    def day(source):
        value = (source.get("clock") or {}).get("value") or ""
        if len(value) >= 10:
            return value[:10]
        if source.get("source_id") in target_ids and evaluation_day:
            return evaluation_day
        return "时间未知"

    groups = {}
    for source in sources:
        groups.setdefault(day(source), []).append(source)
    ordered_days = sorted((value for value in groups if value != "时间未知"), reverse=True)
    if "时间未知" in groups:
        ordered_days.append("时间未知")
    open_day = evaluation_day if evaluation_day in groups else (ordered_days[0] if ordered_days else None)
    rendered = []
    for value in ordered_days:
        items = groups[value]
        label = f"{value}（24小时）" if value != "时间未知" else value
        rendered.append(_node(
            "details", {"class": "day-source-group", "open": "" if value == open_day else None},
            _node("summary", {}, _h(
                f"{label} · {len(items)} 份来源" + (" · 当前CP评价日" if value == evaluation_day else ""))),
            *[_source_card(source, role="target" if source.get("source_id") in target_ids else "context")
              for source in items]))
    return rendered


def _researcher_originals(packet):
    originals = packet.get("researcher_target_originals") or []
    if isinstance(originals, dict):
        if "text" in originals or "source_id" in originals:
            originals = [originals]
        else:
            originals = [dict(source_id=source_id, text=text)
                         for source_id, text in originals.items()]
    cards = []
    for original in originals:
        if isinstance(original, str):
            cards.append(_pre(original))
        else:
            cards.append(_source_card(original, researcher=True))
    return _node(
        "section", {"class": "step workflow-step researcher-only",
                    "id": "workflow-research", "hidden": ""},
        _node("div", {"class": "step-number"}, "02"),
        _node("div", {"class": "step-body"},
              _node("div", {"class": "research-warning"},
                    _node("strong", {}, "研究审核，不是医生投影"),
                    _node("p", {}, "以下原文仅供研究者核对；不得把本页作为医生盲评材料分发。")),
              *(cards or [_node("p", {"class": "empty"}, "未提供研究审核原文。")]),
              *([_candidate_actions(packet)]
                if packet.get("kind") == "BEFORE_ACQUISITION" else [])),
    )


def _mask_span(span):
    safe = {key: span.get(key) for key in (
        "source_id", "start", "end", "label", "mask_reason", "reason"
    ) if span.get(key) is not None}
    return _pre(json.dumps(safe, ensure_ascii=False))


def _masked_projection(packet):
    projection = packet.get("masked_projection") or {}
    spans = projection.get("masked_spans") or []
    whole = projection.get("whole_source_ids") or []
    reviewed = projection.get("cross_document_reviewed")
    return _node(
        "section", {"class": "step workflow-step mask-step",
                    "id": "workflow-mask", "hidden": ""},
        _node("div", {"class": "step-number"}, "03"),
        _node("div", {"class": "step-body"},
              _node("div", {"class": "step-kicker"}, "MASK 范围、理由与遮蔽后全文"),
              _node("h3", {}, "联合遮蔽投影"),
              _node("div", {"class": "mask-reason"},
                    _node("strong", {}, "遮蔽理由"),
                    _node("p", {}, _h(projection.get("reason") or "未提供"))),
              _node("p", {}, _h(
                  f"局部 MASK {len(spans)} 处 · 整份来源遮蔽 {len(whole)} 份 · "
                  f"跨文书复核：{'已记录' if reviewed else '未确认'}"
              )),
              _node("details", {},
                    _node("summary", {}, "查看 MASK 元数据（不展示被遮原文）"),
                    *[_mask_span(span) for span in spans],
                    _pre(json.dumps(whole, ensure_ascii=False))),
              _node("h4", {}, "遮蔽后全文"),
              *[_source_card(source) for source in packet.get("included_sources") or []]),
    )


def _relation_text(item):
    relation = (item or {}).get("relation")
    if isinstance(relation, dict):
        return relation.get("relation") or relation.get("type") or "未分类"
    return relation or "未分类"


def _neighbor(packet, identifier, direction):
    if not identifier:
        return _node("article", {"class": "neighbor empty"},
                     _node("strong", {}, "前一节点" if direction == "previous" else "后一节点"),
                     _node("p", {}, "本 Episode 内无相邻节点。"))
    relation = next((item for item in packet.get("related_checkpoints") or []
                     if item.get("checkpoint_id") == identifier), None)
    return _node(
        "article", {"class": "neighbor"},
        _node("div", {"class": "step-kicker"},
              "前一节点" if direction == "previous" else "后一节点"),
        _node("h4", {}, _h(identifier)),
        _node("p", {}, _h(
            f"关系：{_relation_text(relation)} · "
            f"说明：{(relation or {}).get('projection_explanation') or '未记录直接关系'}"
        )),
        _node("button", {"type": "button", "class": "secondary", "data-goto": identifier},
              "打开该 Checkpoint"),
    )


def _relations(packet):
    navigation = packet.get("episode_navigation") or {}
    related = packet.get("related_checkpoints") or []
    return _node(
        "section", {"class": "step workflow-step", "id": "workflow-relations", "hidden": ""},
        _node("div", {"class": "step-number"}, "04"),
        _node("div", {"class": "step-body"},
              _node("div", {"class": "step-kicker"}, "Episode 前后节点及关系"),
              _node("h3", {}, _h(
                  f"{packet.get('episode_id') or '未分配 Episode'} · "
                  f"{navigation.get('index', '?')} / {navigation.get('total', '?')}"
              )),
              _node("div", {"class": "neighbor-grid"},
                    _neighbor(packet, navigation.get("previous_checkpoint_id"), "previous"),
                    _neighbor(packet, navigation.get("next_checkpoint_id"), "next")),
              _node("details", {},
                    _node("summary", {}, f"全部已记录关系（{len(related)} 条）"),
                    *[_node("article", {"class": "relation"},
                            _node("button", {"type": "button", "class": "secondary",
                                             "data-goto": item.get("checkpoint_id")},
                                  _h(item.get("checkpoint_id"))),
                            _node("p", {}, _h(
                                f"方向：{item.get('direction') or '未标明'} · "
                                f"来源：{item.get('relation_source') or '旧event关系迁移'} · "
                                f"{_relation_text(item)}：{item.get('projection_explanation') or '具体理由在独立关系运行文件核对'}"
                            )),
                            _node("p", {"class": "small"}, _h(
                                f"关系引文 {item.get('basis_quote_count', 0)} 条；"
                                "请在独立关系运行文件核对，不纳入当前时点可见资料。"
                            ))) for item in related]),
              _node("p", {"class": "small"}, "显示顺序不等于已确认的临床决策顺序。")),
    )


def _action_row():
    return _node(
        "fieldset", {"class": "action-row"},
        _node("legend", {}, "具体动作"),
        _node("label", {}, "action_type",
              _node("input", {"name": "action_type", "type": "text", "required": "required"})),
        _node("label", {}, "target",
              _node("input", {"name": "target", "type": "text", "required": "required"})),
        _node("label", {}, "timing",
              _node("input", {"name": "timing", "type": "text", "required": "required"})),
        _node("label", {}, "conditions（每行一项，可选）",
              _node("textarea", {"name": "conditions"})),
        _node("button", {"type": "button", "class": "secondary remove-action"}, "移除此动作"),
    )


def _action_form(packet):
    space = packet.get("action_space") or {}
    modes = space.get("modes") or []
    schema = space.get("action_plan_schema") or {}
    return _node(
        "section", {"class": "step workflow-step action-step",
                    "id": "workflow-action", "hidden": ""},
        _node("div", {"class": "step-number"}, "05"),
        _node("div", {"class": "step-body"},
              _node("div", {"class": "step-kicker"}, "动作 schema 与可填写动作计划草稿"),
              _node("h3", {}, "管理动作计划（草稿，不是临床 Reference）"),
              _node("p", {}, _h(space.get("note") or "程序不预填答案。")),
              _node("details", {},
                    _node("summary", {}, "查看 action_plan_schema"),
                    _pre(json.dumps(schema, ensure_ascii=False, indent=2))),
              _node("form", {"class": "action-form", "data-checkpoint": packet["checkpoint_id"]},
                    _node("label", {}, "Mode",
                          _node("select", {"name": "mode", "required": "required"},
                                _node("option", {"value": ""}, "未填写"),
                                *[_node("option", {"value": mode}, _h(mode)) for mode in modes])),
                    _node("div", {"class": "actions"}, _action_row()),
                    _node("button", {"type": "button", "class": "secondary", "id": "add-action"},
                          "新增具体动作"),
                    _node("label", {}, "rationale",
                          _node("textarea", {"name": "rationale", "required": "required"})),
                    _node("label", {}, "evidence_to_obtain（每行一项，可选）",
                          _node("textarea", {"name": "evidence_to_obtain"})),
                    _node("div", {},
                          _node("button", {"type": "submit"}, "保存本机草稿"),
                          _node("span", {"class": "saved", "aria-live": "polite"})))),
    )


def _candidate_actions(packet):
    actions = packet.get("candidate_actions")
    cards = []
    if isinstance(actions, list):
        for index, action in enumerate(actions, 1):
            if not isinstance(action, dict):
                continue
            reasons = [
                f"{label}：{action[key]}"
                for key, label in (("reason", "reason"), ("rationale", "rationale"))
                if action.get(key)
            ]
            provenance = [
                f"{label}：{action[key]}"
                for key, label in (("actor", "actor"), ("review_source", "review_source"))
                if action.get(key)
            ]
            cards.append(_node(
                "article", {"class": "candidate-action"},
                _node("h4", {}, _h(f"候选动作 {index}")),
                _node("dl", {"class": "candidate-action-grid"},
                      _node("div", {}, _node("dt", {}, "action_type"),
                            _node("dd", {}, _h(action.get("action_type") or "未提供"))),
                      _node("div", {}, _node("dt", {}, "target"),
                            _node("dd", {}, _h(action.get("target") or "未提供"))),
                      _node("div", {}, _node("dt", {}, "理由"),
                            _node("dd", {}, _h("；".join(reasons) or "未提供理由")))),
                *([_node("p", {"class": "small"}, _h(" · ".join(provenance)))]
                  if provenance else []),
            ))
    status = packet.get("discovery_review_status")
    return _node(
        "section", {"class": "candidate-actions", "aria-label": "Agent候选动作，不是答案或Reference"},
        _node("h3", {}, "Agent候选动作（不是答案/Reference）"),
        *([_node("p", {"class": "small"}, _h(f"发现复核状态：{status}"))] if status else []),
        *(cards or [_node(
            "p", {"class": "technical-failure", "role": "alert"},
            "技术失败警示：material packet 未提供有效的 candidate_actions（非空对象数组），"
            "无法显示 Agent 候选动作。这不表示临床上没有可行动作，也不是临床结论。"
        )]),
    )


def _navigation(packet):
    sequence = packet.get("sequence_navigation") or {}
    episode = packet.get("episode_navigation") or {}

    def button(identifier, scope, direction, label):
        return _node("button", {"type": "button", "class": "secondary",
                                "data-goto": identifier,
                                "data-navigation": f"{scope}-{direction}",
                                "disabled": "disabled" if not identifier else None}, label)

    return _node(
        "div", {"class": "checkpoint-navigation"},
        _node("nav", {"class": "checkpoint-nav", "aria-label": "本患者显示顺序节点切换"},
              button(sequence.get("previous_checkpoint_id"), "sequence", "previous",
                     "← 本患者上一 CP"),
              _node("span", {}, _h(
                  f"本患者 CP {sequence.get('index', '?')} / {sequence.get('total', '?')}"
              )),
              button(sequence.get("next_checkpoint_id"), "sequence", "next",
                     "本患者下一 CP →")),
        _node("p", {"class": "small navigation-note"},
              "本患者导航按 display_order 排列，仅用于浏览当前患者的候选 Checkpoint；显示顺序不等于已确认的临床决策、治疗或时间关系。"),
        _node("nav", {"class": "checkpoint-nav", "aria-label": "Episode 内节点切换"},
              button(episode.get("previous_checkpoint_id"), "episode", "previous",
                     "← Episode 内上一 CP"),
              _node("span", {}, _h(
                  f"Episode 内 {episode.get('index', '?')} / {episode.get('total', '?')}"
              )),
              button(episode.get("next_checkpoint_id"), "episode", "next",
                     "Episode 内下一 CP →")),
    )


def _packet_html(packet):
    evaluation = packet.get("evaluation") or {}
    target = packet.get("target") or {}
    order = packet.get("display_order") or {}
    header = _node(
        "header", {"class": "task-header"},
        _node("div", {"class": "eyebrow"}, "候选 Checkpoint · 私有研究审核"),
        _node("h2", {}, _h(target.get("title") or "通用管理任务")),
        _node("p", {"class": "question"}, _h(
            "管理问题：" + (packet.get("management_question") or "未提供管理问题"))),
        _node("dl", {"class": "task-grid"},
              _node("div", {}, _node("dt", {}, "Checkpoint"),
                    _node("dd", {}, _h(packet["checkpoint_id"]))),
              _node("div", {}, _node("dt", {}, "任务类型"),
                    _node("dd", {}, _h(
                        KIND_LABELS.get(packet.get("kind"), packet.get("kind") or "未分类")))),
              _node("div", {}, _node("dt", {}, "管理目标"),
                    _node("dd", {}, _h(
                        (packet.get("management_goal") or {}).get("label")
                        if isinstance(packet.get("management_goal"), dict)
                        else packet.get("management_goal") or "PDAC根治性切除路径"))),
              _node("div", {}, _node("dt", {}, "当前阶段 phase"),
                    _node("dd", {}, _h(packet.get("phase") or "旧版候选未提供"))),
              _node("div", {}, _node("dt", {}, "当前CP原始文书类型"),
                    _node("dd", {}, _h(
                        f"{target.get('title') or '未标来源'}（{target.get('document_profile') or '未分类'}）"))),
              _node("div", {}, _node("dt", {}, "评价时点"),
                    _node("dd", {}, _h(_clock_text(evaluation.get("clock"))))),
              _node("div", {}, _node("dt", {}, "时间依据"),
                    _node("dd", {}, _h(evaluation.get("time_basis") or "未提供"))),
              _node("div", {}, _node("dt", {}, "时点角色"),
                    _node("dd", {}, _h(evaluation.get("role") or "未提供"))),
              _node("div", {}, _node("dt", {}, "显示顺序"),
                    _node("dd", {}, _h(f"{order.get('index', '?')} / {order.get('total', '?')}")))),
        _node("p", {"class": "small"}, _h(
            "发现阶段 Agent 分类：" + ", ".join(packet.get("record_functions") or ["检查事件由程序组织"])
            + "；旧 span → 本 CP：" + ", ".join(packet.get("migration_event_ids") or ["无旧管理 span"])
            + "；支持记录：" + ", ".join(packet.get("supporting_event_ids") or ["无"])
            + "；旧 Episode 来源：" + ", ".join(packet.get("origin_episode_ids") or ["无"])
        )),
        _node("p", {"class": "time-guidance"},
              "仅依据本节点评价时点可见资料作答；检索时钟及报告可见性代理不等于医生实际阅读或选择时刻。"),
        _node("div", {"class": "state-delta"},
              _node("strong", {}, "本状态相对前一状态的新增内容"),
              _node("p", {}, _h(
                  "新证据：即将获取的证据已遮蔽，任务是选择下一步管理动作。"
                  if packet.get("kind") == "BEFORE_ACQUISITION" else
                  "当前报告作为新证据完整可见，后续管理动作仍遮蔽。"
                  if packet.get("kind") == "AFTER_EVIDENCE_ARRIVAL" else
                  "当前管理问题或条件发生变化；重复执行记录仅作支持。")),
              _node("p", {"class": "small"}, _h(
                  "action_cycle_id：" + str(packet.get("action_cycle_id") or "旧版未提供")
                  + " · evidence_state_id：" + str(packet.get("evidence_state_id") or "旧版未提供")
                  + " · 支持来源：" + ", ".join(packet.get("support_source_ids") or ["无"])))),
        (_node("p", {"class": "small"},
               "Discovery 候选动作及理由含目标检查信息，已移至“研究原文”页；默认任务头不展示，避免泄漏。")
         if packet.get("kind") == "BEFORE_ACQUISITION" else _candidate_actions(packet)),
        _node("details", {"class": "migration-details"},
              _node("summary", {}, _h(
                  f"本 CP 的旧候选迁移与归并理由（{len(packet.get('migration_records') or [])} 项）")),
              *[_node("p", {"class": "small"}, _h(
                  f"{item.get('event_id') or '未标事件'} · {item.get('migration_status') or '未标状态'}"
                  f" · {item.get('reason') or '未标理由'}"))
                for item in packet.get("migration_records") or []]),
        _node("p", {"class": "meta"}, _h(
            "目标来源 ID：" + ", ".join(target.get("source_ids") or [])
        )),
        (_node("p", {"class": "technical-failure target-cutoff-warning"}, _h(
            "目标原文截止后创建，未进入当时可见资料："
            + ", ".join(target.get("created_after_cutoff_source_ids") or [])
            + "。保留其他时点内上下文供审核，不将该文书正文回填到本状态。"
         )) if target.get("created_after_cutoff_source_ids") else ""),
    )
    target_ids = set(target.get("source_ids") or [])
    included_sources = packet.get("included_sources") or []
    originals = _node(
        "section", {"class": "step workflow-step", "id": "workflow-original"},
        _node("div", {"class": "step-number"}, "01"),
        _node("div", {"class": "step-body"},
              _node("div", {"class": "step-kicker"}, "本节点评价时点可见且已完成遮蔽的原文"),
              _node("h3", {}, "当时可见资料"),
              _node("p", {"class": "small"},
                    "全部 included_sources 按自然日（每个24小时折叠块）排列；当前CP评价日默认展开，其余日期可按需展开。MASK 详情及研究审核未遮原文在独立页展示。"),
              *(_visible_source_groups(included_sources, target_ids, evaluation.get("clock"))
                or [_node("p", {"class": "technical-failure", "role": "alert"},
                          "技术失败警示：未提供 included_sources，无法呈现当时可见资料。")])),
    )
    workflow = _node("nav", {"class": "workflow-tabs", "aria-label": "候选 CP 工作页"}, *[
        _node("button", {"type": "button", "data-workflow": name,
                         "class": "secondary" + (" active" if name == "original" else "")}, label)
        for name, label in (("original", "当时可见资料"), ("research", "研究原文"),
                            ("mask", "MASK"), ("relations", "前后关系"),
                            ("action", "动作填写"))])
    return (header + workflow + _navigation(packet) + originals + _researcher_originals(packet)
            + _masked_projection(packet) + _relations(packet) + _action_form(packet)
            + _navigation(packet))


_STYLE = r"""
:root{color-scheme:light;--bg:#f3f6f4;--paper:#fff;--ink:#17231d;--muted:#5c6a62;--line:#d3ded7;--accent:#17654c;--soft:#edf3ef;--warn:#8a4b08}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}main{max-width:1200px;margin:auto;padding:24px 18px 64px}h1{font-size:26px;margin:0}h2{font-size:25px;margin:4px 0 12px}h3{font-size:20px;margin:4px 0 10px}h4{margin:16px 0 4px}.private-note{margin:6px 0 12px}.bar{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 0}select,input,textarea,button{font:inherit}select,input,textarea{border:1px solid var(--line);border-radius:5px;padding:8px;background:#fff;max-width:100%}button{border:0;border-radius:5px;background:var(--accent);color:#fff;padding:9px 14px;cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}.secondary{background:var(--soft);color:var(--accent);border:1px solid #aac4b5}.small,.meta{font-size:13px;color:var(--muted)}.task-header{background:var(--paper);border:1px solid var(--line);border-top:4px solid var(--accent);border-radius:9px;padding:12px 16px;margin-top:8px}.eyebrow,.step-kicker{font-size:12px;font-weight:750;letter-spacing:.03em;color:var(--accent)}.question{font-size:19px;font-weight:700;line-height:1.45;margin:8px 0}.task-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px 16px}.task-grid>div{min-width:0}dt{font-size:12px;color:var(--muted)}dd{margin:0;font-weight:650;overflow-wrap:anywhere}.time-guidance{background:#eaf4ef;border-left:4px solid var(--accent);padding:10px 14px}.step{display:grid;grid-template-columns:42px minmax(0,1fr);gap:16px;background:var(--paper);border:1px solid var(--line);border-radius:9px;padding:22px;margin:16px 0}.workflow-step[hidden]{display:none!important}.workflow-tabs{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}.workflow-tabs .active{background:var(--accent);color:#fff}.step-number{width:34px;height:34px;display:grid;place-items:center;border:1px solid #aac4b5;border-radius:50%;color:var(--accent);font-weight:750}.step-body{min-width:0}.source{border-top:1px solid var(--line);padding:10px 0}pre{white-space:pre-wrap;word-break:break-word;background:#f7f9f7;border-radius:5px;padding:12px;font:13px/1.7 ui-monospace,SFMono-Regular,Consolas,monospace}details summary{cursor:pointer;font-weight:650}.source-clocks{font-size:13px;color:var(--muted)}.researcher-only{border:3px solid #c56d13;background:#fffaf2}.research-warning{background:#fff0d8;color:#783b05;padding:12px 16px;border-radius:6px;font-size:17px}.research-warning p{font-size:14px;margin:4px 0}.mask-step{background:#fffdf7}.mask-reason{border-left:4px solid #c58a2d;background:#fff4dd;padding:8px 14px}.mask-reason p{margin:4px 0}.neighbor-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.neighbor,.relation{border:1px solid var(--line);border-radius:7px;padding:12px;margin:8px 0}.checkpoint-navigation{margin:14px 0}.checkpoint-nav{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:6px 0}.navigation-note{margin:4px 0 10px}.action-form{display:grid;gap:12px}.action-form>label,.action-row label{display:grid;gap:4px;font-weight:650}.action-form textarea{min-height:72px;resize:vertical}.action-row{border:1px solid var(--line);border-radius:7px;padding:12px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.action-row label:has(textarea),.action-row .remove-action{grid-column:1/-1}.saved{color:var(--accent);margin-left:10px}.empty{color:var(--muted)}#download{color:var(--accent)}@media(max-width:720px){.task-grid,.neighbor-grid,.action-row{grid-template-columns:1fr}.step{grid-template-columns:30px minmax(0,1fr);gap:8px;padding:14px}.bar{position:static}.checkpoint-nav{flex-wrap:wrap}}
.state-delta{border:1px solid #aac4b5;background:var(--soft);border-radius:7px;padding:10px 14px;margin:10px 0}.state-delta p{margin:5px 0}.day-source-group{border:1px solid var(--line);border-radius:8px;margin:10px 0;background:#fbfcfb}.day-source-group>summary{padding:11px 14px;background:var(--soft);border-radius:7px;color:var(--accent)}.day-source-group>.source{margin:0 14px}.candidate-actions{border-top:1px solid var(--line);margin-top:14px;padding-top:8px}.candidate-action{background:var(--soft);border:1px solid var(--line);border-radius:7px;padding:8px 12px;margin:8px 0}.candidate-action h4{margin:4px 0}.candidate-action-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.candidate-action-grid dd{overflow-wrap:anywhere}.source-role{display:inline-block;margin:8px 0 0;padding:1px 8px;border-radius:4px;background:var(--soft);color:var(--accent);font-size:12px;font-weight:750}.target-source{border-left:4px solid var(--accent);padding-left:12px}.context-source{border-left:4px solid #9aaba1;padding-left:12px}.technical-failure{background:#fff0d8;border:2px solid #c56d13;border-radius:6px;color:#783b05;padding:12px 16px;font-weight:700}@media(max-width:720px){.candidate-action-grid{grid-template-columns:1fr}}
"""


_SCRIPT = r"""
const DATA=__DATA__;
const caseId=__CASE_ID__,manifestSha=__MANIFEST_SHA__;
const byId=new Map(DATA.map(p=>[p.checkpoint_id,p]));
const kindSelect=document.querySelector('#kind'),cpSelect=document.querySelector('#checkpoint');
let current=null;
function fillKinds(){
 for(const kind of [...new Set(DATA.map(p=>p.kind||'未分类'))].sort()){
  const option=document.createElement('option');option.value=kind;option.textContent=kind;kindSelect.append(option);
 }
}
function fillCheckpoints(kind,selected){
 cpSelect.replaceChildren();
 for(const p of DATA.filter(p=>!kind||p.kind===kind||(kind==='未分类'&&!p.kind))){
  const option=document.createElement('option');option.value=p.checkpoint_id;
  option.textContent=`${p.display_order.index}/${p.display_order.total} · ${p.title} · ${p.checkpoint_id}`;
  cpSelect.append(option);
 }
 if(selected&&byId.has(selected))cpSelect.value=selected;
}
function go(id,updateHash=true){
 const packet=byId.get(id);if(!packet)return;current=packet;
 kindSelect.value=packet.kind||'未分类';fillCheckpoints(kindSelect.value,id);
 document.querySelector('#material').innerHTML=packet.html;
 document.querySelector('#position').textContent=`当前 ${packet.display_order.index} / ${DATA.length}；一次显示一个候选 Checkpoint；未请求网络`;
 bindPacket();restore(id);
 if(updateHash)history.replaceState(null,'','#'+encodeURIComponent(id));
 window.scrollTo({top:0,behavior:'instant'});
}
function bindPacket(){
 for(const button of document.querySelectorAll('[data-workflow]'))button.addEventListener('click',()=>{
  for(const panel of document.querySelectorAll('.workflow-step'))panel.hidden=panel.id!=='workflow-'+button.dataset.workflow;
  for(const tab of document.querySelectorAll('[data-workflow]'))tab.classList.toggle('active',tab===button);
 });
 for(const button of document.querySelectorAll('[data-goto]'))button.addEventListener('click',()=>go(button.dataset.goto));
 document.querySelector('#add-action').addEventListener('click',()=>{
  const rows=document.querySelector('.actions'),first=rows.querySelector('.action-row'),row=first.cloneNode(true);
  for(const field of row.querySelectorAll('input,textarea'))field.value='';rows.append(row);bindRemove(row);
 });
 for(const row of document.querySelectorAll('.action-row'))bindRemove(row);
 document.querySelector('.action-form').addEventListener('submit',save);
}
function bindRemove(row){
 row.querySelector('.remove-action').addEventListener('click',()=>{
  const rows=document.querySelector('.actions');if(rows.children.length>1)row.remove();
  else for(const field of row.querySelectorAll('input,textarea'))field.value='';
 });
}
function lines(value){return value.split('\n').map(x=>x.trim()).filter(Boolean);}
function response(form){
 const actions=[...form.querySelectorAll('.action-row')].map(row=>({
  action_type:row.querySelector('[name="action_type"]').value,
  target:row.querySelector('[name="target"]').value,
  timing:row.querySelector('[name="timing"]').value,
  conditions:lines(row.querySelector('[name="conditions"]').value),
 }));
 return {checkpoint_id:current.checkpoint_id,case_id:caseId,material_manifest_sha256:manifestSha,
  form_version:__FORM_VERSION__,material_format:__HTML_FORMAT__,reviewer_status:'draft_not_clinical_reference',
  mode:form.querySelector('[name="mode"]').value,actions,
  rationale:form.querySelector('[name="rationale"]').value,
  evidence_to_obtain:lines(form.querySelector('[name="evidence_to_obtain"]').value)};
}
function key(id){return 'candidate-action-draft-'+caseId+'-'+id;}
function save(event){
 event.preventDefault();const data=response(event.currentTarget),status=event.currentTarget.querySelector('.saved');
 try{localStorage.setItem(key(current.checkpoint_id),JSON.stringify(data));status.textContent='已保存本机草稿；导出后方可交回台账';}
 catch{status.textContent='本机存储不可用，请直接导出草稿';}
}
function restore(id){
 let old=null;try{old=JSON.parse(localStorage.getItem(key(id)));}catch{}if(!old)return;
 const form=document.querySelector('.action-form');form.querySelector('[name="mode"]').value=old.mode||'';
 form.querySelector('[name="rationale"]').value=old.rationale||'';
 form.querySelector('[name="evidence_to_obtain"]').value=(old.evidence_to_obtain||[]).join('\n');
 const rows=document.querySelector('.actions');
 while((old.actions||[]).length>rows.children.length){document.querySelector('#add-action').click();}
 [...rows.querySelectorAll('.action-row')].forEach((row,index)=>{
  const action=(old.actions||[])[index]||{};
  for(const name of ['action_type','target','timing'])row.querySelector(`[name="${name}"]`).value=action[name]||'';
  row.querySelector('[name="conditions"]').value=(action.conditions||[]).join('\n');
 });
}
kindSelect.addEventListener('change',()=>{fillCheckpoints(kindSelect.value);go(cpSelect.value);});
cpSelect.addEventListener('change',()=>go(cpSelect.value));
document.querySelector('#export').addEventListener('click',()=>{
 const responses=[];for(const packet of DATA){try{const old=JSON.parse(localStorage.getItem(key(packet.checkpoint_id)));if(old)responses.push(old);}catch{}}
 const form=document.querySelector('.action-form');if(form){
  if(!form.checkValidity()){form.reportValidity();return;}
  const fresh=response(form),index=responses.findIndex(x=>x.checkpoint_id===fresh.checkpoint_id);if(index>=0)responses[index]=fresh;else responses.push(fresh);
 }
 const output={format:__RESPONSE_FORMAT__,case_id:caseId,material_manifest_sha256:manifestSha,renderer_format:__HTML_FORMAT__,responses};
 const blob=new Blob([JSON.stringify(output,null,2)],{type:'application/json'}),link=document.querySelector('#download');
 if(link.href)URL.revokeObjectURL(link.href);link.href=URL.createObjectURL(blob);link.download='candidate-action-drafts.json';link.textContent='草稿已生成，点击下载';link.hidden=false;
});
fillKinds();const hash=decodeURIComponent(location.hash.slice(1));go(byId.has(hash)?hash:DATA[0].checkpoint_id,false);
"""


def render_html(material, manifest_sha):
    """Render all packets as a private, one-Checkpoint-at-a-time offline page."""
    packets = sorted(material.get("packets") or [],
                     key=lambda packet: packet["display_order"]["index"])
    if not packets:
        raise ValueError("MATERIAL_SET_HAS_NO_CANDIDATES")
    payload = []
    for index, source_packet in enumerate(packets):
        packet = dict(source_packet)
        packet["sequence_navigation"] = packet.get("sequence_navigation") or dict(
            index=index + 1,
            total=len(packets),
            previous_checkpoint_id=packets[index - 1]["checkpoint_id"] if index else None,
            next_checkpoint_id=(packets[index + 1]["checkpoint_id"]
                                if index + 1 < len(packets) else None),
        )
        payload.append({
            "checkpoint_id": packet["checkpoint_id"],
            "kind": packet.get("kind") or "未分类",
            "title": (packet.get("target") or {}).get("title") or "通用管理任务",
            "episode_id": packet.get("episode_id"),
            "display_order": packet["display_order"],
            "sequence_navigation": packet["sequence_navigation"],
            "html": _packet_html(packet),
        })
    script = _SCRIPT.replace("__DATA__", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
    for token, value in [
        ("__CASE_ID__", material["case_id"]),
        ("__MANIFEST_SHA__", manifest_sha),
        ("__FORM_VERSION__", FORM_VERSION),
        ("__HTML_FORMAT__", HTML_FORMAT),
        ("__RESPONSE_FORMAT__", RESPONSE_FORMAT),
    ]:
        script = script.replace(token, json.dumps(value, ensure_ascii=False))
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>候选 Checkpoint 私有研究审核</title><style>" + _STYLE + "</style></head>"
        "<body data-manifest='" + _h(manifest_sha) + "'><main>"
        "<h1>候选 Checkpoint 私有研究材料</h1><p class='small'>"
        + _h(material["case_id"]) + " · " + _h(material.get("format") or "未标格式")
        + " · 单文件离线 · 一次显示一个 Checkpoint</p>"
        "<p class='small private-note'>私有研究审核：含未遮原文，不是医生盲评文件。</p>"
        "<div class='bar'><label for='kind'>按任务类型筛选</label>"
        "<select id='kind' aria-label='按 kind 筛选 Checkpoint'></select>"
        "<label for='checkpoint'>当前 Checkpoint</label><select id='checkpoint'></select>"
        "<button id='export' type='button'>导出动作计划草稿</button><a id='download' hidden></a></div>"
        "<div id='position' class='small'></div><div id='material'></div>"
        "</main><script>" + script + "</script></body></html>"
    )


def write_html(material, manifest, output):
    output = Path(output)
    page = render_html(material, manifest["manifest_sha256"])
    (output / "index.html").write_text(page, encoding="utf-8")
    receipt = dict(
        output=str(output / "index.html"), candidates=len(material["packets"]),
        manifest_sha256=manifest["manifest_sha256"], html_format=HTML_FORMAT,
        html_sha256=hashlib.sha256(page.encode("utf-8")).hexdigest(),
        provider_requests=0, doctor_release=False,
        private_research_preview=True, network_calls=0,
    )
    (output / "render-receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return receipt
