"""Build the harness for one case: sessions, transport, tools, skills, compaction.

Everything the caller must supply is explicit. Nothing here reads a frozen
batch, a prior cache, a reference file or a credential file.

One decision worth stating: a case gets **one session per turn-series**, not one
session for the whole case. Discovery for unit 4 must not inherit unit 3's
conversation — that would make the fourth document's judgment depend on the
third one's, and it would pay for the same history again and again. Compaction
then exists for the real case it is meant for: one long unit, or one long
ordering turn, where the work genuinely outgrows the window.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import compaction as C
from . import session as S
from .agent import Agent, Budget
from .skills import SkillRegistry
from .spill import SpillStore
from .tokens import TokenMeter
from .tools import ToolRegistry


@dataclass
class HarnessConfig:
    """All tunables in one place, with defaults that are defensible.

    ``context_window`` is the only number that must be supplied: everything
    about when to condense follows from it. There is deliberately no
    ``max_context_characters`` — that was the setting that measured the wrong
    thing.
    """

    context_window: int = 1_000_000
    output_reservation: int = 16_000
    threshold_ratio: float = 0.8
    retain_ratio: float = 0.16
    headroom_tokens: int = 65_536
    max_summary_tokens: int = 4_096
    max_inline_tool_tokens: int = 12_000
    max_inline_unit_tokens: int = 60_000
    max_turns: int = 12
    max_requests: int = 80
    max_total_tokens: int = 6_000_000
    max_steps_per_turn: int = 14
    max_transport_recovery_attempts: int = 2
    model_temperature: int = 0
    max_output_tokens: int = 8_000

    def policy(self):
        return C.CompactionPolicy(
            context_window=self.context_window,
            output_reservation=self.output_reservation,
            threshold_ratio=self.threshold_ratio,
            retain_ratio=self.retain_ratio,
            headroom_tokens=self.headroom_tokens,
            max_summary_tokens=self.max_summary_tokens)

    def budget(self):
        """One budget per case, shared by every agent the case builds.

        Request and token caps are case-level: an authorization grants a budget
        for the case, not for each of its 37 discovery units. Building a fresh
        ``Budget`` per agent made the cap per-unit while its number was read as
        per-case, so the case could spend 37× what the authorization named. The
        per-turn caps stay inside the same object because they are per-agent and
        the agent already resets its own turn counter.
        """
        return Budget(max_turns=self.max_turns, max_requests=self.max_requests,
                      max_total_tokens=self.max_total_tokens,
                      max_steps_per_turn=self.max_steps_per_turn)


class Harness:
    """One case's agent stack. Create per case; do not share sessions."""

    def __init__(self, *, case_id, run_dir, cache_dir, transport, config=None,
                 skills_dirs=None):
        self.case_id = case_id
        self.config = config or HarnessConfig()
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = Path(cache_dir)
        self.meter = TokenMeter()
        self.case_budget = self.config.budget()
        self.transport = transport
        self.spill = SpillStore(self.run_dir / "spill",
                                max_inline_tokens=self.config.max_inline_tool_tokens,
                                meter=self.meter)
        self.skills = SkillRegistry(skills_dirs or [
            Path(__file__).resolve().parent / "skills"])
        self.compaction = C.CompactionEngine(
            self.config.policy(), meter=self.meter,
            summarize=self._summarize)
        self._summarize_calls = []
        self._sessions = {}
        self._closed = False

    # -- sessions ------------------------------------------------------
    def session_for(self, name):
        """Return the session for one turn-series, creating it on first use."""
        if name in self._sessions:
            return self._sessions[name]
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:80] or "session"
        directory = self.run_dir / "sessions"
        session = S.Session(
            directory / (slug + ".jsonl"),
            session_id="p2h-%s-%s" % (self.case_id, name),
            metadata=dict(case_id=self.case_id,
                          created_at=datetime.now(timezone.utc).isoformat(),
                          config=_config_snapshot(self.config)))
        self._sessions[name] = session
        return session

    def sessions(self):
        return dict(self._sessions)

    # -- factory -------------------------------------------------------
    def _summarize(self, instruction, compaction_id):
        """Summarise a span with the same transport, redaction and receipts.

        The provider is called with ``response_format=json_object`` everywhere,
        so the summary is requested as a JSON object too. Asking for bare text
        under a JSON-only response format is a request the provider cannot
        honour; the model would answer in JSON regardless and the caller would
        store the serialised envelope as the "summary".
        """
        messages = [
            dict(role="system", content=(
                "你在压缩一段工作记录。只保留后续判断仍然需要的事实："
                "标识符、引文、时间及其精度、已作出的决定与理由、"
                "以及明确的不确定与未决问题。不要补充不存在的内容。"
                '只输出一个 JSON 对象，形如 {"summary": "压缩后的文本"}。')),
            dict(role="user", content=instruction),
        ]
        result, receipt = self.transport.complete(
            messages, task="compaction_summary", purpose=compaction_id)
        # A compaction summary is a billed request like any other. Leaving it
        # out of the case ledger would let the case exceed what the
        # authorization granted without tripping a cap.
        usage = receipt.get("usage") or {}
        self.case_budget.take_request()
        self.case_budget.add_usage(
            usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0,
            usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
        value = _summary_text(result)
        if not value.strip():
            raise ValueError("COMPACTION_SUMMARY_EMPTY")
        self._summarize_calls.append(dict(
            compaction_id=compaction_id, characters=len(value),
            usage=receipt.get("usage") or {}))
        return value

    def agent(self, *, registry, system_prompt, session=None, budget=None):
        # ``Session.__len__`` counts events, so an empty session is falsy.
        # ``session or ...`` silently replaced the caller's session on its first
        # empty turn; the check must be against None.
        target = self.session_for("default") if session is None else session
        return Agent(session=target,
                     transport=self.transport, tools=registry, skills=self.skills,
                     compaction=self.compaction, meter=self.meter,
                     budget=budget or self.case_budget,
                     system_prompt=system_prompt)

    def spend(self):
        """What the case has actually spent, against what the run was allowed."""
        return self.case_budget.spent()

    def approve_writes(self, *, session=None):
        """The write gate. Every approval is recorded with the tool and arguments.

        The approval is written into the session that owns the turn, so a
        proposal is traceable to the conversation that made it.
        """
        scope = session

        def approve(tool, arguments, call_id):
            target = self.session_for("default") if scope is None else scope
            target.append(S.POLICY_EVENT, dict(
                kind="write_approved", tool=tool.name, call_id=call_id,
                argument_keys=sorted(arguments or {})))
            return True
        return approve

    def new_registry(self, *, session=None, approve=None, max_batch=16):
        return ToolRegistry(
            approve=approve or self.approve_writes(session=session),
            max_batch=max_batch, parallel_reads=4, spill=self.spill)

    # -- close ---------------------------------------------------------
    def close(self):
        if self._closed:
            return self.summary()
        summary = self.summary()
        self._closed = True
        for session in self._sessions.values():
            try:
                open_compaction = session.active_compaction()
                if open_compaction is not None:
                    # An interrupted compaction is closed as abandoned, never
                    # as complete, so a reader never mistakes it for a summary
                    # that exists.
                    session.abandon_stale_compaction(
                        session.compaction_id(open_compaction),
                        reason="harness closed with a compaction in flight")
            finally:
                session.close()
        return summary

    def summary(self):
        sessions = {}
        for name, session in self._sessions.items():
            completed = sum(
                1 for record in session.events()
                if record["type"] == S.COMPACTION_END
                and (record.get("payload") or {}).get("outcome") == "COMPLETE")
            sessions[name] = dict(events=len(session), compactions=completed)
        return dict(
            case_id=self.case_id, sessions=sessions,
            events=sum(len(session) for session in self._sessions.values()),
            summarize_calls=len(self._summarize_calls),
            spill_files=len(self.spill.written),
            receipts=len(self.transport.receipts),
            budget_spent=self.spend())


def _config_snapshot(config):
    return {key: getattr(config, key) for key in (
        "context_window", "output_reservation", "threshold_ratio",
        "retain_ratio", "headroom_tokens", "max_turns", "max_requests",
        "max_total_tokens", "max_inline_tool_tokens", "max_inline_unit_tokens",
        "max_transport_recovery_attempts")}


def _summary_text(result):
    """Accept the documented object shape, and a bare string, nothing else."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("summary", "text", "content"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def json_dumps(value):
    return json.dumps(value, ensure_ascii=False)