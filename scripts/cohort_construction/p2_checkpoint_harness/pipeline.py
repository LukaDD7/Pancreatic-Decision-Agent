"""Stage sequencing for one case: nothing a stage produced may be dropped by the next.

The frozen pipeline lost information at exactly these seams: review overwrote 40
discovery classifications, a single failed citation removed a whole event, 225
zero-event units were re-requested instead of recorded, and assembly silently
refused 106 relations whose cutoff was unknown. The rule here is the one the
research principles already state — a later stage may reinterpret an earlier
record, but it may not make it disappear.

Concretely:

* discovery returns one record per reading task, including tasks with zero
  candidates; scope=no is an explicit, located exclusion, not a missing run;
* a candidate with citation problems still carries its problem list and is never
  silently deleted;
* performed-test units also receive a program-built evidence inventory. The
  discovery LLM does not invent a management question or choice reason for them;
  problem list and reaches ordering; it is never deleted for failing a check;
* ordering may demote an event, but the demotion is a recorded annotation with
  the overturning quote attached, and the event remains in the case with its
  original classification readable;
* episode assembly reports orphan events and unresolved relations instead of
  quietly not merging them.
"""
from __future__ import annotations

from collections import Counter

from .tools.proposals import ProposalStore
from .tools.sources import SourceIndex
from .stages.common import digest, event_id, finish_tool


