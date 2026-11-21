"""Tool registry with a guarded execution pipeline.

Shape taken from dsh's `ctx.tools` seam and the single-agent-loop reference,
reduced to what we need:

* ``Tool(name, description, input_schema, run, writes=False)``
* reads run in parallel within a batch, writes run serially and only after reads
* every call and result is logged before it can influence the next request
* a tool that writes requires an approval callback at registry construction; a
  missing gate raises rather than defaulting to "allow"
* a call that raises yields an ``is_error`` result the model can see, so one bad
  call never kills the turn

Deliberately absent: subagents, agent teams, terminal/PTY, browser use, plugin
market, background jobs. None of them serve checkpoint reconstruction, and each
would add a failure mode without removing work.
"""
from __future__ import annotations

import json
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    run: object
    writes: bool = False
    deferred: bool = False
    terminal: bool = False
    requires_prior_tool_results: bool = False

    def schema(self):
        return dict(name=self.name, description=self.description,
                    input_schema=self.input_schema)


class ToolError(RuntimeError):
    """A tool refused the call. The message reaches the model verbatim."""


@dataclass
class ToolCallResult:
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False
    metadata: dict = field(default_factory=dict)


class ToolRegistry:
    def __init__(self, *, approve=None, max_batch=16, parallel_reads=4, spill=None):
        if not isinstance(max_batch, int) or max_batch < 1:
            raise ValueError("TOOL_MAX_BATCH_INVALID")
        self._tools = {}
        self._approve = approve
        self.max_batch = max_batch
        self.parallel_reads = max(1, int(parallel_reads))
        self.spill = spill
        self.calls = []
        self.result_observers = []

    # -- registration --------------------------------------------------
    def register(self, tool: Tool):
        if tool.name in self._tools:
            raise ValueError("TOOL_ALREADY_REGISTERED:" + tool.name)
        if tool.writes and self._approve is None:
            # The write gate must be present at construction time. A registry
            # that can be built without one will eventually be built without
            # one in the wrong place.
            raise ValueError("TOOL_WRITE_GATE_REQUIRED:" + tool.name)
        self._tools[tool.name] = tool
        return tool

    def get(self, name):
        return self._tools.get(name)

    def __contains__(self, name):
        return name in self._tools

    def schemas(self, *, include_deferred=False):
        return [tool.schema() for tool in self._tools.values()
                if include_deferred or not tool.deferred]

    def names(self):
        return sorted(self._tools)

    # -- execution -----------------------------------------------------
    def execute(self, calls, *, turn=None, step=None, session=None):
        """Run one batch. Order of returned results matches ``calls``.

        Reads are dispatched together; writes are executed afterwards, one at a
        time, in the order the model emitted them. Tools marked
        ``requires_prior_tool_results`` cannot share a batch with reads: their
        arguments must be chosen after the model has received those results.
        """
        calls = list(calls or ())
        if len(calls) > self.max_batch:
            results = [ToolCallResult(
                tool_call_id=call.get("id", ""), name=call.get("name", ""),
                content=("TOOL_BATCH_TOO_LARGE: %d calls, maximum is %d. "
                         "Split the batch and retry." % (len(calls), self.max_batch)),
                is_error=True) for call in calls]
            for call, result in zip(calls, results):
                # A refused batch is still a request the model made; the log must
                # show the call as well as the refusal, or the transcript implies
                # nothing was attempted.
                self._log(session, call, result, turn=turn, step=step)
            return results

        reads, writes = [], []
        for index, call in enumerate(calls):
            tool = self._tools.get(call.get("name"))
            if tool is not None and tool.writes:
                writes.append((index, call, tool))
            else:
                reads.append((index, call, tool))

        results = [None] * len(calls)
        if reads:
            if self.parallel_reads == 1 or len(reads) == 1:
                for index, call, tool in reads:
                    results[index] = self._run_one(call, tool, turn=turn, step=step, session=session)
            else:
                with ThreadPoolExecutor(max_workers=self.parallel_reads) as pool:
                    futures = {pool.submit(self._run_one, call, tool, turn=turn,
                                           step=step, session=session): index
                               for index, call, tool in reads}
                    for future, index in futures.items():
                        results[index] = future.result()
        for index, call, tool in writes:
            if reads and tool.requires_prior_tool_results:
                result = ToolCallResult(
                    call.get("id") or "", call.get("name") or "",
                    "TOOL_REQUIRES_RESULTS_FROM_PREVIOUS_BATCH: 先取得读取结果，"
                    "再在后续工具批次单独提交本操作。", is_error=True)
                self._log(session, call, result, turn=turn, step=step)
                results[index] = result
                continue
            results[index] = self._run_one(call, tool, turn=turn, step=step, session=session)
        return results

    def _run_one(self, call, tool, *, turn, step, session):
        call_id = call.get("id") or ""
        name = call.get("name") or ""
        record = dict(tool_call_id=call_id, name=name, arguments=call.get("arguments"))
        self.calls.append(record)
        if tool is None:
            result = ToolCallResult(call_id, name,
                "UNKNOWN_TOOL: %r is not available. Available tools: %s"
                % (name, ", ".join(self.names())), is_error=True)
            self._log(session, call, result, turn=turn, step=step)
            return result
        try:
            arguments = call.get("arguments")
            if isinstance(arguments, str):
                arguments = json.loads(arguments) if arguments.strip() else {}
            if not isinstance(arguments, dict):
                raise ToolError("TOOL_ARGUMENTS_MUST_BE_OBJECT")
            if tool.writes:
                self._approve(tool, arguments, call_id)
            output = tool.run(**(arguments or {}))
        except ToolError as exc:
            result = ToolCallResult(call_id, name, str(exc), is_error=True)
        except TypeError as exc:
            result = ToolCallResult(call_id, name,
                "TOOL_ARGUMENT_ERROR: %s. Schema: %s"
                % (exc, json.dumps(tool.input_schema, ensure_ascii=False)), is_error=True)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model, not swallowed
            result = ToolCallResult(call_id, name,
                "TOOL_FAILED: %s: %s" % (type(exc).__name__, exc), is_error=True,
                metadata=dict(traceback=traceback.format_exc(limit=3)))
        else:
            result = output if isinstance(output, ToolCallResult) else \
                ToolCallResult(call_id, name, _stringify(output))
        if tool.terminal:
            # A terminal tool ends the step's turn. The loop reads this flag
            # rather than guessing from the tool's name.
            result.metadata = dict(result.metadata or {}, terminal=True)
        result = self._spill(result)
        for observer in self.result_observers:
            observer(call, result)
        self._log(session, call, result, turn=turn, step=step)
        return result

    def _spill(self, result):
        """Replace an oversized result with a preview plus a locator."""
        if self.spill is None or result.is_error:
            return result
        return self.spill.apply(result)

    def _log(self, session, call, result, *, turn, step):
        if session is None:
            return
        session.append("tool/call", dict(
            tool_call_id=result.tool_call_id, name=call.get("name"),
            arguments=call.get("arguments")), turn=turn, step=step)
        session.append("tool/result", dict(
            tool_call_id=result.tool_call_id, name=result.name,
            content=result.content, is_error=result.is_error,
            metadata=_trim_metadata(result.metadata)), turn=turn, step=step)


def _trim_metadata(metadata):
    """Keep metadata small; it must not become a second hidden payload."""
    if not metadata:
        return {}
    text = json.dumps(metadata, ensure_ascii=False, default=str)
    if len(text) <= 2000:
        return dict(metadata)
    return dict(truncated=True, characters=len(text))


def _stringify(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)