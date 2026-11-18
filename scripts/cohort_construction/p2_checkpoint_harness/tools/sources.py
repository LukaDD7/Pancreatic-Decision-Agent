"""Retrieval tools over the immutable source set.

This module is the answer to the concrete defect measured in the frozen batch:
``discovery_context_characters`` bounded only 10% of the request while a
metadata catalog was 57%, and the largest request reached 186,482 characters
against a configured limit of 80,000. The cause was that "what the model may
see" was decided once, up front, by sending everything. Here it is decided by
the model, one query at a time.

Every tool returns data plus a count of what it did not return, so a truncated
answer is visibly truncated. None of them mutate anything; the only write tools
live in ``tools/propose.py``.
"""
from __future__ import annotations

import re

from .registry import Tool, ToolError


class SourceIndex:
    """Read-only view over one case's sources.

    ``sources`` must be the same objects the rest of the pipeline uses; nothing
    is copied, re-timestamped or normalised here. A retrieved span carries the
    exact ``source_sha256`` so a quote can be verified against it later.
    """

    def __init__(self, sources, *, units=()):
        self.sources = list(sources)
        self.by_id = {source["source_id"]: source for source in self.sources}
        self.units = list(units)
        self.units_by_id = {unit["unit_id"]: unit for unit in self.units}
        self.read_source_ids = set()
        # A source may appear in several units only if the unit grouping put it
        # there; keep the mapping explicit rather than guessing.
        self.source_units = {}
        for unit in self.units:
            for source_id in unit.get("source_ids") or ():
                self.source_units.setdefault(source_id, []).append(unit["unit_id"])

    # -- lookup --------------------------------------------------------
    def require(self, source_id):
        source = self.by_id.get(source_id)
        if source is None:
            raise ToolError(
                "UNKNOWN_SOURCE_ID: %r is not in this case. Use list_sources or "
                "search_sources to find the right id." % (source_id,))
        return source

    def unit_sources(self, unit_id):
        """The exact immutable source membership shown for one discovery unit."""
        unit = self.units_by_id.get(unit_id)
        if unit is None:
            raise ToolError("UNKNOWN_UNIT_ID: %r" % (unit_id,))
        return tuple(unit.get("source_ids") or ())

    @staticmethod
    def _clock_value(source, role="clock"):
        if role == "clock":
            return (source.get("clock") or {}).get("value")
        return ((source.get("clocks") or {}).get(role) or {}).get("value")

    # -- tools ---------------------------------------------------------
    def tool_list_sources(self):
        def run(kind=None, title_contains=None, after=None, before=None,
                has_clock=None, offset=0, limit=40):
            rows = []
            for source in self.sources:
                if kind and source.get("kind") != kind:
                    continue
                if title_contains and title_contains not in (source.get("title") or ""):
                    continue
                clock = self._clock_value(source)
                if has_clock is True and not clock:
                    continue
                if has_clock is False and clock:
                    continue
                if after and not (clock and clock >= after):
                    continue
                if before and not (clock and clock < before):
                    continue
                rows.append(self._summary(source))
            rows.sort(key=lambda row: (row["clock"] or "9999", row["source_id"]))
            offset = max(0, int(offset))
            limit = max(1, min(200, int(limit)))
            page = rows[offset:offset + limit]
            return dict(
                total=len(rows), offset=offset, returned=len(page),
                truncated=offset + len(page) < len(rows),
                characters=sum(row["characters"] for row in page),
                sources=page,
                note=("clock is the retrieval clock (the earliest defensible time "
                      "this record could be known). A record clock is not a "
                      "clinical decision time."))
        return Tool(
            name="list_sources",
            description=("按类型/标题/时间浏览本例来源目录。只返回元数据，不返回正文。"
                         "先用它确定范围，再用 read_source 取需要的段落。"),
            input_schema=dict(type="object", additionalProperties=False, properties=dict(
                kind=dict(type="string", description="document / imaging / lab / pathology"),
                title_contains=dict(type="string"),
                after=dict(type="string", description="ISO 时间下界，含"),
                before=dict(type="string", description="ISO 时间上界，不含"),
                has_clock=dict(type="boolean"),
                offset=dict(type="integer", minimum=0),
                limit=dict(type="integer", minimum=1, maximum=200))),
            run=run)

    def tool_search_sources(self):
        def run(query, kind=None, context_chars=120, limit=20):
            if not isinstance(query, str) or not query.strip():
                raise ToolError("SEARCH_QUERY_REQUIRED")
            limit = max(1, min(50, int(limit)))
            context_chars = max(20, min(400, int(context_chars)))
            pattern = re.compile(re.escape(query))
            hits = []
            for source in self.sources:
                if kind and source.get("kind") != kind:
                    continue
                text = source.get("text") or ""
                for match in pattern.finditer(text):
                    start = max(0, match.start() - context_chars)
                    end = min(len(text), match.end() + context_chars)
                    hits.append(dict(
                        source_id=source["source_id"], title=source.get("title"),
                        kind=source.get("kind"), clock=self._clock_value(source),
                        start=start, end=end, text=text[start:end]))
                    if len(hits) >= limit * 4:
                        break
                if len(hits) >= limit * 4:
                    break
            return dict(query=query, total_occurrences_scanned=len(hits),
                        returned=min(len(hits), limit), truncated=len(hits) > limit,
                        hits=hits[:limit],
                        note=("每个命中都带 source_id 与字符偏移；引用时必须逐字复制，"
                              "不要把不同命中的片段拼接。"))
        return Tool(
            name="search_sources",
            description=("在整份来源正文中做字面检索，返回命中位置上下文。"
                         "适合找某个具体措施（如“减黄”“分期腹腔镜”“增强CT”）出现过的地方。"),
            input_schema=dict(type="object", additionalProperties=False,
                              required=["query"], properties=dict(
                query=dict(type="string", minLength=1),
                kind=dict(type="string"),
                context_chars=dict(type="integer", minimum=20, maximum=400),
                limit=dict(type="integer", minimum=1, maximum=50))),
            run=run)

    def tool_read_source(self):
        def run(source_id, start=0, end=None):
            source = self.require(source_id)
            text = source.get("text") or ""
            start = max(0, int(start))
            end = len(text) if end is None else min(len(text), int(end))
            if end <= start:
                raise ToolError("READ_RANGE_EMPTY")
            self.read_source_ids.add(source_id)
            return dict(source_id=source_id, title=source.get("title"),
                        kind=source.get("kind"),
                        source_sha256=source.get("source_sha256"),
                        clock=self._clock_value(source),
                        clock_basis=source.get("clock_basis"),
                        clocks=dict(source.get("clocks") or {}),
                        total_characters=len(text), start=start, end=end,
                        truncated=start > 0 or end < len(text),
                        text=text[start:end])
        return Tool(
            name="read_source",
            description=("读取一份来源的原文区间。整份不会自动铺开——先用目录或检索定位，"
                         "再读你真正需要判断的那一段。偏移是字符偏移，与引用一致。"),
            input_schema=dict(type="object", additionalProperties=False,
                              required=["source_id"], properties=dict(
                source_id=dict(type="string", minLength=1),
                start=dict(type="integer", minimum=0),
                end=dict(type="integer", minimum=0))),
            run=run)

    def tool_list_units(self):
        def run(offset=0, limit=50):
            offset = max(0, int(offset))
            limit = max(1, min(200, int(limit)))
            rows = [dict(unit_id=unit["unit_id"], kind=unit.get("kind"),
                         document_profile=unit.get("document_profile"),
                         clock=(unit.get("clock") or {}).get("value"),
                         source_ids=list(unit.get("source_ids") or ()),
                         characters=sum(len(t.get("text", ""))
                                        for t in unit.get("original_texts") or ()))
                    for unit in self.units]
            rows.sort(key=lambda row: (row["clock"] or "9999", row["unit_id"]))
            page = rows[offset:offset + limit]
            return dict(total=len(rows), offset=offset, returned=len(page),
                        truncated=offset + len(page) < len(rows), units=page)
        return Tool(
            name="list_units",
            description="列出本例的处理单位（一份文书，或同一次检查的一组报告）。",
            input_schema=dict(type="object", additionalProperties=False, properties=dict(
                offset=dict(type="integer", minimum=0),
                limit=dict(type="integer", minimum=1, maximum=200))),
            run=run)

    def tool_source_units(self):
        def run(source_id):
            self.require(source_id)
            return dict(source_id=source_id,
                        unit_ids=self.source_units.get(source_id, []))
        return Tool(
            name="source_units",
            description="查一份来源属于哪些处理单位（同一份报告可能出现在多个导出行）。",
            input_schema=dict(type="object", additionalProperties=False,
                              required=["source_id"],
                              properties=dict(source_id=dict(type="string"))),
            run=run)

    def check_quote(self, citation, *, label="citation"):
        """Shared exact-source check for proposals, relations and demotions."""
        from .proposals import ProposalStore
        return ProposalStore(source_index=self).check_quote(citation, label=label)

    # -- registration --------------------------------------------------
    def tool_read_unit(self):
        def run(unit_id):
            unit = self.units_by_id.get(unit_id)
            if unit is None:
                raise ToolError(
                    "UNKNOWN_UNIT_ID: %r. Call list_units first." % (unit_id,))
            return dict(
                unit_id=unit_id, kind=unit.get("kind"),
                document_profile=unit.get("document_profile"),
                clock=dict(unit.get("clock") or {}),
                clock_basis=unit.get("clock_basis"),
                sources=[dict(
                    source_id=item.get("source_id"),
                    source_sha256=(self.by_id.get(item.get("source_id")) or {}).get(
                        "source_sha256"),
                    characters=len(item.get("text") or ""),
                    text=item.get("text") or "")
                    for item in unit.get("original_texts") or ()],
                note=("这是程序组织的同一处理单位的原文，不是全患者病历；"
                      "单位身份用于核对同一判断的重复提案，不是已确认的临床 episode。"))
        return Tool(
            name="read_unit",
            description=("按处理单位读取实际原文及其 source_id，用于核对重复提案和"
                         "候选检查组的完整范围。一次只读一个单位。"),
            input_schema=dict(type="object", additionalProperties=False,
                              required=["unit_id"], properties=dict(
                                  unit_id=dict(type="string"))),
            run=run)

    # -- registration --------------------------------------------------
    def register(self, registry):
        for factory in (self.tool_list_sources, self.tool_search_sources,
                        self.tool_read_source, self.tool_list_units,
                        self.tool_source_units, self.tool_read_unit):
            registry.register(factory())
        return registry

    def _summary(self, source):
        return dict(
            source_id=source["source_id"], kind=source.get("kind"),
            title=source.get("title"), clock=self._clock_value(source),
            clock_basis=source.get("clock_basis"),
            characters=len(source.get("text") or ""),
            unit_ids=self.source_units.get(source["source_id"], []))