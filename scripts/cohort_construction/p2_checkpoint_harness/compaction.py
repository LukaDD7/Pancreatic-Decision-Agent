"""Context condensation: the real successor to a hand-tuned character ceiling.

The contract is dsh's, reduced to one backend. What is copied and why:

* **Trigger**: ``floor(min(W * thresholdRatio, W - O - headroom))`` with
  ``thresholdRatio=0.8`` and ``headroom=65,536`` tokens. A ceiling set by hand
  measured the wrong thing: in the frozen batch the configured
  ``discovery_context_characters=80000`` bounded 10% of the request while a
  metadata catalog was 57%, so the largest request reached 186,482 characters
  under a limit that never fired.
* **Retention**: keep the newest ``retainRatio`` (0.16) of ``W - O`` verbatim.
  The tail is where the current work is; older spans get summarised.
* **Bracket-first transaction**: append ``compaction/start``, build the summary,
  revalidate, append ``compaction/summary`` + a ``user/message`` carrying
  ``surfaceOp={op:'replace', startSeq, endSeq}``, then ``compaction/end``. A crash
  between start and end leaves an unmatched start, which the session reports as
  an orphan rather than a completed compaction.
* **Tool pairing**: both edges of the condensed span are moved outward until no
  tool call is separated from its result. A half pair in history is a request
  the provider rejects, and a summary that splits one silently drops evidence.

One divergence, deliberate: the summary is produced by a caller-supplied
``summarize`` callable so the pipeline can pin the same provider, redaction and
receipt chain it uses everywhere else. No second client, no second cache.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

from . import session as S

DEFAULT_THRESHOLD_RATIO = 0.8
DEFAULT_RETAIN_RATIO = 0.16
DEFAULT_HEADROOM_TOKENS = 65536
DEFAULT_MAX_SUMMARY_TOKENS = 4096

CHECKPOINT_PREAMBLE = (
    "This is an automatically generated checkpoint condensing an earlier span of "
    "the conversation to free up context. Treat the captured context as "
    "established background and build on it without restating it. Continue the "
    "task directly from the messages that follow, without acknowledging this "
    "checkpoint.")


@dataclass(frozen=True)
class CompactionPolicy:
    context_window: int
    output_reservation: int = 16000
    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO
    retain_ratio: float = DEFAULT_RETAIN_RATIO
    headroom_tokens: int = DEFAULT_HEADROOM_TOKENS
    max_summary_tokens: int = DEFAULT_MAX_SUMMARY_TOKENS

    def __post_init__(self):
        for field in ("context_window", "output_reservation", "headroom_tokens"):
            value = getattr(self, field)
            if not isinstance(value, int) or value <= 0:
                raise ValueError("COMPACTION_POLICY_INVALID:" + field)
        if not 0 < self.threshold_ratio < 1:
            raise ValueError("COMPACTION_THRESHOLD_RATIO_INVALID")
        if not 0 < self.retain_ratio < self.threshold_ratio:
            # A retained span at or above the trigger means compaction can never
            # shrink the surface enough to be worth a model call.
            raise ValueError("COMPACTION_RETAIN_RATIO_INVALID")
        if self.available <= 0:
            raise ValueError("COMPACTION_WINDOW_TOO_SMALL_FOR_HEADROOM")

    @property
    def available(self):
        return self.context_window - self.output_reservation - self.headroom_tokens

    @property
    def trigger(self):
        return int(min(self.context_window * self.threshold_ratio, self.available))

    @property
    def retain_tokens(self):
        return int((self.context_window - self.output_reservation) * self.retain_ratio)


@dataclass
class CompactionResult:
    compaction_id: str
    start_seq: int
    end_seq: int
    summary_seq: int
    shadowed_seqs: list
    shadowed_tokens: int
    tokens_after: int
    summary: str


def _tool_pairs(records):
    """Map call id -> (call seq, result seq) for the records given."""
    calls, results = {}, {}
    for record in records:
        payload = record.get("payload") or {}
        if record["type"] == S.TOOL_CALL:
            calls[payload.get("tool_call_id")] = record["seq"]
        elif record["type"] == S.ASSISTANT_MESSAGE:
            # Agent logs projected calls on assistant/message, not as a second
            # surface node. HCP017: looking only for tool/call let compaction
            # split the actual production representation from its result.
            for call in payload.get("tool_calls") or []:
                if call.get("id"):
                    calls[call["id"]] = record["seq"]
        elif record["type"] == S.TOOL_RESULT:
            results[payload.get("tool_call_id")] = record["seq"]
    return calls, results


def balance_edges(records, start_index, end_index):
    """Move span edges outward until no tool call is split from its result.

    Returns ``(start_index, end_index)`` over the ``records`` list. Only ever
    widens: narrowing would re-introduce a split at the other edge.
    """
    if not records:
        return 0, -1
    changed = True
    while changed:
        changed = False
        window = records[start_index:end_index + 1]
        calls, results = _tool_pairs(window)
        for call_id, call_seq in calls.items():
            result_seq = results.get(call_id)
            if result_seq is not None:
                continue
            # A call inside the span whose result is outside it: extend forward.
            for index, record in enumerate(records):
                payload = record.get("payload") or {}
                if record["type"] == S.TOOL_RESULT \
                        and payload.get("tool_call_id") == call_id and index > end_index:
                    end_index = index
                    changed = True
                    break
        for call_id, result_seq in results.items():
            if call_id in calls:
                continue
            # A result inside the span whose call is outside it: extend backward.
            for index in range(start_index - 1, -1, -1):
                record = records[index]
                payload = record.get("payload") or {}
                if (record["type"] == S.TOOL_CALL \
                        and payload.get("tool_call_id") == call_id) \
                        or (record["type"] == S.ASSISTANT_MESSAGE \
                        and any(call.get("id") == call_id
                                for call in payload.get("tool_calls") or [])):
                    start_index = index
                    changed = True
                    break
        if start_index == 0 and records[0]["type"] == S.SYSTEM_MESSAGE:
            # Never shadow the head system prompt; stop widening backwards.
            break
    return start_index, end_index


class CompactionEngine:
    def __init__(self, policy: CompactionPolicy, *, meter, summarize, retain_ratio=None):
        self.policy = policy
        self.meter = meter
        self.summarize = summarize
        self.retain_ratio = policy.retain_ratio if retain_ratio is None else retain_ratio
        self.history = []

    # -- policy --------------------------------------------------------
    def pressure(self, messages, *, tools=None):
        return self.meter.measure(messages, tools=tools)

    def should_compact(self, messages, *, tools=None):
        return self.pressure(messages, tools=tools) >= self.policy.trigger

    def select_region(self, session):
        """Pick the oldest span that can be condensed, keeping a recent tail.

        Returns ``(start_seq, end_seq)`` or ``None`` when nothing can be safely
        condensed (empty surface, nothing old enough, or one indivisible unit).
        """
        surface = session.surface()
        if len(surface) <= 2:
            return None
        head = surface[0]
        body = surface[1:] if head["type"] == S.SYSTEM_MESSAGE else surface
        if not body:
            return None
        # Walk back from the end accumulating tokens until the retained budget
        # is spent; everything before that boundary is a condensation candidate.
        retained = 0
        boundary = len(body)
        for index in range(len(body) - 1, -1, -1):
            record = body[index]
            retained += self.meter.measure_text(_record_text(record))
            if retained >= self.policy.retain_tokens:
                boundary = index
                break
        if boundary <= 0:
            return None
        start_index, end_index = balance_edges(body, 0, boundary - 1)
        if end_index < 0:
            return None
        start = body[start_index]["seq"]
        end = body[end_index]["seq"]
        if end - start + 1 < 2:
            return None
        session.validate_range(max(start, min(r["seq"] for r in body)), end)
        return start, end

    # -- transaction ---------------------------------------------------
    def compact(self, session, *, trigger="pressure", turn=None, step=None):
        """Condense the oldest balanced span. Returns a result or ``None``."""
        session.assert_compaction_idle()
        region = self.select_region(session)
        if region is None:
            return None
        start_seq, end_seq = region
        shadowed = [record for record in session.events()
                    if start_seq <= record["seq"] <= end_seq]
        if not shadowed:
            return None
        shadowed_tokens = sum(
            self.meter.measure_text(_record_text(record)) for record in shadowed)
        before = self.pressure(session.derive_messages())
        compaction_id = uuid4().hex
        session.append(S.COMPACTION_START, dict(
            compaction_id=compaction_id, trigger=trigger, startSeq=start_seq,
            endSeq=end_seq, shadowedTokenEstimate=shadowed_tokens,
            policy=dict(context_window=self.policy.context_window,
                        trigger=self.policy.trigger,
                        retain_tokens=self.policy.retain_tokens)),
            turn=turn, step=step)
        try:
            summary = self._summarize_span(session, shadowed, compaction_id)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            session.append(S.COMPACTION_ERROR, dict(
                compaction_id=compaction_id, error=type(exc).__name__,
                detail=str(exc)[:400]), turn=turn, step=step)
            session.append(S.COMPACTION_END, dict(
                compaction_id=compaction_id, outcome="FAILED"),
                turn=turn, step=step)
            raise
        # Revalidate: nothing may have shifted the surface while we waited.
        session.validate_range(start_seq, end_seq)
        summary_seq = session.append(S.COMPACTION_SUMMARY, dict(
            compaction_id=compaction_id, summary=summary,
            startSeq=start_seq, endSeq=end_seq,
            shadowedSeqs=[record["seq"] for record in shadowed],
            shadowedTokenEstimate=shadowed_tokens),
            turn=turn, step=step)
        session.append(S.USER_MESSAGE, dict(
            text=_framed(summary),
            surfaceOp=dict(op="replace", startSeq=start_seq, endSeq=end_seq),
            compaction_id=compaction_id),
            turn=turn, step=step)
        after = self.pressure(session.derive_messages())
        summary_seq = session.append(S.COMPACTION_END, dict(
            compaction_id=compaction_id, outcome="COMPLETE",
            tokensBeforeEstimate=before, tokensAfterEstimate=after,
            summarySeq=summary_seq), turn=turn, step=step)
        result = CompactionResult(
            compaction_id=compaction_id, start_seq=start_seq, end_seq=end_seq,
            summary_seq=summary_seq,
            shadowed_seqs=[record["seq"] for record in shadowed],
            shadowed_tokens=shadowed_tokens, tokens_after=after, summary=summary)
        self.history.append(result)
        return result

    def compact_if_needed(self, session, *, turn=None, step=None, tools=None):
        if not self.should_compact(session.derive_messages(), tools=tools):
            return None
        return self.compact(session, trigger="pressure", turn=turn, step=step)

    def _summarize_span(self, session, shadowed, compaction_id):
        transcript = []
        for record in shadowed:
            kind = record["type"]
            payload = record.get("payload") or {}
            if kind == S.USER_MESSAGE:
                transcript.append("USER: " + str(payload.get("text", "")))
            elif kind == S.ASSISTANT_MESSAGE:
                text = str(payload.get("text", ""))
                calls = payload.get("tool_calls") or []
                if calls:
                    text += " [tool calls: " + ", ".join(
                        str(call.get("name")) for call in calls) + "]"
                transcript.append("ASSISTANT: " + text)
            elif kind == S.TOOL_RESULT:
                transcript.append("TOOL RESULT (%s): %s" % (
                    payload.get("name"), str(payload.get("content", ""))[:4000]))
            elif kind == S.SKILL_LOAD:
                transcript.append("SKILL LOADED: " + str(payload.get("text", ""))[:2000])
            elif kind in (S.COMPACTION_SUMMARY,):
                transcript.append("EARLIER SUMMARY: " + str(payload.get("summary", "")))
        body = "\n\n".join(transcript)
        instruction = (
            "Condense the following earlier span of a checkpoint-reconstruction "
            "session. Preserve every fact that later reasoning could depend on: "
            "source ids, unit ids, event ids, quoted clinical text, timestamps and "
            "their precision, decisions already taken with their reasons, and any "
            "explicit uncertainty or open question. Do not summarise away an "
            "identifier. Do not add facts that are not present. Output only the "
            "condensed text.\n\n" + body)
        return self.summarize(instruction, compaction_id)


def _record_text(record):
    payload = record.get("payload") or {}
    kind = record["type"]
    if kind in (S.SYSTEM_MESSAGE, S.USER_MESSAGE, S.ASSISTANT_MESSAGE):
        return str(payload.get("text", ""))
    if kind == S.TOOL_RESULT:
        return str(payload.get("content", ""))
    if kind == S.SKILL_LOAD:
        return str(payload.get("text", ""))
    if kind == S.COMPACTION_SUMMARY:
        return str(payload.get("summary", ""))
    return json.dumps(payload, ensure_ascii=False, default=str)


def _framed(summary):
    return CHECKPOINT_PREAMBLE + "\n\n<compacted-summary>\n" + summary + "\n</compacted-summary>"