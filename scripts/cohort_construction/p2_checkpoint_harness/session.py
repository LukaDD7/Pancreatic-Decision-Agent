"""Append-only session log; the model's history is projected from it, never kept separately.

Two rules, both taken from the DeepSeek Harness design because they fix concrete
defects in the current workflow:

1. **Model-visible means logged.** Anything that reaches a provider request must
   first exist as a durable event, so a request can be reconstructed after the
   fact from the log alone. The current pipeline rebuilds prompts inline; there
   is no record of what the model actually saw, which is why character ceilings
   could silently drop whole source ranges without anything in the output
   saying so.
2. **A compaction replaces a range, it does not rewrite history.** The summary is
   one `user/message` carrying `surfaceOp={op:'replace', startSeq, endSeq}`. The
   shadowed events stay in the log forever.

Events are written with `O_EXCL`-style durability through a single writer, and
the file is JSON Lines so a partial write is detectable rather than silently
truncating the record.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

FORMAT = "p2-harness-session-0.1.0"

# Durable event kinds. Anything not in this set is a programming error.
SESSION_EVENT = "session/event"
TURN_START = "turn/start"
TURN_END = "turn/end"
STEP_START = "step/start"
STEP_END = "step/end"
USER_MESSAGE = "user/message"
SYSTEM_MESSAGE = "system/message"
ASSISTANT_MESSAGE = "assistant/message"
TOOL_CALL = "tool/call"
TOOL_RESULT = "tool/result"
SKILL_LOAD = "skill/load"
COMPACTION_START = "compaction/start"
COMPACTION_SUMMARY = "compaction/summary"
COMPACTION_END = "compaction/end"
COMPACTION_ERROR = "compaction/summary-error"
POLICY_EVENT = "policy/event"
STAGE_EVENT = "stage/event"

_ALLOWED = frozenset({
    TURN_START, TURN_END, STEP_START, STEP_END, USER_MESSAGE, SYSTEM_MESSAGE,
    ASSISTANT_MESSAGE, TOOL_CALL, TOOL_RESULT, SKILL_LOAD, COMPACTION_START,
    COMPACTION_SUMMARY, COMPACTION_END, COMPACTION_ERROR, POLICY_EVENT, STAGE_EVENT,
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wire_call(call):
    """One internal tool call in chat-completions shape.

    ``arguments`` is a JSON *string* on the wire. The internal form may hold a
    dict (as the provider's own tool-call parsing gives) or a string (as it
    arrives from the model's JSON answer); both are normalised here so the
    history the model sees back is well formed either way.
    """
    arguments = call.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {},
                               ensure_ascii=False)
    return dict(id=call.get("id") or "",
                type="function",
                function=dict(name=call.get("name") or "", arguments=arguments))


class SessionClosed(RuntimeError):
    """Appending after `close()` is a bug, not a retry condition."""


class CompactionLocked(RuntimeError):
    """An unmatched `compaction/start` exists: a prior compaction never finished.

    A crash between start and end must leave *evidence*, not a session that looks
    healthy. Recovery is explicit (`abandon_stale_compaction`) and is itself
    logged, so the log never claims a compaction completed when it did not.
    """


class Session:
    """Append-only log with a projection to model messages.

    ``messages`` is not stored. It is derived from the log every time, applying
    every recorded surface replacement. That is what keeps "what the model saw"
    and "what the log says" the same object.
    """

    def __init__(self, path, *, session_id, metadata=None, resume=False):
        self.path = Path(path)
        self.session_id = session_id
        self._events = []
        self._closed = False
        self._fh = None
        if resume and self.path.is_file():
            self._load()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            header = dict(
                format=FORMAT, session_id=session_id, created_at=_now(),
                metadata=dict(metadata or {}),
            )
            # Truncating an existing session is a destructive act; refuse it.
            if self.path.exists() and self.path.stat().st_size:
                raise FileExistsError(
                    "SESSION_PATH_IN_USE:" + str(self.path))
            self.path.write_text(
                json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8")
        self._fh = open(self.path, "a", encoding="utf-8")

    # -- lifecycle -----------------------------------------------------
    def _load(self):
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if not lines:
            raise ValueError("SESSION_LOG_EMPTY:" + str(self.path))
        header = json.loads(lines[0])
        if header.get("format") != FORMAT:
            raise ValueError("SESSION_FORMAT_UNSUPPORTED")
        if header.get("session_id") != self.session_id:
            raise ValueError("SESSION_ID_MISMATCH")
        for line in lines[1:]:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("type") not in _ALLOWED:
                raise ValueError("SESSION_EVENT_TYPE_UNKNOWN:" + str(record.get("type")))
            self._events.append(record)

    def close(self):
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            finally:
                self._fh.close()
            self._fh = None
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- append --------------------------------------------------------
    def append(self, event_type, payload=None, *, turn=None, step=None, **extra):
        if self._closed:
            raise SessionClosed("SESSION_CLOSED")
        if event_type not in _ALLOWED:
            raise ValueError("SESSION_EVENT_TYPE_UNKNOWN:" + str(event_type))
        record = dict(
            seq=len(self._events) + 1, type=event_type, at=_now(),
            turn=turn, step=step, payload=payload if payload is not None else dict(),
        )
        record.update(extra)
        self._events.append(record)
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()
        return record["seq"]

    # -- read ----------------------------------------------------------
    def events(self, *, kind=None, since_seq=0):
        for record in self._events:
            if record["seq"] <= since_seq:
                continue
            if kind is not None and record["type"] != kind:
                continue
            yield record

    def last(self, kind):
        for record in reversed(self._events):
            if record["type"] == kind:
                return record
        return None

    def get(self, seq):
        if not isinstance(seq, int) or not 1 <= seq <= len(self._events):
            raise KeyError("SESSION_SEQ_OUT_OF_RANGE:" + str(seq))
        return self._events[seq - 1]

    def __len__(self):
        return len(self._events)

    # -- projection ----------------------------------------------------
    def shadowed_ranges(self):
        """Return ``[(startSeq, endSeq, summarySeq)]`` in append order."""
        ranges = []
        for record in self._events:
            if record["type"] != USER_MESSAGE:
                continue
            operation = (record.get("payload") or {}).get("surfaceOp")
            if isinstance(operation, dict) and operation.get("op") == "replace":
                ranges.append((operation.get("startSeq"), operation.get("endSeq"), record["seq"]))
        return ranges

    def _shadowed_seqs(self):
        shadowed = set()
        for start, end, _ in self.shadowed_ranges():
            if not isinstance(start, int) or not isinstance(end, int):
                raise ValueError("COMPACTION_SURFACE_OP_INVALID")
            for seq in range(start, end + 1):
                shadowed.add(seq)
        return shadowed

    def surface(self):
        """Ordered surface nodes: the log minus every replaced range.

        `system/message` at the head is retained; a summary that would shadow it
        is rejected at write time (`validate_range`), so this projection does not
        need a special case.
        """
        shadowed = self._shadowed_seqs()
        return [record for record in self._events if record["seq"] not in shadowed]

    def derive_messages(self):
        """Project the provider-facing message list from the surface.

        Tool calls are stored in the log in the shape this harness reasons about
        (``{id, name, arguments}``) and projected into the chat-completions wire
        shape on the way out. The two are not the same object: the provider
        requires ``tool_calls[].type`` and a nested ``function``, and requires
        every ``role=tool`` message to resolve to a tool name and a call id.
        Sending the internal shape was refused with HTTP 400 on the first live
        run (2026-11-09), and the log should not have to carry two vocabularies
        just because one request did.
        """
        messages = []
        for record in self.surface():
            kind = record["type"]
            payload = record.get("payload") or {}
            if kind == SYSTEM_MESSAGE:
                messages.append(dict(role="system", content=payload.get("text", "")))
            elif kind == USER_MESSAGE:
                messages.append(dict(role="user", content=payload.get("text", "")))
            elif kind == ASSISTANT_MESSAGE:
                message = dict(role="assistant", content=payload.get("text", ""))
                calls = payload.get("tool_calls")
                if calls:
                    message["tool_calls"] = [_wire_call(call) for call in calls]
                messages.append(message)
            elif kind == TOOL_RESULT:
                messages.append(dict(
                    role="tool", tool_call_id=payload.get("tool_call_id"),
                    name=payload.get("name"),
                    content=payload.get("content", "")))
            elif kind == SKILL_LOAD:
                # A skill body enters history as an ordinary user message so it
                # compacts and replays like any other content.
                messages.append(dict(role="user", content=payload.get("text", "")))
        return messages

    # -- compaction helpers --------------------------------------------
    @staticmethod
    def compaction_id(record):
        """Compaction identity lives in the payload; read it in one place."""
        payload = record.get("payload") or {}
        return payload.get("compaction_id") or record.get("compaction_id")

    def active_compaction(self):
        """Return the open `compaction/start` if any, else None."""
        open_start = None
        for record in self._events:
            if record["type"] == COMPACTION_START:
                if open_start is not None:
                    # A second start without a close is the same failure.
                    continue
                open_start = record
            elif record["type"] == COMPACTION_END:
                if open_start is not None and self.compaction_id(record) \
                        == self.compaction_id(open_start):
                    open_start = None
        return open_start

    def assert_compaction_idle(self):
        active = self.active_compaction()
        if active is not None:
            raise CompactionLocked(
                "COMPACTION_IN_FLIGHT:" + str(self.compaction_id(active)))

    def abandoned_compactions(self):
        """Starts that never closed: evidence of an interrupted compaction."""
        started = {self.compaction_id(record): record for record in self._events
                   if record["type"] == COMPACTION_START}
        closed = {self.compaction_id(record) for record in self._events
                  if record["type"] == COMPACTION_END}
        return [record for cid, record in started.items() if cid not in closed]

    def abandon_stale_compaction(self, compaction_id, *, reason):
        """Close an orphaned start explicitly; never silently forget it."""
        if not any(record["type"] == COMPACTION_START
                   and self.compaction_id(record) == compaction_id
                   for record in self._events):
            raise KeyError("COMPACTION_ID_UNKNOWN:" + str(compaction_id))
        return self.append(COMPACTION_END, dict(
            compaction_id=compaction_id, abandoned=True,
            reason=str(reason)[:500]))

    def validate_range(self, start_seq, end_seq):
        """A compaction may never shadow the head system message."""
        surface = self.surface()
        if not surface:
            raise ValueError("SESSION_SURFACE_EMPTY")
        head = surface[0]
        if head["type"] == SYSTEM_MESSAGE and start_seq <= head["seq"] <= end_seq:
            raise ValueError("COMPACTION_WOULD_SHADOW_SYSTEM_HEAD")
        return True