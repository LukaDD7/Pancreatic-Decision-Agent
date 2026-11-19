"""The agent loop: one turn is a sequence of steps, a step is one request plus its tools.

Shape from `anthropics/single-agent-loop-kit` (Apache-2.0) and dsh's
`core/agent-loop`, cut to the parts that earn their complexity:

* a step sends the projected history plus the tool schemas, gets back JSON with
  either ``tool_calls`` or a ``final`` payload
* tool calls execute, their results are logged, the next step sees them
* compaction is checked before each request, never mid-step, so a step's request
  bytes are never rewritten under it
* budgets are hard: turns, requests and total tokens. Hitting one is a stop
  reason, not an exception, so a partial result is still auditable
* a step that returns unparseable JSON gets one format-repair request with the
  same bytes plus the parse error; a second failure ends the turn with a reason
* **an unreadable reply is never an answer.** A reply carrying neither a tool
  call nor any text is a format failure, not "the model had nothing to say".
  The first live run (2026-11-09) recorded 35 units as ``answered`` that had
  called no tool at all: the loop read one reply shape, the model used five, and
  every unread shape fell through to "no tool calls, therefore finished". That
  turned "I could not read it" into an empty episode set that looked complete.

The reply protocol is stated in the system message (``REPLY_PROTOCOL``) and
enforced in ``_read_reply``. Stating it is not enough on its own —
a protocol the loop cannot check is a wish — and checking it is not enough
without stating it — a model cannot comply with a rule it was never told. Both
halves are needed, so they live here together.

Not included: subagents, teams, PTY, browser, background jobs, plan mode. A
checkpoint-reconstruction turn needs retrieval, evidence, and a place to write.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import session as S

STOP_ANSWERED = "answered"
STOP_MAX_TURNS = "max_turns"
STOP_REQUEST_BUDGET = "request_budget"
STOP_TOKEN_BUDGET = "token_budget"
STOP_FORMAT_FAILED = "format_failed"
STOP_REFUSAL = "refusal"
# Readable, but never declared the end of the unit. For a stage that supplied a
# finishing tool, that is "stopped mid-work", not "finished with nothing to say".
STOP_NOT_FINISHED = "not_finished"

# The reply vocabulary, stated to the model on every request. It is stated and
# also *checked*: the first live run (2026-11-09) showed the model answering in
# five shapes the loop did not read, and the loop treating every one of them as
# "no tool calls, so this unit is answered". A protocol that is only stated is a
# wish; one that is only checked is a trap. Both halves live here.
REPLY_PROTOCOL = """## 回复格式（必须遵守）
每条回复只输出一个 JSON 对象，不要包裹在代码块里，也不要附带其它文字。二选一：

调用工具：
{"tool_calls": [{"name": "工具名", "arguments": {"参数": "值"}}]}

结束本轮：
{"final": {"note": "说明"}}

`arguments` 必须是对象。每个回复只调用1个工具，完成必要的读取后尽快record_relation，再逐条继续。
JSON必须完整闭合；不要为了凑数量把许多调用写成长列表。请用上面的标准形状——
`{"skill": ...}`、`{"action": ...}`、`{"name": ..., "arguments": ...}` 这样的简写需要额外归一化。"""


@dataclass
class Budget:
    """Caps for one case, shared by every agent the case starts.

    ``max_turns`` and ``max_steps_per_turn`` bound *one turn*, so they are
    per-agent by nature. ``max_requests`` and ``max_total_tokens`` bound the
    *case*: an authorization grants 140 requests for a case, not 140 for each of
    its 37 discovery units. Passing a fresh ``Budget`` to every agent read the
    second number as the first and left a per-unit cap where a case cap was
    intended — the same shape of mistake as the frozen batch's character limit,
    which counted the wrong quantity and therefore bounded nothing.

    The spend counters mutate, so this object is deliberately not frozen and not
    copied: the harness hands the same instance to every agent it builds.
    """

    max_turns: int = 12
    max_requests: int = 60
    max_total_tokens: int = 3_000_000
    max_steps_per_turn: int = 12
    spent_requests: int = 0
    spent_tokens: int = 0

    def __post_init__(self):
        for name in ("max_turns", "max_requests", "max_total_tokens", "max_steps_per_turn"):
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise ValueError("AGENT_BUDGET_INVALID:" + name)
        for name in ("spent_requests", "spent_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError("AGENT_BUDGET_SPEND_INVALID:" + name)

    def take_request(self):
        self.spent_requests += 1

    def add_usage(self, prompt_tokens, completion_tokens):
        self.spent_tokens += prompt_tokens + completion_tokens

    def exhausted(self):
        """The stop reason if another request would exceed the case's caps."""
        if self.spent_requests >= self.max_requests:
            return STOP_REQUEST_BUDGET
        if self.spent_tokens >= self.max_total_tokens:
            return STOP_TOKEN_BUDGET
        return None

    def spent(self):
        return dict(requests=self.spent_requests, tokens=self.spent_tokens,
                    max_requests=self.max_requests,
                    max_total_tokens=self.max_total_tokens)


