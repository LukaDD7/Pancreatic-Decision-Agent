"""Offline, text-only HTML for one-at-a-time candidate material inspection.

This is a researcher preview, not a clinician distribution. A future clinician
renderer must use a separately derived permitted packet rather than merely hide
parts of this page with CSS. No network, external libraries or provider calls.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from .materials import FORM_VERSION, HTML_FORMAT, RESPONSE_FORMAT, digest


def _h(value):
    return html.escape("" if value is None else str(value), quote=True)


def _node(tag, attributes, *children):
    attrs = "".join(f' {key}="{_h(value)}"' for key, value in attributes.items()
                    if value is not None)
    return f"<{tag}{attrs}>{''.join(children)}</{tag}>"


def _pre(content, attributes=None):
    return _node("pre", attributes or {}, _h(content))


def _source_clock(source):
    clock = source.get("clock") or {}
    return clock.get("value"), clock.get("precision")


def _source_card(source, *, target_source_id):
    is_target = source["source_id"] == target_source_id
    flags = []
    if is_target:
        flags.append("当前候选所在文书 · 候选定位原文已遮蔽")
    if source.get("masked_candidate_count"):
        flags.append(f"已定位候选遮蔽 {source['masked_candidate_count']} 处")
    value, precision = _source_clock(source)
    if value is None:
        flags.append("检索时钟未知；不是已确认的临床选择时刻")
    clocks = []
    for name, item in (source.get("clocks") or {}).items():
        clocks.append(_node("li", {}, _h(f"{name}: {item.get('value') or '未知'}")))
    time_details = _node("details", {"class": "clock-details"},
                         _node("summary", {}, "查看来源时间线索"),
                         _node("ul", {}, *clocks)) if clocks else ""
    return _node("section", {"class": "source", "data-kind": source.get("kind")},
                 _node("h4", {}, _h(source.get("title") or "无标题来源")),
                 _node("div", {"class": "meta"},
                       _h(f"来源 {source['source_id']} · 文书检索时钟 {value or '未知'}"
                          f"（{precision or '未知精度'}）")),
                 _node("div", {"class": "flags"}, *[
                     _node("span", {"class": "flag"}, _h(item)) for item in flags]),
                 time_details, _pre(source.get("text") or ""))


def _day_groups(packet):
    groups = {}
    for source in packet["included_sources"]:
        value, _ = _source_clock(source)
        groups.setdefault(value[:10] if value else "检索日期未知", []).append(source)
    return [_node("details", {"class": "day"},
                  _node("summary", {}, _h(f"{day} · {len(items)} 份来源")),
                  *[_source_card(source, target_source_id=packet["target"]["source_id"])
                    for source in items])
            for day, items in sorted(groups.items())]


def _context_excerpt(packet):
    target_source = next((source for source in packet["included_sources"]
                          if source["source_id"] == packet["target"]["source_id"]), None)
    if not target_source:
        return None
    text = target_source.get("text") or ""
    span = packet["researcher_candidate_inspection"].get("span") or {}
    quote = span.get("quote") or ""
    marker = "[当前候选定位原文已遮蔽]"
    marker_at = text.find(marker)
    if marker_at >= 0 and quote:
        original_at, masked_at, masked_length = marker_at, marker_at, len(marker)
    elif span.get("start") is not None and span.get("end") is not None:
        original_at = masked_at = span["start"]
        masked_length = span["end"] - span["start"]
    else:
        return None
    start = max(0, original_at - 180)
    end = min(len(text), masked_at + masked_length + 180)
    prefix = ("… " if start else "") + text[start:original_at]
    suffix = text[masked_at + masked_length:end] + (" …" if end < len(text) else "")
    original = _node("pre", {"class": "decision-context"}, _h(prefix),
                     _node("mark", {}, _h(quote)), _h(suffix))
    masked_text = marker if marker_at >= 0 else quote
    masked = _node("pre", {"class": "decision-context masked-context"}, _h(prefix),
                   _node("span", {"class": "masked-text"}, _h(masked_text)), _h(suffix))
    return original, masked


def _neighbor_card(packet, packets_by_id, neighbor_id, direction):
    label = "前一候选" if direction == "previous" else "后一候选"
    if not neighbor_id:
        return _node("article", {"class": "neighbor-card empty-neighbor"},
                     _node("div", {"class": "step-kicker"}, label + "（显示顺序）"),
                     _node("p", {"class": "empty"}, "本 Episode 内没有" + label + "。"))
    neighbor = packets_by_id[neighbor_id]
    inspection = neighbor["researcher_candidate_inspection"]
    related = next((item for item in packet["related_candidates"]
                    if item["checkpoint_id"] == neighbor_id), None)
    relation_status = "Agent 已记录直接关系" if related else "未记录直接关系"
    relation_class = "relation-present" if related else "relation-absent"
    children = [
        _node("div", {"class": "step-kicker"}, label + "（显示顺序，不代表临床先后）"),
        _node("h4", {}, _h(neighbor["target"].get("source_title") or "无标题来源")),
        _node("div", {"class": "meta"},
              _h(f"{neighbor_id} · {neighbor['target']['clock'].get('value') or '时点未知'}")),
        _node("p", {"class": "neighbor-quote"}, _h((inspection.get("span") or {}).get("quote") or "未提供候选原文")),
        _node("span", {"class": "relation-status " + relation_class}, relation_status)]
    if related:
        relation = related["relation"]
        children.extend([
            _node("p", {"class": "relation-summary"},
                  _node("strong", {}, _h(relation.get("relation") or "未分类")),
                  _node("span", {"class": "tag"}, _h(relation.get("confidence") or "未标置信度"))),
            _node("p", {}, _node("strong", {}, "Agent 连接理由："),
                  _h(relation.get("explanation") or "未提供")),
            _node("details", {}, _node("summary", {}, "查看关系两端引文"),
                  _pre(json.dumps(relation.get("basis_quotes") or [], ensure_ascii=False, indent=2)))])
    else:
        children.append(_node("p", {"class": "small"},
                              "仅表示这两个相邻显示候选之间没有已记录的直接边；不等于临床上无关系，也不否定其他间接关系。"))
    children.append(_node("button", {"type": "button", "class": "secondary", "data-goto": neighbor_id},
                          "打开" + label))
    return _node("article", {"class": "neighbor-card"}, *children)


def _material(packet, packets_by_id):
    target = packet["target"]
    inspection = packet["researcher_candidate_inspection"]
    clock = target.get("clock") or {}
    clock_known = bool(clock.get("value"))
    if clock_known:
        blinding = "候选定位原文已遮蔽；检索时钟不等于临床选择时刻。"
    else:
        blinding = "检索时钟未知：仅保留当前原文供时点核对；这不是已完成的盲评材料。"
    candidate_span = inspection.get("span") or {}
    quote = candidate_span.get("quote") or ""
    source_title = target.get("source_title") or candidate_span.get("source_id")
    summary = _node("section", {"class": "target-summary", "aria-label": "当前候选说明"},
        _node("div", {"class": "target-actions"},
              _node("button", {"type": "button", "class": "secondary", "id": "show-candidate-original"},
                    "定位 Agent 选定原文"),
              _node("span", {"class": "small"}, "研究审核专用 · 原文与遮蔽投影分层")),
        _node("div", {"class": "eyebrow"}, "当前候选 Checkpoint（尚未经专家确认）"),
        _node("h2", {"id": "current-title"}, _h(packet["checkpoint_id"])),
        _node("div", {"class": "target-grid"},
            _node("div", {}, _node("dt", {}, "候选 Episode"),
                  _node("dd", {}, _h(packet["episode_id"] or "未分配"))),
            _node("div", {}, _node("dt", {}, "发现阶段 LLM 的文书功能分类"),
                  _node("dd", {}, _h(inspection.get("record_function") or "未分类"))),
            _node("div", {}, _node("dt", {}, "所在文书"),
                  _node("dd", {}, _h(source_title))),
            _node("div", {}, _node("dt", {}, "文书检索时钟（不是临床选择时刻）"),
                  _node("dd", {}, _h(clock.get("value") or "未知")))),
        _node("details", {},
              _node("summary", {}, "查看候选 Episode 的未决问题与成员"),
              _node("ul", {}, *[_node("li", {}, _h(item)) for item in
                                 packet["candidate_episode"]["open_questions"]]),
              _node("p", {"class": "small"},
                    _h("程序候选成员：" + ", ".join(packet["candidate_episode"]["event_ids"])))))
    context = _context_excerpt(packet)
    original_context = context[0] if context else _pre(quote)
    masked_context = context[1] if context and clock_known else _node("p", {"class": "warning"}, _h(blinding))
    target_source = next((source for source in packet["included_sources"]
                          if source["source_id"] == target["source_id"]), {})
    mask_count = target_source.get("masked_candidate_count") or 0
    if clock_known:
        mask_reason = ("避免把当前候选所定位的管理判断直接提供给后续评审；程序仅替换已定位的当前及未来候选区间。"
                       "这是开发阶段投影，不是已冻结的完整防泄漏方案。")
    else:
        mask_reason = "检索时钟未知，当前未执行目标候选遮蔽；先核对来源时点，不将本页称为已完成盲评材料。"
    increment = _node("section", {"class": "panel increment-panel", "id": "panel-increment"},
        _node("div", {"class": "increment-heading"},
              _node("div", {}, _node("div", {"class": "eyebrow"}, "DEFAULT VIEW · 本节点新增信息"),
                    _node("h3", {}, "这一 Checkpoint 多了什么？")),
              _node("span", {"class": "increment-index"},
                    _h(f"{packet['episode_id'] or '未分配 Episode'} · {packet['episode_navigation']['index']} / {packet['episode_navigation']['total']}"))),
        _node("p", {"class": "small"},
              "按“患者原文 → Agent 决策定位 → 拟遮蔽 → 前后连接”阅读。所有判断均为候选提案，不是专家确认的临床决策。"),
        _node("section", {"class": "increment-step"},
              _node("div", {"class": "step-number"}, "01"),
              _node("div", {"class": "step-content"},
                    _node("div", {"class": "step-kicker"}, "患者在这个 Checkpoint 的原始文书"),
                    _node("h3", {}, _h(source_title)),
                    _node("p", {"class": "meta"},
                          _h(f"来源 {target['source_id']} · 文书检索时钟 {clock.get('value') or '未知'}")),
                    original_context,
                    _node("p", {"class": "small"}, "黄色为 Agent 选定的连续原文；保留其前后各约 180 字作为定位上下文。其他已定位的未来候选仍保持遮蔽，未改写其他文书内容。"))),
        _node("section", {"class": "increment-step"},
              _node("div", {"class": "step-number"}, "02"),
              _node("div", {"class": "step-content"},
                    _node("div", {"class": "step-kicker"}, "Agent 把什么提议为管理判断候选"),
                    _node("blockquote", {"class": "selected-quote"}, _h(quote)),
                    _node("p", {}, _node("strong", {}, "文书表述功能分类："),
                          _node("span", {"class": "function-tag"}, _h(inspection.get("record_function") or "未分类")),
                          _node("span", {"class": "tag"}, _h(f"程序定位 {inspection.get('status') or '未知'}"))),
                    _node("p", {"class": "agent-rationale"}, _node("strong", {}, "Agent 选定理由："),
                          _h(inspection.get("rationale") or "未提供")),
                    _node("details", {"class": "researcher-only"},
                          _node("summary", {}, "查看 Agent 提议的候选定位原文与字符边界（研究审核专用）"),
                          _pre(quote),
                          _node("p", {"class": "small"},
                                _h(f"主体：发现阶段 LLM 提议；程序核验：{inspection.get('status')}。"
                                   f"字符区间 [{candidate_span.get('start')}, {candidate_span.get('end')})；"
                                   "定位成功不等于临床语义正确。"))))),
        _node("section", {"class": "increment-step mask-step"},
              _node("div", {"class": "step-number"}, "03"),
              _node("div", {"class": "step-content"},
                    _node("div", {"class": "step-kicker"}, "拟 Mask 什么，为什么"),
                    _node("h3", {}, "遮蔽前后对照"),
                    _node("div", {"class": "compare-grid"},
                          _node("div", {}, _node("div", {"class": "compare-label"}, "当前候选原文定位（其他候选仍按本节点遮蔽）"), original_context),
                          _node("div", {}, _node("div", {"class": "compare-label"}, "提供给后续评审的遮蔽投影"), masked_context)),
                    _node("div", {"class": "mask-reason"},
                          _node("strong", {}, "遮蔽范围与理由"),
                          _node("p", {}, _h(mask_reason)),
                          _node("p", {"class": "small"},
                                _h(f"当前候选：{quote}（{len(quote)} 字）。本份文书共标记 {mask_count} 处已定位候选遮蔽；"
                                   "邻近解释、结果及其他未来信息尚未经专家核验。"))))),
        _node("section", {"class": "increment-step relation-step"},
              _node("div", {"class": "step-number"}, "04"),
              _node("div", {"class": "step-content"},
                    _node("div", {"class": "step-kicker"}, "Agent 是否连接了前后节点"),
                    _node("h3", {}, "相邻候选关系"),
                    _node("div", {"class": "neighbor-grid"},
                          _neighbor_card(packet, packets_by_id,
                                         packet["episode_navigation"].get("previous_checkpoint_id"), "previous"),
                          _neighbor_card(packet, packets_by_id,
                                         packet["episode_navigation"].get("next_checkpoint_id"), "next")))))
    tabs = _node("nav", {"class": "tabs", "aria-label": "材料分层"},
        _node("button", {"type": "button", "data-tab": "increment", "class": "active"},
              "本节点增量"),
        _node("button", {"type": "button", "data-tab": "target"},
              "目标文书（完整遮蔽投影）"),
        _node("button", {"type": "button", "data-tab": "timeline"},
              f"时点内完整资料（{len(packet['included_sources'])} 份）"),
        _node("button", {"type": "button", "data-tab": "relations"},
              f"Agent 关系（{len(packet['related_candidates'])} 条）"),
        _node("button", {"type": "button", "data-tab": "evidence"},
              f"已记录证据引用（{len(packet['evidence_used'])} 条）"))
    if clock_known:
        target_sources = [s for s in packet["included_sources"]
                          if s["source_id"] == target["source_id"]]
        target_body = "".join(_source_card(s, target_source_id=target["source_id"])
                              for s in target_sources)
        if not target_body:
            target_body = _node("p", {"class": "warning"},
                                "当前来源未进入按时钟纳入集合；请核对来源时钟。")
        target_explanation = ("当前候选所在文书的完整正文投影；只替换已定位候选区间。"
                              "不是全患者病历，也不等于临床团队当时实际阅读的内容。")
    else:
        target_sources = list(packet["included_sources"])
        target_body = "".join(_source_card(s, target_source_id=target["source_id"])
                              for s in target_sources)
        target_explanation = ("时点未知，当前仅呈现目标原文及来源时间线索，供核对时点；"
                              "未声称完成时点隔离或遮蔽。")
    panels = [increment,
        _node("section", {"class": "panel", "id": "panel-target", "hidden": ""},
        _node("h3", {}, "目标文书与候选遮蔽"),
        _node("p", {"class": "small"}, _h(target_explanation)), target_body),
        _node("section", {"class": "panel", "id": "panel-timeline", "hidden": ""},
        _node("h3", {}, "按时点纳入的完整来源"),
        _node("div", {"class": "toolbar"},
              _node("input", {"id": "source-filter", "type": "search",
                              "placeholder": "筛选标题、来源 ID 或原文",
                              "aria-label": "筛选时点内完整资料"}),
              _node("span", {"id": "source-count", "class": "small"})),
        _node("p", {"class": "small"},
              _h("按文书检索时钟筛入，按日期折叠；并非 Agent 已使用的证据。"
                 f"未纳入 {len(packet['excluded_sources'])} 份来源（晚于时点或时间待核）。")),
        _node("div", {"id": "source-list"}, *_day_groups(packet))),
        _node("section", {"class": "panel", "id": "panel-relations", "hidden": ""},
        _node("h3", {}, "Agent 提议、程序核验引文的关系"),
        _node("p", {"class": "small"},
              "点击相关候选可直接切换并比较。关系是 Agent 提议，不是已确认临床先后、因果或 Episode 边界。"),
        *[_node("article", {"class": "relation"},
          _node("div", {"class": "relation-title"},
                _node("span", {"class": "relation-kind"}, _h(item["relation"]["relation"])),
                _node("span", {"class": "tag"}, _h(item["relation"].get("confidence")))),
          _node("button", {"type": "button", "class": "candidate-link secondary",
                           "data-goto": item["checkpoint_id"]},
                _h(item["checkpoint_id"])),
          _node("p", {}, _h(f"{item.get('source_title') or '无标题来源'} · "
                            f"检索时钟 {item.get('clock', {}).get('value') or '未知'} · "
                            f"LLM文书分类 {item.get('record_function') or '未分类'}")),
          _pre(item["span"].get("quote") or ""),
          _node("p", {}, _node("strong", {}, "Agent 连接理由："),
                _h(item["relation"].get("explanation") or "未提供")),
          _node("details", {}, _node("summary", {}, "查看该关系的两端引文与方向"),
                _pre(json.dumps(item["relation"], ensure_ascii=False, indent=2))))
          for item in packet["related_candidates"]] or [
              _node("p", {"class": "empty"}, "本候选没有已记录的管理候选关系。")]),
        _node("section", {"class": "panel", "id": "panel-evidence", "hidden": ""},
        _node("h3", {}, "Agent 记录的既往证据引用"),
        _node("p", {"class": "small"}, _h(packet["evidence_use_note"])),
        *[_node("article", {"class": "relation"},
          _pre(json.dumps(item, ensure_ascii=False, indent=2)))
          for item in packet["evidence_used"]] or [
              _node("p", {"class": "empty"}, "本候选没有已记录的证据引用；这不等于没有相关临床证据。")])]
    navigation = packet["episode_navigation"]
    previous_id = navigation.get("previous_checkpoint_id")
    next_id = navigation.get("next_checkpoint_id")
    def nav_button(identifier, direction):
        return _node("button", {"type": "button", "class": direction,
                                "data-goto": identifier, "disabled": "disabled" if not identifier else None},
                     ("← 上一候选" if direction == "previous" else "下一候选 →"))
    nav = _node("div", {"class": "checkpoint-nav"}, nav_button(previous_id, "previous"),
        _node("span", {"class": "small"},
              _h(f"Episode 内候选 {navigation['index']} / {navigation['total']} · "
                 "切换顺序不等于临床决策顺序")),
        nav_button(next_id, "next"))
    form = _node("form", {"class": "review", "data-checkpoint": packet["checkpoint_id"]},
        _node("h3", {}, "评审草稿（未形成临床 Reference）"),
        _node("label", {}, "候选 Checkpoint 判断",
              _node("select", {"name": "checkpoint_judgment"},
                    *[_node("option", {"value": value}, _h(label)) for value, label in [
                        ("", "未填写"), ("independent", "独立候选 Checkpoint"),
                        ("process_part", "属于过程的一部分"),
                        ("duplicate", "重复/已包含"), ("uncertain", "无法判断")]])),
        _node("label", {}, "可接受 Mode 集合（由医生填写）",
              _node("textarea", {"name": "acceptable_modes",
                                 "placeholder": "由研究方案和医生填写；此预览不预设答案"})),
        _node("label", {}, "可接受动作与条件",
              _node("textarea", {"name": "acceptable_actions"})),
        _node("label", {}, "证据演化与原文依据",
              _node("textarea", {"name": "evidence_evolution"})),
        _node("label", {}, "备注", _node("textarea", {"name": "note"})),
        _node("div", {},
              _node("button", {"type": "submit"}, "保存本机草稿"),
              _node("span", {"class": "saved", "aria-live": "polite"})))
    return summary + nav + tabs + "".join(panels) + nav + form


_STYLE = r"""
:root{color-scheme:light;--bg:#f4f6f5;--ink:#17231d;--muted:#5b6b62;--line:#d4dfd8;--accent:#17654c;--paper:#fff;--warn:#855913;--soft:#edf3ef}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}main{max-width:1240px;margin:auto;padding:26px 18px 70px}h1{font-size:26px;margin:0 0 8px}h2{font-size:24px;margin:4px 0 16px}h3{font-size:18px;margin:20px 0 10px}h4{margin:14px 0 2px}.notice{background:#fff7e4;border:1px solid #e4c985;padding:15px;border-radius:8px;margin:18px 0}.bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:20px 0;position:sticky;top:0;background:var(--bg);padding:12px 0;z-index:4;border-bottom:1px solid var(--line)}select,textarea,button,input{font:inherit}select,input{padding:8px;max-width:100%;min-width:0}.tabs{max-width:100%;min-width:0}button{cursor:pointer;background:var(--accent);color:#fff;border:0;border-radius:5px;padding:9px 15px}button:disabled{background:#c3cdc7;cursor:not-allowed}.secondary{background:var(--soft);color:var(--accent);border:1px solid #adc4b7}.small{font-size:13px;color:var(--muted)}.meta{font-size:12px;color:var(--muted)}.eyebrow{font-weight:650;color:var(--accent)}.target-summary{background:var(--paper);border:1px solid var(--line);border-radius:9px;padding:20px;margin:18px 0}.target-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:10px 0}.target-actions button{font-size:13px;padding:7px 10px}.target-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}dt{font-size:12px;color:var(--muted)}dd{margin:2px 0;font-weight:600}.warning{color:var(--warn);background:#fff9ec;padding:10px;border-radius:5px}.researcher-only{border:1px solid #c99642;background:#fffaf0;border-radius:6px;padding:10px 14px;margin:12px 0}details>summary{cursor:pointer;font-weight:600;padding:8px 0}pre{white-space:pre-wrap;word-break:break-word;font:13px/1.7 ui-monospace,SFMono-Regular,Consolas,monospace;background:#f7f9f7;padding:12px;border-radius:5px}.checkpoint-nav{display:flex;gap:12px;justify-content:space-between;align-items:center;margin:16px 0;flex-wrap:wrap}.tabs{display:flex;gap:6px;overflow:auto;padding:6px 0}.tabs button{white-space:nowrap;background:var(--soft);color:var(--ink)}.tabs button.active{background:var(--accent);color:#fff}.panel{background:var(--paper);border:1px solid var(--line);border-radius:9px;padding:16px;margin:14px 0}.source{border-top:1px solid var(--line);padding-bottom:10px}.flag,.tag{display:inline-block;color:var(--muted);background:#edf1ee;border-radius:4px;padding:1px 7px;margin:2px}.clock-details{font-size:13px}.toolbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.toolbar input{min-width:260px;flex:1;border:1px solid var(--line);border-radius:5px}.relation{border-top:1px solid var(--line);padding:10px 0}.relation-title{display:flex;gap:10px}.relation-kind{font-weight:650;color:var(--accent)}.candidate-link{font-family:monospace;font-weight:650}.review{display:grid;gap:12px;background:var(--paper);border:1px solid var(--line);border-radius:9px;padding:18px;margin-top:16px}.review label{display:grid;gap:4px;font-weight:600}.review textarea{min-height:70px;padding:8px;border:1px solid var(--line);border-radius:5px;resize:vertical}.saved{color:var(--accent);margin-left:10px}.empty{color:var(--muted);padding:20px}#export{margin-top:0}#download{color:var(--accent)}.increment-panel{padding:0;overflow:hidden}.increment-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;padding:22px 24px 0}.increment-heading h3{font-size:25px;margin:3px 0 8px}.increment-panel>.small{padding:0 24px 18px;margin:0;border-bottom:1px solid var(--line)}.increment-index{font-size:12px;font-weight:700;color:var(--accent);background:var(--soft);padding:5px 9px;border-radius:20px;white-space:nowrap}.increment-step{display:grid;grid-template-columns:48px minmax(0,1fr);gap:16px;padding:24px;border-bottom:1px solid var(--line)}.increment-step:last-child{border-bottom:0}.step-number{font:700 13px/1 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--accent);border:1px solid #adc4b7;border-radius:50%;width:36px;height:36px;display:grid;place-items:center;margin-top:2px}.step-content{min-width:0}.step-content h3{margin:2px 0 8px;font-size:20px}.step-kicker{font-size:12px;font-weight:750;letter-spacing:.04em;color:var(--accent);text-transform:uppercase}.decision-context{margin:12px 0 6px;max-height:270px;overflow:auto;border:1px solid var(--line);background:#fbfcfb;font:14px/1.85 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}.decision-context mark{background:#fff0a8;color:#382e05;padding:3px 1px;border-radius:2px;font-weight:700}.selected-quote{margin:14px 0;padding:15px 18px;border-left:4px solid var(--accent);background:var(--soft);font-size:18px;font-weight:650;line-height:1.7}.function-tag{display:inline-block;background:#dcebe3;color:#124d3a;border-radius:4px;padding:2px 8px;margin-left:5px;font-weight:700}.agent-rationale{background:#f7f9f7;padding:12px 14px;border-radius:6px}.mask-step{background:#fffdf7}.compare-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.compare-grid>div{min-width:0}.compare-label{font-size:12px;font-weight:700;color:var(--muted);margin:8px 0 2px}.masked-context{background:#fff9ec;border-color:#e4c985}.masked-text{display:inline-block;background:#303832;color:#fff;padding:2px 5px;border-radius:3px;font-weight:650}.mask-reason{border-left:3px solid #c99642;background:#fff8e8;padding:10px 14px;margin-top:12px}.mask-reason p{margin:5px 0}.neighbor-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:14px}.neighbor-card{border:1px solid var(--line);border-radius:8px;padding:16px;background:#fbfcfb;min-width:0}.neighbor-card h4{font-size:17px;margin:5px 0}.neighbor-quote{font-weight:600;border-bottom:1px solid var(--line);padding-bottom:12px}.relation-status{display:inline-block;font-size:12px;font-weight:750;padding:3px 8px;border-radius:4px}.relation-present{background:#dcebe3;color:#12553d}.relation-absent{background:#f0f1f0;color:#5c625e}.relation-summary{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.empty-neighbor{background:#f7f9f7}.review h3{margin-top:0}@media(max-width:760px){.compare-grid,.neighbor-grid{grid-template-columns:1fr}.increment-step{grid-template-columns:34px minmax(0,1fr);gap:10px;padding:18px 14px}.increment-heading{padding:18px 14px 0}.increment-panel>.small{padding:0 14px 14px}.step-number{width:30px;height:30px}.decision-context{max-height:230px}}@media(max-width:600px){main{padding:16px 12px}.target-summary,.panel{padding:12px}.increment-panel{padding:0}.target-grid{grid-template-columns:1fr}.bar{position:static}.checkpoint-nav button{flex:1}.tabs{margin:0 -4px}.increment-heading{display:block}.increment-index{display:inline-block;margin-top:5px}.selected-quote{font-size:16px}}
"""


_SCRIPT = r"""
const DATA=__DATA__;
const caseId=__CASE_ID__,manifestSha=__MANIFEST_SHA__;
const episodes=__EPISODES__;
const byId=new Map(DATA.map(p=>[p.checkpoint_id,p]));
const selection=document.querySelector('#episode'),cpSelection=document.querySelector('#checkpoint');
let current=null,activeTab='increment';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));function options(episode){
 cpSelection.replaceChildren();
 for(const p of DATA.filter(p=>!episode||p.episode_id===episode)){
  const o=document.createElement('option');o.value=p.checkpoint_id;
  o.textContent=`${p.checkpoint_id} · ${p.target.source_title||'无标题来源'} · ${p.target.clock.value||'时点未知'}`;
  cpSelection.append(o);
 }
}
selection.addEventListener('change',()=>{options(selection.value);go(cpSelection.value)});
cpSelection.addEventListener('change',()=>go(cpSelection.value));
function go(id,push=true){
 const p=byId.get(id);if(!p)return;current=p;
 if(selection.value&&selection.value!==p.episode_id)selection.value=p.episode_id;
 if(!selection.value||selection.value===p.episode_id){options(selection.value);cpSelection.value=id;}
 document.querySelector('#material').innerHTML=p.html;
 document.querySelector('#count').textContent=`当前候选 ${p.display_order.index} / ${DATA.length}；Episode ${p.episode_id||'未分配'}；仅本机预览，未请求网络`;
 bindMaterial();showTab(activeTab);restore(p.checkpoint_id);
 if(push)history.replaceState(null,'','#'+encodeURIComponent(id));
 window.scrollTo({top:0,behavior:'instant'});
}
function showTab(name){
 activeTab=name;
 for(const panel of document.querySelectorAll('.panel'))panel.hidden=panel.id!=='panel-'+name;
 for(const b of document.querySelectorAll('[data-tab]')){const on=b.dataset.tab===name;b.classList.toggle('active',on);b.setAttribute('aria-selected',String(on));}
 if(name==='timeline')filterSources();
}
function bindMaterial(){
 for(const b of document.querySelectorAll('[data-goto]'))b.addEventListener('click',()=>go(b.dataset.goto));
 for(const b of document.querySelectorAll('[data-tab]'))b.addEventListener('click',()=>showTab(b.dataset.tab));
 const filter=document.querySelector('#source-filter');if(filter)filter.addEventListener('input',filterSources);
 const form=document.querySelector('.review');if(form)form.addEventListener('submit',save);
 const reveal=document.querySelector('#show-candidate-original');
 if(reveal)reveal.addEventListener('click',()=>{showTab('increment');const d=document.querySelector('.researcher-only');d.open=true;d.scrollIntoView({block:'nearest'});});
}
function filterSources(){
 const q=(document.querySelector('#source-filter')?.value||'').trim().toLowerCase();let n=0;
 for(const source of document.querySelectorAll('#source-list .source')){
  const show=!q||source.textContent.toLowerCase().includes(q);source.hidden=!show;source.style.display=show?'':'none';if(show)n++;
 }
 for(const day of document.querySelectorAll('#source-list .day')){
  const visible=[...day.querySelectorAll('.source')].filter(s=>!s.hidden).length;day.hidden=!visible;
  const summary=day.querySelector('summary');if(!summary.dataset.day)summary.dataset.day=summary.textContent.split(' · ')[0];
  summary.textContent=`${summary.dataset.day} · ${visible} 份来源${q?` · 筛选 ${visible} 份`:''}`;
 }
 document.querySelector('#source-count').textContent=`显示 ${n} / ${current.included_sources.length} 份来源`;
}
function response(form){
 return {...Object.fromEntries(new FormData(form)),checkpoint_id:current.checkpoint_id,
  candidate_event_id:current.candidate_event_id,episode_id:current.episode_id,
  material_packet_content_sha256:current.content_sha256,form_version:__FORM_VERSION__,
  material_format:current.format,reviewer_status:'draft_not_clinical_reference'};
}
function save(event){
 event.preventDefault();const data=response(event.currentTarget);
 try{localStorage.setItem('candidate-review-'+current.checkpoint_id,JSON.stringify(data));
  event.currentTarget.querySelector('.saved').textContent='已存本机草稿（导出后才可交回台账）';
 }catch{event.currentTarget.querySelector('.saved').textContent='本机存储不可用，请导出草稿';}
}
function restore(id){
 try{const old=JSON.parse(localStorage.getItem('candidate-review-'+id));if(!old)return;
  for(const [k,v] of Object.entries(old)){const el=document.querySelector(`.review [name="${CSS.escape(k)}"]`);if(el)el.value=v;}
 }catch{}
}
document.querySelector('#export').addEventListener('click',()=>{
 const responses=[];
 for(const p of DATA){try{const old=JSON.parse(localStorage.getItem('candidate-review-'+p.checkpoint_id));if(old&&Object.entries(old).some(([k,v])=>!['checkpoint_id','candidate_event_id','episode_id','material_packet_content_sha256','form_version','material_format','reviewer_status'].includes(k)&&String(v).trim()))responses.push(old);}catch{}}
 const form=document.querySelector('.review');if(form){const fresh=response(form);const index=responses.findIndex(x=>x.checkpoint_id===fresh.checkpoint_id);if(index>=0)responses[index]=fresh;else if(Object.values(Object.fromEntries(new FormData(form))).some(v=>v.trim()))responses.push(fresh);}
 const out={format:__RESPONSE_FORMAT__,case_id:caseId,material_manifest_sha256:manifestSha,renderer_format:__HTML_FORMAT__,responses};
 const blob=new Blob([JSON.stringify(out,null,2)],{type:'application/json'}),a=document.querySelector('#download');
 if(a.href)URL.revokeObjectURL(a.href);a.href=URL.createObjectURL(blob);a.download='candidate-review-draft.json';a.textContent='草稿已生成，点击下载';a.hidden=false;
});
for(const id of ['',...new Set(DATA.map(p=>p.episode_id).filter(Boolean))]){
 const o=document.createElement('option');o.value=id;o.textContent=id?`${id} · ${episodes[id]} 个候选`:'全部候选 Episode';selection.append(o);
}
const hash=decodeURIComponent(location.hash.slice(1));go(byId.has(hash)?hash:DATA[0].checkpoint_id,false);
"""


def render_html(material, manifest_sha256):
    """Render the private multi-packet research preview.

    The HTML contains every packet, including later candidates, so it must never
    be distributed as a clinician packet. No answer or Reference is pre-filled.
    """
    if not material["packets"]:
        raise ValueError("MATERIAL_SET_HAS_NO_CANDIDATES")
    packets = sorted(material["packets"], key=lambda p: p["display_order"]["index"])
    payload = []
    for packet in packets:
        public = {key: value for key, value in packet.items()
                  if key not in ("included_sources", "excluded_sources")}
        public["included_sources"] = [dict(source_id=s["source_id"], title=s.get("title"))
                                     for s in packet["included_sources"]]
        public["excluded_sources"] = list(packet["excluded_sources"])
        public["html"] = _material(packet, {item["checkpoint_id"]: item for item in packets})
        payload.append(public)
    script = _SCRIPT.replace("__DATA__", json.dumps(payload, ensure_ascii=False)
                    .replace("</", "<\\/"))
    script = script.replace("__CASE_ID__", json.dumps(material["case_id"], ensure_ascii=False))
    script = script.replace("__MANIFEST_SHA__", json.dumps(manifest_sha256))
    script = script.replace("__EPISODES__", json.dumps(
        {episode_id: sum(p["episode_id"] == episode_id for p in packets)
         for episode_id in {p["episode_id"] for p in packets}}, ensure_ascii=False))
    script = script.replace("__FORM_VERSION__", json.dumps(FORM_VERSION))
    script = script.replace("__RESPONSE_FORMAT__", json.dumps(RESPONSE_FORMAT))
    script = script.replace("__HTML_FORMAT__", json.dumps(HTML_FORMAT))
    title = "候选 Episode 与 Checkpoint 研究材料"
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>候选 Checkpoint 研究审核</title><style>""" + _STYLE + """</style></head><body data-manifest='""" + _h(manifest_sha256) + """'><main><h1>候选 Episode 与 Checkpoint 研究材料</h1><p class="small">""" + _h(material["case_id"]) + """ · 本地研究审核预览 · 不是医生发布包 · 未形成临床 Reference</p><div class="notice"><strong>阅读顺序</strong><br>默认“本节点增量”依次呈现患者原始文书上下文、Agent 选定的管理判断候选及理由、拟遮蔽范围与前后对照、相邻候选的直接关系。需要核对全文时，再打开目标文书、时点内完整资料、全部 Agent 关系及证据引用。上一/下一候选可点击切换；此顺序按检索时钟及 ID 排列，未知时点后移，不是已确认临床决策顺序。相邻解释、结果及未来信息泄漏尚未经专家核验。</div><div class="notice"><strong>隐私与交付边界</strong><br>此页含全例候选及研究审核专用原文，只能由研究者在许可环境本机使用；不能作为医生盲评文件分发。医生包须另按冻结方案派生，不能仅隐藏本页元素。未请求网络。</div><div class="bar"><label for="episode">候选 Episode</label><select id="episode"></select><label for="checkpoint">当前候选 Checkpoint</label><select id="checkpoint"></select><button id="export" type="button">导出结构化草稿</button><a id="download" hidden></a></div><div id="count" class="small"></div><div id="material"></div></main><script>""" + script + """</script></body></html>"""


def write_html(material, manifest, output):
    output = Path(output)
    page = render_html(material, manifest["manifest_sha256"])
    (output / "index.html").write_text(page, encoding="utf-8")
    receipt = dict(output=str(output / "index.html"), candidates=len(material["packets"]),
                manifest_sha256=manifest["manifest_sha256"], html_format=HTML_FORMAT,
                html_sha256=digest(page), provider_requests=0, doctor_release=False,
                private_research_preview=True, network_calls=0)
    (output / "render-receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt
