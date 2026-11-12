"""Agent loop: step protocol, format repair, terminal tools, budgets, final tool."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.cohort_construction.p2_checkpoint_harness import session as S
from scripts.cohort_construction.p2_checkpoint_harness.agent import (
    REPLY_PROTOCOL, STOP_ANSWERED, STOP_FORMAT_FAILED, STOP_MAX_TURNS,
    STOP_NOT_FINISHED, STOP_REQUEST_BUDGET, STOP_TOKEN_BUDGET, Agent, Budget,
)
from scripts.cohort_construction.p2_checkpoint_harness.tokens import TokenMeter
from scripts.cohort_construction.p2_checkpoint_harness.tools import Tool, ToolRegistry


class StubTransport:
    """Replays canned answers; records the messages each request actually saw."""

    def __init__(self, answers, *, usage=None):
        self.answers = list(answers)
        self.seen = []
        self.calls = []
        self.usage = usage or dict(prompt_tokens=10, completion_tokens=5)

    def complete(self, messages, *, tools=None, task="agent_step", purpose=None):
        self.seen.append([dict(message) for message in messages])
        self.calls.append(dict(task=task, tools=[t["name"] for t in tools or ()],
                               purpose=purpose))
        if not self.answers:
            raise AssertionError("StubTransport ran out of answers")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return dict(content=text), dict(status="SUCCESS", usage=dict(self.usage))


class LoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.session = S.Session(self.dir / "s.jsonl", session_id="c")
        self.meter = TokenMeter()

    def tearDown(self):
        self.session.close()
        self._tmp.cleanup()

    def _agent(self, transport, registry=None, **kwargs):
        registry = registry or ToolRegistry()
        return Agent(session=self.session, transport=transport, tools=registry,
                     meter=self.meter, system_prompt="SYSTEM", **kwargs)

    def test_HAG001_plain_answer_ends_turn(self):
        transport = StubTransport([dict(final=dict(ok=True))])
        result = self._agent(transport).act("hello")
        self.assertEqual(result.stop, STOP_ANSWERED)
        self.assertEqual(result.final, dict(ok=True))

    def test_HAG002_system_message_installed_once(self):
        transport = StubTransport([dict(final={}), dict(final={})])
        agent = self._agent(transport)
        agent.act("hello")
        agent.act("again")
        heads = [record for record in self.session.surface()
                 if record["type"] == S.SYSTEM_MESSAGE]
        self.assertEqual(len(heads), 1)

    def test_HAG003_tool_call_runs_and_result_reaches_next_request(self):
        registry = ToolRegistry()
        registry.register(Tool(name="echo", description="d",
                               input_schema=dict(type="object",
                                                 additionalProperties=False,
                                                 properties=dict(value=dict(type="string"))),
                               run=lambda value="": dict(echoed=value)))
        transport = StubTransport([
            dict(tool_calls=[dict(id="c1", name="echo", arguments=dict(value="hi"))]),
            dict(final=dict(done=True)),
        ])
        result = self._agent(transport, registry).act("go")
        self.assertEqual(result.stop, STOP_ANSWERED)
        last = transport.seen[-1]
        self.assertTrue(any(message["role"] == "tool" for message in last))
        tool_message = [message for message in last if message["role"] == "tool"][0]
        self.assertIn("hi", tool_message["content"])

    def test_HAG004_unparseable_answer_gets_one_repair(self):
        transport = StubTransport(["not json at all", dict(final=dict(ok=True))])
        result = self._agent(transport).act("go")
        self.assertEqual(result.stop, STOP_ANSWERED)
        self.assertEqual(len(transport.seen), 2)
        self.assertTrue(transport.seen[1][-1]["content"].startswith("上一条回复"))

    def test_HAG005_second_format_failure_stops_turn(self):
        transport = StubTransport(["nope", "still nope"])
        result = self._agent(transport).act("go")
        self.assertEqual(result.stop, STOP_FORMAT_FAILED)

    def test_HAG006_terminal_tool_ends_turn_with_its_payload(self):
        registry = ToolRegistry()
        registry.register(Tool(name="finish", description="d",
                               input_schema=dict(type="object",
                                                 additionalProperties=False),
                               run=lambda: dict(finished=True), terminal=True))
        transport = StubTransport([dict(tool_calls=[
            dict(id="c1", name="finish", arguments={})])])
        result = self._agent(transport, registry).act("go")
        self.assertEqual(result.stop, STOP_ANSWERED)
        self.assertEqual(result.final.get("finished"), True)

    def test_HAG007_request_budget_stops_before_sending(self):
        transport = StubTransport([dict(tool_calls=[
            dict(id="c1", name="missing", arguments={})])] * 5)
        agent = self._agent(transport, budget=Budget(max_requests=1,
                                                     max_steps_per_turn=10))
        result = agent.act("go")
        self.assertEqual(result.stop, STOP_REQUEST_BUDGET)
        self.assertEqual(len(transport.seen), 1)

    def test_HAG008_step_limit_stops_turn(self):
        transport = StubTransport([dict(tool_calls=[
            dict(id="c%d" % n, name="missing", arguments={})]) for n in range(5)])
        agent = self._agent(transport, budget=Budget(max_steps_per_turn=2))
        result = agent.act("go")
        self.assertEqual(result.stop, STOP_MAX_TURNS)

    def test_HAG009_usage_accumulates_from_receipts(self):
        transport = StubTransport([dict(final={})],
                                  usage=dict(prompt_tokens=100, completion_tokens=20))
        result = self._agent(transport).act("go")
        self.assertEqual(result.input_tokens, 100)
        self.assertEqual(result.output_tokens, 20)

    def test_HAG010_turn_start_and_end_logged(self):
        transport = StubTransport([dict(final={})])
        self._agent(transport).act("go")
        kinds = [record["type"] for record in self.session.events()]
        self.assertIn(S.TURN_START, kinds)
        self.assertIn(S.TURN_END, kinds)

    def test_HAG011_skill_catalog_reaches_system_prompt(self):
        from scripts.cohort_construction.p2_checkpoint_harness.skills import SkillRegistry
        root = self.dir / "skills"
        skill_dir = root / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: 演示技能\n---\n\n正文\n", encoding="utf-8")
        transport = StubTransport([dict(final={})])
        agent = Agent(session=self.session, transport=transport,
                      tools=ToolRegistry(), skills=SkillRegistry([root]),
                      meter=self.meter, system_prompt="SYSTEM")
        agent.act("go")
        system = [message for message in transport.seen[0]
                  if message["role"] == "system"][0]["content"]
        self.assertIn("demo-skill: 演示技能", system)

    def test_HAG012_skill_tool_loads_body_into_history(self):
        from scripts.cohort_construction.p2_checkpoint_harness.skills import SkillRegistry
        root = self.dir / "skills"
        skill_dir = root / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: 演示技能\n---\n\n技能正文内容\n",
            encoding="utf-8")
        transport = StubTransport([
            dict(tool_calls=[dict(id="c1", name="skill",
                                  arguments=dict(name="demo-skill"))]),
            dict(final={}),
        ])
        agent = Agent(session=self.session, transport=transport,
                      tools=ToolRegistry(), skills=SkillRegistry([root]),
                      meter=self.meter, system_prompt="SYSTEM")
        agent.act("go")
        joined = "\n".join(message["content"] for message in transport.seen[-1])
        self.assertIn("技能正文内容", joined)

    def test_HAG013_compaction_runs_before_a_request_when_pressured(self):
        from scripts.cohort_construction.p2_checkpoint_harness.compaction import (
            CompactionEngine, CompactionPolicy,
        )
        policy = CompactionPolicy(context_window=2_000, output_reservation=200,
                                  headroom_tokens=100, retain_ratio=0.1)
        compactions = []

        def summarize(instruction, compaction_id):
            compactions.append(compaction_id)
            return "压缩后"

        engine = CompactionEngine(policy, meter=self.meter, summarize=summarize)
        transport = StubTransport([dict(final=dict(ok=True))])
        agent = Agent(session=self.session, transport=transport,
                      tools=ToolRegistry(), meter=self.meter,
                      compaction=engine, system_prompt="S" * 3000)
        # Compaction condenses an earlier span; it never splits one record, so a
        # surface of only [system, current prompt] has nothing it may replace.
        for index in range(3):
            self.session.append(S.USER_MESSAGE, dict(text="早期问题%d" % index))
            self.session.append(S.ASSISTANT_MESSAGE, dict(text="早期回答%d" % index))
        agent.act("这" * 4000)
        self.assertTrue(compactions)
        seen = "\n".join(message["content"] for message in transport.seen[-1])
        self.assertIn("<compacted-summary>", seen)
        self.assertNotIn("早期问题0", seen)
        # The shadowed records stay readable from the log itself.
        texts = [record["payload"].get("text") for record in self.session.events()
                 if record["type"] == S.USER_MESSAGE]
        self.assertIn("早期问题0", texts)

    def test_HAG014_failed_request_is_logged_and_reraised(self):
        transport = StubTransport([RuntimeError("provider down")])
        agent = self._agent(transport)
        with self.assertRaises(RuntimeError):
            agent.act("go")
        kinds = [record["payload"].get("kind") for record in self.session.events()
                 if record["type"] == S.POLICY_EVENT]
        self.assertIn("request_failed", kinds)

    def test_HAG015_a_shared_budget_caps_the_whole_case_not_each_agent(self):
        # The negative direction: a fresh Budget per agent would let three
        # agents spend three times the granted requests and each would look
        # compliant on its own.
        budget = Budget(max_requests=3, max_total_tokens=100_000, max_turns=4)
        transport = StubTransport([dict(tool_calls=[
            dict(id="c1", name="missing", arguments={})])] * 5)
        first = self._agent(transport, budget=budget).act("go")
        second = self._agent(transport, budget=budget).act("go")
        # The first agent spends the whole case allowance; the second is
        # refused before sending, because the request belongs to the case.
        self.assertEqual(first.stop, STOP_REQUEST_BUDGET)
        self.assertEqual(second.stop, STOP_REQUEST_BUDGET)
        self.assertEqual(len(transport.seen), 3)
        self.assertEqual(budget.spent_requests, 3)

    def test_HAG016_token_spend_is_charged_where_usage_arrives(self):
        budget = Budget(max_total_tokens=30, max_requests=50)
        transport = StubTransport([dict(tool_calls=[
            dict(id="c1", name="missing", arguments={})])] * 3,
            usage=dict(prompt_tokens=10, completion_tokens=5))
        result = self._agent(transport, budget=budget).act("go")
        # 15 tokens per request: the second request crosses 30 and the turn
        # stops before a third is sent.
        self.assertEqual(result.stop, STOP_TOKEN_BUDGET)
        self.assertEqual(len(transport.seen), 2)
        self.assertEqual(budget.spent_tokens, 30)

    def test_HAG017_a_format_repair_request_is_also_charged(self):
        budget = Budget(max_requests=2, max_total_tokens=100_000)
        transport = StubTransport(["不是 JSON", dict(tool_calls=[
            dict(id="c1", name="missing", arguments={})])])
        agent = self._agent(transport, budget=budget)
        result = agent.act("go")
        # A repair is a billed request; leaving it out of the ledger is how a
        # case overspends while every agent reports it stayed inside its cap.
        self.assertEqual(budget.spent_requests, 2)
        self.assertEqual(len(transport.seen), 2)
        self.assertIn(result.stop, (STOP_MAX_TURNS, STOP_REQUEST_BUDGET))

    # -- reply protocol, from the r4 live-run defect --------------------
    def _echo_registry(self):
        registry = ToolRegistry()
        registry.register(Tool(
            name="echo", description="d",
            input_schema=dict(type="object", properties=dict(v=dict(type="string"))),
            run=lambda v="": dict(echoed=v)))
        return registry

    def _finish_tool(self):
        return Tool(name="finish", description="d", input_schema=dict(type="object"),
                    run=lambda: dict(finished=True), terminal=True)

    def test_HAG018_bare_named_tool_call_is_executed_not_mistaken_for_an_answer(self):
        # The negative direction, and the exact shape the model used 26 times in
        # r4: if the loop only accepts content["tool_calls"], this becomes
        # "answered without calling anything", and the unit is silently empty.
        transport = StubTransport([
            dict(name="echo", arguments=dict(v="hi")),
            dict(final=dict(note="done")),
        ])
        agent = self._agent(transport, self._echo_registry())
        result = agent.act("go", final_tool=self._finish_tool())
        self.assertEqual(result.stop, STOP_ANSWERED)
        self.assertEqual(agent.tools.calls[0]["name"], "echo")
        self.assertTrue(any(message["role"] == "tool"
                            for message in transport.seen[-1]))
        # Normalisation is visible in the log, not silently accepted.
        kinds = [record["payload"].get("kind")
                 for record in self.session.events(kind=S.POLICY_EVENT)]
        self.assertIn("reply_normalized", kinds)

    def test_HAG019_unreadable_shape_is_repaired_once_then_fails_not_answered(self):
        # r4 used {"skill": ...} six times; treating it as "no tool calls" made
        # the unit look complete. A registered tool is not a reason to guess.
        transport = StubTransport([dict(skill="discovery"), dict(skill="discovery")])
        result = self._agent(transport).act("go", final_tool=self._finish_tool())
        self.assertEqual(result.stop, STOP_FORMAT_FAILED)
        self.assertEqual(len(transport.seen), 2)
        self.assertIn("skill", transport.seen[-1][-1]["content"])
        self.assertIn("tool_calls", transport.seen[-1][-1]["content"])

    def test_HAG020_a_declared_final_is_an_answer_but_plain_words_without_finish_are_not(self):
        # A stage that supplied finish() has stated how its work ends. A readable
        # reply that never calls it is the model stopping mid-unit.
        words = self._agent(StubTransport([dict(text="看过了，没有事件。")]))
        result = words.act("go", final_tool=self._finish_tool())
        self.assertEqual(result.stop, STOP_NOT_FINISHED)
        declared = self._agent(StubTransport([dict(final=dict(note="零事件"))]))
        result = declared.act("go", final_tool=self._finish_tool())
        self.assertEqual(result.stop, STOP_ANSWERED)

    def test_HAG021_the_reply_protocol_reaches_the_model(self):
        transport = StubTransport([dict(final={})])
        self._agent(transport).act("go")
        system = [message for message in transport.seen[0]
                  if message["role"] == "system"][0]["content"]
        self.assertIn(REPLY_PROTOCOL, system)
        self.assertIn("tool_calls", system)
        self.assertIn("final", system)

    def test_HAG022_truncated_oversized_batch_repairs_only_its_complete_prefix(self):
        # Live r6 counterexample: a truncated 20,143-character reply held 162
        # complete compare_clocks calls plus an incomplete one. Do not execute
        # all 162, guess the unfinished call, or discard the readable prefix.
        calls = [dict(name="echo", arguments=dict(v=str(n))) for n in range(17)]
        raw = json.dumps(dict(tool_calls=calls), ensure_ascii=False)
        raw = raw[:-2] + ', {"name": "echo", "arguments":'
        transport = StubTransport([
            raw,
            dict(tool_calls=[dict(name="echo", arguments=dict(v="16"))]),
            dict(tool_calls=[dict(name="finish", arguments={})]),
        ])
        agent = self._agent(transport, self._echo_registry())
        result = agent.act("go", final_tool=self._finish_tool())
        self.assertEqual(result.stop, STOP_ANSWERED)
        self.assertEqual(len(transport.seen), 3)
        self.assertEqual([(call["name"], call["arguments"].get("v"))
                          for call in agent.tools.calls],
                         [("echo", "16"), ("finish", None)])
        self.assertEqual(transport.seen[1][-2]["role"], "user")
        self.assertIn("TOOL_BATCH_TOO_LARGE", transport.seen[1][-2]["content"])
        self.assertIn("INCOMPLETE_TOOL_CALL_OMITTED",
                      transport.seen[1][-3]["content"])
        repair = transport.seen[1][-1]["content"]
        self.assertIn("最多 16", repair)
        prefix = json.loads(transport.seen[1][-3]["content"].split(
            "\n[INCOMPLETE_TOOL_CALL_OMITTED]", 1)[0])["tool_calls"]
        self.assertEqual(len(prefix), 17)
        self.assertEqual(prefix[0]["arguments"]["v"], "0")
        self.assertIn("INCOMPLETE_TOOL_CALL_OMITTED",
                      transport.seen[1][-3]["content"])
        self.assertTrue(all(call["name"] == "echo" for call in prefix))
        kinds = [record["payload"].get("kind")
                 for record in self.session.events(kind=S.POLICY_EVENT)]
        self.assertIn("oversized_batch_prefix_recovered", kinds)

    def test_HAG023_a_repaired_prefix_may_not_smuggle_in_another_oversized_batch(self):
        calls = [dict(name="echo", arguments=dict(v=str(n))) for n in range(17)]
        raw = json.dumps(dict(tool_calls=calls), ensure_ascii=False)[:-1]
        transport = StubTransport([raw, dict(tool_calls=calls)])
        agent = self._agent(transport, self._echo_registry())
        result = agent.act("go", final_tool=self._finish_tool())
        self.assertEqual(result.stop, STOP_FORMAT_FAILED)
        self.assertEqual(agent.tools.calls, [])

    def test_HAG024_a_format_repair_never_exceeds_the_shared_request_budget(self):
        agent = self._agent(StubTransport(["not json"]), budget=Budget(
            max_requests=1, max_total_tokens=1_000_000))
        result = agent.act("go")
        self.assertEqual(result.stop, STOP_REQUEST_BUDGET)
        self.assertEqual(agent.budget.spent_requests, 1)
        self.assertEqual(len(agent.transport.seen), 1)


if __name__ == "__main__":
    unittest.main()