@dataclass
class TurnResult:
    text: str
    final: dict
    stop: str
    turns: int
    requests: int
    input_tokens: int
    output_tokens: int
    tool_calls: int
    steps: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.stop == STOP_ANSWERED


class Agent:
    """A single agent over one session log.

    ``act(turn_prompt)`` runs one turn. The caller supplies the user message;
    the agent never invents the research question.
    """

    def __init__(self, *, session, transport, tools, skills=None, compaction=None,
                 meter=None, budget=None, system_prompt=""):
        self.session = session
        self.transport = transport
        self.tools = tools
        self.skills = skills
        self.compaction = compaction
        self.meter = meter
        self.budget = budget or Budget()
        self.system_prompt = system_prompt
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.turns = 0
        self._install_system_message()
        self._install_skill_tool()

    # -- setup ---------------------------------------------------------
    def _install_system_message(self):
        existing = [record for record in self.session.surface()
                    if record["type"] == S.SYSTEM_MESSAGE]
        if existing:
            return
        text = self.system_prompt
        if self.skills is not None and self.skills.skills:
            text += (
                "\n\n可用技能（需要时用 skill 工具加载全文，不要凭名称猜内容）：\n"
                + self.skills.catalog())
        # The reply vocabulary used to be implicit: the loop read
        # ``content["tool_calls"]`` and nothing said so. Saying it costs ~200
        # tokens per unit and removes the whole class of "the model had no way
        # to know" failures.
        text += "\n\n" + REPLY_PROTOCOL
        self.session.append(S.SYSTEM_MESSAGE, dict(text=text))

    def _install_skill_tool(self):
        if self.skills is None or "skill" in self.tools:
            return
        from .tools import Tool

        registry = self.skills

        def run(name):
            """Load one skill's full instructions into this conversation."""
            skill = registry.load(name)
            self.session.append(S.SKILL_LOAD, dict(
                name=skill.name, text="[skill: %s]\n\n%s" % (skill.name, skill.body)))
            return dict(loaded=skill.name, characters=len(skill.body))

        self.tools.register(Tool(
            name="skill",
            description=("加载一个技能的全部说明。可用名称："
                         + ", ".join(registry.names())),
            input_schema=dict(type="object", additionalProperties=False,
                              required=["name"],
                              properties=dict(name=dict(
                                  type="string", enum=registry.names()))),
            run=run))

    # -- turn ----------------------------------------------------------
    def act(self, prompt, *, final_tool=None):
        """Run one turn; return when the model answers or a budget trips."""
        self.turns += 1
        turn = self.turns
        if self.turns > self.budget.max_turns:
            return TurnResult("", {}, STOP_MAX_TURNS, self.turns, self.requests,
                              self.input_tokens, self.output_tokens, 0)
        if final_tool is not None and final_tool.name not in self.tools:
            # A stage's finishing tool must be executable, not merely advertised.
            # Registering it here (rather than passing it as an extra schema) is
            # what keeps "the model was shown a tool" and "the tool can run" the
            # same fact.
            self.tools.register(final_tool)
        if self.compaction is not None:
            # The tool schemas are part of every request body, so they are part
            # of the pressure. Leaving them out is how a "character limit" ends
            # up bounding the wrong thing.
            self.compaction.compact_if_needed(
                self.session, turn=turn, tools=self.tools.schemas())
        self.session.append(S.TURN_START, dict(prompt=_preview(prompt)), turn=turn)
        self.session.append(S.USER_MESSAGE, dict(text=prompt), turn=turn)
        steps = []
        tool_calls = 0
        for step in range(1, self.budget.max_steps_per_turn + 1):
            if self.budget.exhausted() is not None:
                return self._stop(self.budget.exhausted(), "", {}, turn, steps)
            if self.compaction is not None:
                self.compaction.compact_if_needed(
                    self.session, turn=turn, step=step, tools=self.tools.schemas())
            messages = self.session.derive_messages()
            self.requests += 1
            self.budget.take_request()
            request = self._request(messages, step=step, turn=turn)
            if request["stop"] is not None:
                return self._stop(request["stop"], request.get("text", ""),
                                  request.get("final", {}), turn, steps)
            content = request["final"]
            calls = _with_ids(content.get("tool_calls") or [], turn=turn, step=step)
            text = content.get("text") or ""
            self.session.append(S.ASSISTANT_MESSAGE,
                                dict(text=text, tool_calls=calls or None),
                                turn=turn, step=step)
            if not calls:
                # Two different things can arrive here, and only one of them is
                # an answer. A `final` object is a declared end — the protocol
                # says so. Words with no declaration, in a stage that supplied a
                # finishing tool, are the model stopping mid-unit; recording
                # that as "answered" is what lets an empty result look complete.
                if "final" in content or final_tool is None:
                    return self._stop(STOP_ANSWERED, text,
                                      content.get("final") or {}, turn, steps)
                return self._stop(STOP_NOT_FINISHED, text, {}, turn, steps)
            tool_calls += len(calls)
            results = self.tools.execute(calls, turn=turn, step=step, session=self.session)
            steps.append(dict(step=step, calls=[call.get("name") for call in calls],
                              errors=sum(result.is_error for result in results)))
            self.session.append(S.STEP_END, dict(tool_calls=len(calls),
                                                 errors=sum(r.is_error for r in results)),
                                turn=turn, step=step)
            finished = [result for result in results
                        if (result.metadata or {}).get("terminal")
                        and not result.is_error]
            if finished:
                # A terminal tool (such as `finish`) ends the turn with whatever
                # the tool returned. This is a declared end, not an inferred one.
                final = dict(finished=finished[0].name,
                             content=finished[0].content)
                try:
                    parsed_final = json.loads(finished[0].content)
                except ValueError:
                    parsed_final = dict(content=finished[0].content)
                return self._stop(STOP_ANSWERED, text, parsed_final, turn, steps)
            if self.budget.exhausted() is not None:
                return self._stop(self.budget.exhausted(), "", {}, turn, steps)
        return self._stop("max_steps", "", {}, turn, steps)

    def _request(self, messages, *, step, turn):
        """One model request plus, if needed, exactly one format repair.

        A reply is *usable* only if it carries a tool call, a non-empty text, or
        a ``final`` object. Anything else — ``{"skill": "discovery"}``,
        ``{"status": "ok"}``, ``{"thinking": "..."}`` — is a shape this loop
        cannot execute, and it goes down the repair path with the shape named,
        exactly like unparseable JSON. Treating it as "no tool calls, so the
        unit is answered" is what made the first live run report 35 units as
        finished when the model had never called a single tool.
        """
        tools = self.tools.schemas()
        self.session.append(S.STEP_START, dict(
            request_index=self.requests, message_count=len(messages)),
            turn=turn, step=step)
        attempt = self._send(messages, tools, turn=turn, step=step)
        if attempt["stop"] is not None:
            return attempt
        parsed, problem, shape = _read_reply(attempt["text"], self.tools)
        recovery = _complete_tool_prefix(attempt["text"], self.tools)
        if problem is not None:
            calls = _complete_tool_calls(attempt["text"], self.tools)
            if calls and len(calls) <= self.tools.max_batch:
                # Only fully closed, schema-valid calls may be recovered. The
                # original raw response remains in the session; this is not JSON
                # repair by bracket completion and never guesses a partial tail.
                self.session.append(S.POLICY_EVENT, dict(
                    kind="complete_calls_recovered_from_unclosed_envelope",
                    recovered_calls=len(calls)), turn=turn, step=step)
                return dict(stop=None, final=dict(tool_calls=calls), text=attempt["text"])
        if problem is not None and recovery is not None:
            calls, remainder = recovery
            self.session.append(S.POLICY_EVENT, dict(
                kind="oversized_batch_prefix_recovered",
                recovered_calls=len(calls), remainder_characters=len(remainder),
                repair_request_history="prefix_summary_only"), turn=turn, step=step)
            self.session.append(S.POLICY_EVENT, dict(
                kind="oversized_batch_repair", recovered_calls=len(calls),
                maximum_batch=self.tools.max_batch), turn=turn, step=step)
            self._repair_context(turn=turn, step=step, messages=[
                dict(role="assistant", content=json.dumps(
                    dict(tool_calls=calls), ensure_ascii=False)
                    + "\n[INCOMPLETE_TOOL_CALL_OMITTED]"),
                _batch_refusal(len(calls) + 1, self.tools.max_batch),
                dict(role="user", content=(
                    _oversize_problem(len(calls) + 1, self.tools.max_batch)
                    + " 上面是完整的调用前缀和程序拒绝回执；未写完的调用不能猜测。"
                    "此前给出的调用均未执行，请给出仍需要的第一小批，不要跳过后续工作。\n\n"
                    + REPLY_PROTOCOL)),
            ])
            if self.budget.exhausted() is not None:
                return dict(stop=self.budget.exhausted(), text=attempt["text"])
            self.requests += 1
            self.budget.take_request()
            second = self._send(self.session.derive_messages(), tools,
                                turn=turn, step=step, repair=True)
            if second["stop"] is not None:
                return second
            parsed, problem, shape = _read_reply(second["text"], self.tools)
            self._note_shape(shape, turn=turn, step=step, repair=True)
            if problem is None and len(parsed.get("tool_calls") or []) > self.tools.max_batch:
                problem = ("修复回复仍有 %d 个 tool_calls，超过一次最多 %d 个。"
                           % (len(parsed["tool_calls"]), self.tools.max_batch))
            if problem is not None:
                self.session.append(S.POLICY_EVENT, dict(
                    kind="batch_repair_failed",
                    detail=problem, text=second["text"][:400]), turn=turn, step=step)
                return dict(stop=STOP_FORMAT_FAILED, text=second["text"])
            return dict(stop=None, final=parsed, text=second["text"])
        self._note_shape(shape, turn=turn, step=step)
        if problem is None and len(parsed.get("tool_calls") or []) <= self.tools.max_batch:
            return dict(stop=None, final=parsed, text=attempt["text"])
        if problem is None:
            count = len(parsed["tool_calls"])
            problem = _oversize_problem(count, self.tools.max_batch)
            self.session.append(S.POLICY_EVENT, dict(
                kind="oversized_batch", proposed_calls=count,
                maximum_batch=self.tools.max_batch, executed=False),
                turn=turn, step=step)
            self._repair_context(turn=turn, step=step, messages=[
                dict(role="assistant", content=attempt["text"]),
                _batch_refusal(count, self.tools.max_batch),
                dict(role="user", content=(
                    problem + " 给出仍需要的第一小批，不要跳过其余工作。\n\n"
                    + REPLY_PROTOCOL)),
            ])
        else:
            self._repair_context(turn=turn, step=step, messages=[
                dict(role="assistant", content=attempt["text"]),
                dict(role="user", content=(
                    problem + "\n\n" + REPLY_PROTOCOL + "\n"
                    "请按上面的格式重新给出这一次回复，不要重复解释。")),
            ])
        if self.budget.exhausted() is not None:
            return dict(stop=self.budget.exhausted(), text=attempt["text"])
        self.requests += 1
        self.budget.take_request()
        second = self._send(self.session.derive_messages(), tools,
                            turn=turn, step=step, repair=True)
        if second["stop"] is not None:
            return second
        parsed, problem, shape = _read_reply(second["text"], self.tools)
        self._note_shape(shape, turn=turn, step=step, repair=True)
        if problem is None and len(parsed.get("tool_calls") or []) > self.tools.max_batch:
            problem = _oversize_problem(len(parsed["tool_calls"]), self.tools.max_batch)
        if problem is not None:
            self.session.append(S.POLICY_EVENT, dict(
                kind="reply_unreadable", detail=problem,
                text=second["text"][:400]), turn=turn, step=step)
            return dict(stop=STOP_FORMAT_FAILED, text=second["text"])
        return dict(stop=None, final=parsed, text=attempt["text"])

    def _repair_context(self, *, turn, step, messages):
        """Persist exactly what the repair request will see."""
        for message in messages:
            kind = S.ASSISTANT_MESSAGE if message["role"] == "assistant" else S.USER_MESSAGE
            self.session.append(kind, dict(text=message["content"]),
                                turn=turn, step=step)

    def _note_shape(self, shape, *, turn, step, repair=False):
        """Record a reply that had to be normalised before it could be executed.

        A shorthand that is silently understood is indistinguishable, in the
        log, from a model that got the protocol right — so the next reader
        cannot tell whether the instruction is still needed.
        """
        if shape in ("bare_call", "wire_calls"):
            self.session.append(S.POLICY_EVENT, dict(
                kind="reply_normalized", shape=shape, repair=repair),
                turn=turn, step=step)

    def _send(self, messages, tools, *, turn, step, repair=False):
        self.session.append(S.POLICY_EVENT, dict(
            kind="request_sent", repair=repair,
            estimated_prompt_tokens=self.meter.measure(messages) if self.meter else None,
            tool_count=len(tools)), turn=turn, step=step)
        try:
            result, receipt = self.transport.complete(
                messages, tools=tools, task="agent_step")
        except Exception as exc:  # noqa: BLE001 - a failed request ends the turn
            self.session.append(S.POLICY_EVENT, dict(
                kind="request_failed", error=type(exc).__name__,
                detail=str(exc)[:300]), turn=turn, step=step)
            raise
        usage = receipt.get("usage") or {}
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
        self.input_tokens += prompt
        self.output_tokens += completion
        # The case budget is charged here, at the one place provider usage
        # actually arrives. Charging it in ``act`` would miss the format-repair
        # request, which is a real, billed request.
        self.budget.add_usage(prompt, completion)
        if self.meter is not None:
            self.meter.record_usage(usage)
        text = result.get("content") if isinstance(result, dict) else None
        if not isinstance(text, str):
            # The transport hands back the provider's content unchanged: a parsed
            # object when it was JSON, the raw string when it was not. A raw
            # string is the model's own words and must reach the repair step
            # verbatim — re-serialising it would put the failure into quotes and
            # show the model something it never wrote.
            text = result if isinstance(result, str) else json.dumps(
                result, ensure_ascii=False)
        if isinstance(result, dict) and result.get("refusal"):
            return dict(stop=STOP_REFUSAL, text=text)
        return dict(stop=None, text=text)

    def _stop(self, reason, text, final, turn, steps):
        self.session.append(S.TURN_END, dict(stop=reason, steps=len(steps)),
                            turn=turn)
        return TurnResult(text=text, final=final, stop=reason, turns=self.turns,
                          requests=self.requests, input_tokens=self.input_tokens,
                          output_tokens=self.output_tokens,
                          tool_calls=len(self.tools.calls), steps=steps)