class CaseRunner:
    """Runs the three stages for one case over an already-built unit inventory."""

    def __init__(self, *, harness, sources, units, case_id):
        self.harness = harness
        self.case_id = case_id
        self.sources = list(sources)
        self.units = list(units)
        self.index = SourceIndex(self.sources, units=self.units)
        self.discovery = {}          # unit_id -> stage record
        self.events = []             # every proposed event, in a stable order
        self.demotions = []
        self.relations = []
        self.rejected = []
        self.evidence_inventory = []
        self.episodes = None
        self.audit = None
        self.stage_stops = []

    # -- discovery -----------------------------------------------------
    def run_discovery(self, *, only_unit_ids=None):
        from .stages import discovery as D

        selected = [unit for unit in self.units
                    if only_unit_ids is None or unit["unit_id"] in only_unit_ids]
        for unit in selected:
            # One session per unit. A unit's judgment must not be coloured by
            # the previous unit's conversation, and re-sending prior units'
            # transcripts is the repeat cost the frozen batch paid 94% of
            # the time.
            store = ProposalStore(source_index=self.index, unit_id=unit["unit_id"])
            stop, requests, failure = None, 0, None
            spent_before = self.harness.spend()["requests"]
            recovery = dict(attempts=0, recovered=False, exhausted=False,
                            sessions=[])
            limit = self.harness.config.max_transport_recovery_attempts
            for attempt in range(limit + 1):
                name = "discovery-" + unit["unit_id"]
                if attempt:
                    name += "-recovery-%d" % attempt
                    recovery["attempts"] = attempt
                recovery["sessions"].append(name)
                session = self.harness.session_for(name)
                registry = self.harness.new_registry(session=session)
                self.index.register(registry)
                from .tools import proposals
                proposals.register(registry, store)
                agent = self.harness.agent(
                    registry=registry, session=session,
                    system_prompt=D.DISCOVERY_SYSTEM_PROMPT)
                try:
                    prompt = D.build_prompt(
                        unit, inline_token_budget=self.harness.config.max_inline_unit_tokens)
                    if attempt:
                        prompt += (
                            "\n\n上一次网络传输失败，本会话是独立恢复尝试；"
                            "此前已提交的提案仍保留。请完成本单位的资料判断，"
                            "相同 local_id 会替代旧提案并保留修订记录，"
                            "不要为规避重复而遗漏真实事件。")
                    result = agent.act(
                        prompt, final_tool=finish_tool(label="本单位判断"))
                    stop = result.stop
                    failure = None
                    recovery["recovered"] = bool(attempt)
                    break
                except Exception as exc:  # noqa: BLE001 - provider failure only
                    from .transport import TransportFailed
                    if not isinstance(exc, TransportFailed):
                        raise
                    # A failed attempt never clears already submitted proposals.
                    stop, requests = "request_failed", agent.requests
                    failure = dict(
                        error=type(exc).__name__,
                        reason_codes=sorted({
                            item.get("reason_code")
                            for item in exc.receipt.get("attempts", [])
                            if item.get("reason_code")}))
                    from . import session as S
                    session.append(S.TURN_END, dict(
                        stop=stop, steps=len(agent.tools.calls),
                        transport_failed=True, recovery_attempt=attempt),
                        turn=agent.turns)
                    self.stage_stops.append(dict(
                        stage="discovery", unit_id=unit["unit_id"], stop=stop,
                        recovery_attempt=attempt))
                    if attempt < limit and self.harness.case_budget.exhausted() is None:
                        continue
                    recovery["exhausted"] = True
                    break
            requests = self.harness.spend()["requests"] - spent_before
            record = dict(unit_id=unit["unit_id"], stop=stop,
                          requests=requests, summary=store.summary(),
                          final=dict(getattr(result, "final", None) or {}) \
                              if "result" in locals() else {},
                          failure=failure, recovery=recovery,
                          problems=[dict(local_id=entry["record"]["local_id"],
                                         citation_verdicts=[
                                             verdict for verdict in entry["citation_verdicts"]
                                             if not verdict.get("ok")])
                                    for entry in store.events()
                                    if entry["status"] != "ACCEPTED"],
                          out_of_scope_candidates=[
                              _event(unit, entry) for entry in store.rejected()])
            if unit.get("kind") == "performed_test_report":
                self.evidence_inventory.append(_evidence_artifact(unit))
            self.discovery[unit["unit_id"]] = record
            self.stage_stops.append(dict(stage="discovery", unit_id=unit["unit_id"],
                                         stop=stop))
            for entry in store.events():
                if entry["record"].get("scope") != "no":
                    self.events.append(_event(unit, entry))
            self.rejected.extend(_event(unit, entry) for entry in store.rejected())
        self.events.sort(key=lambda event: event["event_id"])
        return dict(units=len(self.discovery),
                    events=len(self.events),
                    zero_event_units=sum(1 for record in self.discovery.values()
                                         if record["summary"]["proposals"] == 0),
                    units_with_stop=dict(Counter(record["stop"]
                                                 for record in self.discovery.values())),
                    transport_recoveries=sum(
                        record["recovery"]["attempts"]
                        for record in self.discovery.values()),
                    transport_recovered_units=sum(
                        record["recovery"]["recovered"]
                        for record in self.discovery.values()),
                    transport_recovery_exhausted=sum(
                        record["recovery"]["exhausted"]
                        for record in self.discovery.values()))

    # -- ordering ------------------------------------------------------
    def run_ordering(self):
        from .stages import ordering as O

        active = self._active_candidates()
        index = O.EventIndex(active, source_index=self.index)
        stop, failure = None, None
        recovery = dict(attempts=0, recovered=False, exhausted=False,
                        sessions=[])
        limit = self.harness.config.max_transport_recovery_attempts
        for attempt in range(limit + 1):
            name = "ordering"
            if attempt:
                name += "-recovery-%d" % attempt
                recovery["attempts"] = attempt
            recovery["sessions"].append(name)
            session = self.harness.session_for(name)
            registry = self.harness.new_registry(session=session)
            self.index.register(registry)
            for tool in index.tools():
                registry.register(tool)
            recorded = O.relation_tools(registry, events=active,
                                        source_index=self.index)
            agent = self.harness.agent(registry=registry, session=session,
                                       system_prompt=O.ORDERING_SYSTEM_PROMPT)
            try:
                prompt = O.build_prompt(active, case_id=self.case_id)
                if attempt:
                    prompt += (
                        "\n\n上一次网络传输失败，本会话是独立恢复尝试；"
                        "此前记录的关系与降级标注仍保留。"
                        "请基于候选事件与原文完成事件组织，不要省略尚未核对的部分。")
                result = agent.act(prompt, final_tool=finish_tool(label="事件组织"))
                stop, failure = result.stop, None
                recovery["recovered"] = bool(attempt)
                break
            except Exception as exc:  # noqa: BLE001 - provider failure only
                from .transport import TransportFailed
                if not isinstance(exc, TransportFailed):
                    raise
                stop = "request_failed"
                failure = dict(
                    error=type(exc).__name__,
                    reason_codes=sorted({
                        item.get("reason_code")
                        for item in exc.receipt.get("attempts", [])
                        if item.get("reason_code")}))
                from . import session as S
                session.append(S.TURN_END, dict(
                    stop=stop, steps=len(agent.tools.calls),
                    transport_failed=True, recovery_attempt=attempt),
                    turn=agent.turns)
                self.stage_stops.append(dict(
                    stage="ordering", stop=stop, recovery_attempt=attempt))
                if attempt < limit and self.harness.case_budget.exhausted() is None:
                    continue
                recovery["exhausted"] = True
                break
        self.relations = list(recorded["relations"])
        self.demotions = list(recorded["demotions"])
        self._annotate_demotions()
        # The append-only session keeps earlier attempts. The active projection
        # must do the same, rather than silently discarding previously recorded
        # relations during a transport recovery or an incremental revision.
        previous = getattr(self, "_ordering_records", [])
        record = dict(relations=list(self.relations),
                      demotions=list(self.demotions), stop=stop,
                      failure=failure, recovery=dict(recovery))
        previous.append(record)
        merged_relations = []
        merged_demotions = []
        for item in previous:
            for relation in item["relations"]:
                if relation not in merged_relations:
                    merged_relations.append(relation)
            for demotion in item["demotions"]:
                if demotion not in merged_demotions:
                    merged_demotions.append(demotion)
        self.relations = merged_relations
        self.demotions = merged_demotions
        self._annotate_demotions()
        self._ordering_records = previous
        self.stage_stops.append(dict(stage="ordering", stop=stop))
        return dict(relations=len(self.relations), demotions=len(self.demotions),
                    stop=stop, failure=failure, recovery=recovery,
                    attempts=len(previous))

    def candidates(self):
        """All active/out-of-scope LLM spans, excluding program evidence indexes."""
        return list(self.events) + list(self.rejected)

    def _active_candidates(self):
        return [event for event in self.events
                if not event["demoted"] and event.get("scope") != "no"]

    def _organization_objects(self):
        return self._active_candidates() + list(self.evidence_inventory)

    def _annotate_demotions(self):
        """A demotion marks the event; it never removes it from the case."""
        by_id = {event["event_id"]: event for event in self.events}
        for demotion in self.demotions:
            event = by_id.get(demotion["event_id"])
            if event is None:
                continue
            event["demoted"] = True
            event["demotion"] = dict(
                reason=demotion["reason"],
                basis_quotes=demotion.get("basis_quotes") or [],
                to_status=demotion.get("to_status") or "not_a_checkpoint",
                discovery_status=event["status"])

    # -- episodes ------------------------------------------------------
    def run_episodes(self):
        from .stages import episodes as E
        from .stages import ordering as O

        active = self._organization_objects()
        builder = E.EpisodeBuilder(active, relations=self.relations)
        self._episode_review = None
        self._episode_stop = "assembled"
        self._episode_final = {}
        additions = []
        if active:
            # A case-level review must see the transitive containers, not only
            # pairwise rationales. It can supplement relations but cannot erase
            # the append-only ordering record. A transport failure gets its own
            # bounded recovery; it must not erase already-recorded corrections.
            recovery = dict(attempts=0, recovered=False, exhausted=False,
                            sessions=[])
            records = []
            limit = self.harness.config.max_transport_recovery_attempts
            for attempt in range(limit + 1):
                name = "episodes" if not attempt else "episodes-recovery-%d" % attempt
                if attempt:
                    recovery["attempts"] = attempt
                recovery["sessions"].append(name)
                session = self.harness.session_for(name)
                registry = self.harness.new_registry(session=session)
                self.index.register(registry)
                index = O.EventIndex(active, source_index=self.index)
                for tool in index.tools():
                    registry.register(tool)
                for tool in builder.tools():
                    registry.register(tool)
                recorded = O.relation_tools(
                    registry, events=active, allow_demote=False, source_index=self.index)
                recorded["relations"].extend(
                    relation for relation in additions
                    if relation not in recorded["relations"])
                agent = self.harness.agent(registry=registry, session=session,
                                           system_prompt=E.EPISODE_SYSTEM_PROMPT)
                try:
                    prompt = E.build_prompt(active, case_id=self.case_id)
                    if attempt:
                        prompt += ("\n\n上一次网络传输失败，本会话是独立恢复尝试；"
                                   "此前记录的关系仍保留，请完成尚未核对的容器和证据覆盖。")
                    outcome = agent.act(
                        prompt, final_tool=E.episode_finish_tool(
                            events=active, builder=builder))
                    self._episode_stop = outcome.stop
                    self._episode_final = outcome.final
                    records.append(recorded["relations"])
                    recovery["recovered"] = bool(attempt)
                    break
                except Exception as exc:  # noqa: BLE001 - provider failure only
                    from .transport import TransportFailed
                    if not isinstance(exc, TransportFailed):
                        raise
                    self._episode_stop = "request_failed"
                    self._episode_final = {}
                    records.append(recorded["relations"])
                    from . import session as S
                    session.append(S.TURN_END, dict(
                        stop=self._episode_stop, steps=len(agent.tools.calls),
                        transport_failed=True, recovery_attempt=attempt), turn=agent.turns)
                    self.stage_stops.append(dict(
                        stage="episodes", stop=self._episode_stop,
                        recovery_attempt=attempt))
                    if attempt < limit and self.harness.case_budget.exhausted() is None:
                        continue
                    recovery["exhausted"] = True
                    break
            for batch in records:
                additions.extend(relation for relation in batch
                                 if relation not in additions
                                 and relation not in self.relations)
            self.relations.extend(additions)
            # The builder was constructed before this stage started so its
            # preview tools are available. Refresh its read-only relation view
            # before the final assembly; otherwise the final graph omits every
            # relation recorded by the episode-review agent.
            builder.relations = list(self.relations)
            self._episode_review = dict(
                stop=self._episode_stop, final=self._episode_final,
                added_relations=len(additions), recovery=recovery)
        result = builder.assemble()
        # A later ordering revision must not turn duplicate identities into a
        # different process. Keep the old root for an existing process group;
        # first assembly has no prior roots and simply uses the current ones.
        previous_roots = {
            frozenset(episode["event_ids"]): episode["root_event_id"]
            for episode in (self.episodes or {}).get("episodes", [])}
        for episode in result["episodes"]:
            members = frozenset(episode["event_ids"])
            if members in previous_roots:
                root = previous_roots[members]
                episode["root_event_id"] = root
                builder.memberships.update({event_id: root for event_id in members})
        result["duplicate_candidate_groups"] = builder._event_groups(
            sorted(builder.by_id), builder._identity_root)
        self.episodes = result
        self.audit = E.audit(active, self.relations, result)
        self.stage_stops.append(dict(
            stage="episodes", stop=getattr(self, "_episode_stop", "assembled"),
            added_relations=len(additions)))
        return dict(episodes=len(result["episodes"]),
                    singleton=result["singleton_episodes"],
                    unresolved=len(result["unresolved_relations"]),
                    stop=getattr(self, "_episode_stop", "assembled"),
                    added_relations=len(additions))

    # -- output --------------------------------------------------------
    def case_dir(self):
        return digest([self.case_id, sorted(event["event_id"]
                                            for event in self.events)])[:16]

    def artifacts(self):
        """Everything a later reader needs, including what is unresolved."""
        return dict(
            format="p2-harness-case-0.2.0",
            case_id=self.case_id,
            discovery=dict(
                contract="located_candidate_expressions_not_confirmed_decision_events",
                units=dict(sorted(self.discovery.items())),
                units_total=len(self.units),
                units_run=len(self.discovery)),
            candidate_spans=self.events,
            events=self.events,
            out_of_scope_candidates=self.rejected,
            evidence_inventory=self.evidence_inventory,
            rejected_mentions=self.rejected,
            relations=dict(
                recorded=self.relations,
                demotions=self.demotions,
                unresolved=(self.episodes or {}).get("unresolved_relations", [])),
            episodes=self.episodes,
            episode_review=getattr(self, "_episode_review", None),
            audit=self.audit,
            stage_stops=self.stage_stops,
            coverage=_coverage(self.units, self.discovery),
            counts=dict(
                events=len(self.events),
                candidate_spans=len(self.events),
                out_of_scope=len(self.rejected),
                evidence_artifacts=len(self.evidence_inventory),
                accepted=sum(1 for event in self.events
                             if event["status"] == "ACCEPTED"),
                with_problems=sum(1 for event in self.events
                                  if event["status"] != "ACCEPTED"),
                demoted=sum(1 for event in self.events if event["demoted"]),
                relations=len(self.relations),
                episodes=len((self.episodes or {}).get("episodes") or [])),
            note=("本文件是带原文边界的候选重建结果，不是临床Reference。"
                  "scope=no 保留为明确排除索引，不进入候选事件组织；未运行、未识别、"
                  "被降级、未解析关系和带引文问题的候选均保留并计数。"
                  "程序证据索引不是管理判断或已确认临床决策点。"))


def _event(unit, entry):
    """Project one located expression for downstream candidate organization."""
    record = entry["record"]
    attribution = record.get("attribution") or {}
    origins = list(attribution.get("origin_source_ids") or ())
    return dict(
        event_id=event_id(origins, record["local_id"]),
        local_id=record["local_id"],
        candidate_kind="management_expression",
        # Reading assignment is audit metadata, never candidate ownership.
        discovery_unit_id=unit["unit_id"],
        discovery_unit_kind=unit.get("kind"),
        discovery_document_profile=unit.get("document_profile"),
        discovery_source_ids=list(unit.get("source_ids") or ()),
        origin_source_ids=origins,
        origin_unit_id=attribution.get("origin_unit_id"),
        origin_unit_kind=attribution.get("origin_unit_kind"),
        origin_document_profile=attribution.get("origin_document_profile"),
        source_ids=list(origins),
        attribution_problems=list(attribution.get("problems") or ()),
        # Source clock supports retrieval only; it is not a choice timestamp.
        clock=_clock_of(attribution),
        clock_basis=attribution.get("clock_basis"),
        status=entry["status"],
        scope=record.get("scope"),
        record_function=record.get("record_function"),
        span=dict(record.get("span") or {}),
        uncertainty=record.get("uncertainty"),
        rationale=record.get("rationale"),
        reason=record.get("reason"),
        record_id=entry["record_id"],
        citation_verdicts=entry["citation_verdicts"],
        revision=entry.get("revision"),
        demoted=False,
        demotion=None)