def _batch_refusal(count, maximum):
    """A synthetic *user* receipt: an unexecuted call has no provider call id.

    This is model context for a repair request, not a claimed tool result in the
    append-only session. Assigning a made-up call id would violate the real
    assistant/tool pairing contract that the transport relies on.
    """
    return dict(role="user", content=json.dumps(dict(
        kind="BATCH_GUARD_RECEIPT", error="TOOL_BATCH_TOO_LARGE",
        proposed_calls=count, maximum_batch=maximum,
        executed=False), ensure_ascii=False))


def _oversize_problem(count, maximum):
    return ("上一条回复有 %d 个 tool_calls，超过一次最多 %d 个；本批均未执行。"
            "请用少量必要调用继续，不要一次枚举所有事件对。"
            % (count, maximum))


def _complete_tool_calls(text, tools):
    """Read only complete calls from an unclosed top-level JSON envelope."""
    if not isinstance(text, str):
        return []
    match = re.match(r'\s*\{\s*"tool_calls"\s*:\s*\[', text)
    if match is None:
        return []
    decoder, position, calls = json.JSONDecoder(), match.end(), []
    while position < len(text):
        while position < len(text) and text[position] in " \t\r\n,":
            position += 1
        if position >= len(text) or text[position] == "]":
            break
        try:
            call, end = decoder.raw_decode(text, position)
        except ValueError:
            break
        if (not isinstance(call, dict) or call.get("name") not in tools
                or not isinstance(call.get("arguments"), dict)):
            return []
        calls.append(call)
        position = end
    return calls


def _complete_tool_prefix(text, tools):
    """Read complete calls from a truncated, oversized top-level tool_calls array.

    This is *not* JSON repair by bracket completion. A call is retained only
    when ``raw_decode`` consumed its complete object, its name is registered,
    and its arguments are an object. The unfinished tail is reported, never
    guessed or executed. A single small call, even with a missing outer bracket,
    therefore does not take this path.
    """
    if not isinstance(text, str) or "tool_calls" not in text:
        return None
    try:
        decoder = json.JSONDecoder()
        match = re.match(r'\s*\{\s*"tool_calls"\s*:\s*\[', text)
        if match is None:
            return None
        position, calls = match.end(), []
        while position < len(text):
            while position < len(text) and text[position] in " \t\r\n,":
                position += 1
            if position >= len(text) or text[position] == "]":
                break
            try:
                call, end = decoder.raw_decode(text, position)
            except ValueError:
                # This is the boundary of the unfinished tail, not permission
                # to discard the complete objects already read.
                break
            if (not isinstance(call, dict)
                    or call.get("name") not in tools
                    or not isinstance(call.get("arguments"), dict)):
                return None
            calls.append(call)
            position = end
        if len(calls) > tools.max_batch and position < len(text):
            return calls, text[position:]
    except (ValueError, TypeError):
        return None
    return None