def _evidence_artifact(unit):
    """Program inventory entry; never an LLM-authored management question."""
    sources = [dict(source_id=source["source_id"],
                    source_sha256=source.get("source_sha256"),
                    characters=len(source.get("text") or ""))
               for source in unit.get("original_texts") or []
               if source.get("source_id")]
    origins = sorted(source["source_id"] for source in sources)
    return dict(
        event_id="EA-" + digest([unit["unit_id"], origins])[:16],
        local_id=None, candidate_kind="evidence_artifact",
        object_kind="evidence_artifact",
        discovery_unit_id=unit["unit_id"],
        discovery_unit_kind=unit.get("kind"),
        discovery_document_profile=unit.get("document_profile"),
        discovery_source_ids=list(unit.get("source_ids") or ()),
        origin_source_ids=origins, origin_unit_id=unit["unit_id"],
        origin_unit_kind=unit.get("kind"),
        origin_document_profile=unit.get("document_profile"),
        source_ids=origins, source_inventory=sources,
        attribution_problems=[], clock=_clock_of(unit),
        clock_basis=unit.get("clock_basis"),
        status="SOURCE_INDEXED", scope=None, record_function=None,
        choice_time=None, time_basis="unknown",
        evidence_selection_record_status="not_assessed_in_discovery",
        attribution_note=("这是程序组织的同次检查候选报告组的证据索引，"
                          "不是已确认的独立临床决策点，也不证明谁、何时、为何选择检查。"),
        demoted=False, demotion=None)


def _clock_of(unit):
    """Normalise whatever shape the unit carries into ``{value, precision}``."""
    raw = unit.get("clock")
    if isinstance(raw, dict):
        return dict(value=raw.get("value"), precision=raw.get("precision") or "unknown")
    if isinstance(raw, str) and raw:
        precision = "minute" if len(raw) == 16 else (
            "second" if len(raw) >= 19 else "date")
        return dict(value=raw, precision=precision)
    return dict(value=None, precision="unknown")


def _coverage(units, discovery):
    """Say out loud which units were not run, instead of reporting a clean total."""
    run = set(discovery)
    missing = [unit["unit_id"] for unit in units if unit["unit_id"] not in run]
    return dict(units_not_run=len(missing), units_not_run_ids=sorted(missing)[:50],
                note=("未运行的单位不是'无事件'，只是本阶段没有处理它们。"
                      "任何按事件数为零解释它们的说法都不成立。"))