def _with_ids(calls, *, turn, step):
    """Give every tool call an id, because the result must resolve back to it.

    The model answers in this harness's own JSON vocabulary — ``tool_calls``
    with a name and an argument object, no id — and chat-completions requires
    every ``role=tool`` message to resolve to a name and a call id. Minting the
    id here, at the moment the call enters the log, keeps the log and the wire
    agreeing: assigning it only at request time would mean replaying a
    compactated history with different ids than the results were logged with.
    """
    minted = []
    for index, call in enumerate(calls or ()):
        if not isinstance(call, dict):
            raise ValueError("TOOL_CALL_NOT_OBJECT")
        call = dict(call)
        call["id"] = str(call.get("id") or "call_%d_%d_%d"
                         % (turn, step, index + 1))
        minted.append(call)
    return minted


def _read_reply(text, tools):
    """Read one model reply; return ``(content, problem, shape)``.

    ``content`` is the object the step loop consumes: ``tool_calls`` to execute,
    or ``text``/``final`` to end the turn. ``problem`` is a sentence shown back
    to the model before its one repair attempt. ``shape`` names how the reply
    was read, so the log records normalisation instead of hiding it.

    Two shapes are *normalised* rather than refused, because each has exactly
    one reading:

    * ``tool_calls: [{function: {name, arguments}}]`` — the wire shape, which a
      model may echo back
    * a bare ``{name, arguments}`` whose ``name`` is a registered tool — one call
      with no wrapper

    Everything else — ``{"skill": "discovery"}``, ``{"status": "ok"}``,
    ``{"thinking": "..."}``, or a reply with no usable field at all — is a
    problem, not an empty answer. The distinction is the whole point: the first
    live run (2026-11-09) read 43 replies, understood 3, and reported the other
    40 as units that had answered without finding anything.
    """
    parsed = _parse(text)
    if parsed is None:
        # ``_parse`` accepts only a bare object or one fenced block, so this
        # covers both "not JSON" and "JSON that is not an object".
        return None, "上一条回复不是可解析的 JSON 对象。", "unparseable"

    if isinstance(parsed.get("tool_calls"), list) and parsed["tool_calls"]:
        calls, nested = [], False
        for call in parsed["tool_calls"]:
            if isinstance(call, dict) and isinstance(call.get("function"), dict):
                inner = call["function"]
                call = dict(id=call.get("id"), name=inner.get("name"),
                            arguments=inner.get("arguments"))
                nested = True
            calls.append(call)
        return (dict(parsed, tool_calls=calls),
                None, "wire_calls" if nested else "calls")

    if isinstance(parsed.get("final"), dict):
        return dict(parsed, text=parsed.get("text") or ""), None, "final"

    name = parsed.get("name")
    if isinstance(name, str) and "arguments" in parsed and name in tools:
        # One call, no wrapper. Unambiguous: the name resolves to a tool this
        # registry can run. Anything whose name is *not* a registered tool stays
        # a problem — guessing what `{"skill": "x"}` meant is how a typo becomes
        # a silent no-op.
        return (dict(tool_calls=[dict(name=name, arguments=parsed["arguments"])]),
                None, "bare_call")

    if isinstance(parsed.get("text"), str) and parsed["text"].strip():
        # The model's own words. Not a tool call, but not silence either.
        return parsed, None, "text"

    detail = "、".join(sorted(parsed)) or "空对象"
    return None, ("上一条回复的字段是 {%s}，既不包含 tool_calls，也不包含 final 或 text，"
                  "程序无法执行。" % detail), "unreadable"


def _parse(text):
    """Accept a bare JSON object, or one wrapped in a single fenced block."""
    if not isinstance(text, str) or not text.strip():
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].lstrip()
    try:
        value = json.loads(stripped)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _preview(text, limit=200):
    return text if len(text) <= limit else text[:limit] + "…"