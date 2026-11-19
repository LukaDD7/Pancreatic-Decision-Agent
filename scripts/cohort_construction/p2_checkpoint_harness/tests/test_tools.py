"""Tool registry: batching, write gate, error surfacing, spill integration."""
import tempfile
import unittest
from pathlib import Path

from scripts.cohort_construction.p2_checkpoint_harness import session as S
from scripts.cohort_construction.p2_checkpoint_harness.spill import SpillStore
from scripts.cohort_construction.p2_checkpoint_harness.tokens import TokenMeter
from scripts.cohort_construction.p2_checkpoint_harness.tools import (
    Tool, ToolCallResult, ToolError, ToolRegistry,
)


def _tool(name, *, run=None, writes=False, schema=None):
    return Tool(name=name, description="d", input_schema=schema or dict(
        type="object", additionalProperties=False,
        properties=dict(value=dict(type="string"))),
        run=run or (lambda value="": dict(echo=value)), writes=writes)


class RegistryTests(unittest.TestCase):
    def test_HTR001_write_tool_requires_gate_at_construction(self):
        with self.assertRaises(ValueError):
            ToolRegistry(approve=None).register(_tool("w", writes=True))

    def test_HTR002_read_tool_needs_no_gate(self):
        registry = ToolRegistry()
        registry.register(_tool("r"))
        self.assertIn("r", registry)

    def test_HTR003_duplicate_name_rejected(self):
        registry = ToolRegistry()
        registry.register(_tool("r"))
        with self.assertRaises(ValueError):
            registry.register(_tool("r"))

    def test_HTR004_unknown_tool_yields_error_result_not_exception(self):
        registry = ToolRegistry()
        results = registry.execute([dict(id="1", name="nope", arguments={})])
        self.assertTrue(results[0].is_error)
        self.assertIn("UNKNOWN_TOOL", results[0].content)

    def test_HTR005_tool_raising_toolerror_surfaces_message(self):
        def run(value=""):
            raise ToolError("REFUSED_BECAUSE_REASON")
        registry = ToolRegistry()
        registry.register(_tool("r", run=run))
        results = registry.execute([dict(id="1", name="r", arguments={})])
        self.assertTrue(results[0].is_error)
        self.assertEqual(results[0].content, "REFUSED_BECAUSE_REASON")

    def test_HTR006_unexpected_exception_is_error_result(self):
        def run(value=""):
            raise RuntimeError("boom")
        registry = ToolRegistry()
        registry.register(_tool("r", run=run))
        results = registry.execute([dict(id="1", name="r", arguments={})])
        self.assertTrue(results[0].is_error)
        self.assertIn("TOOL_FAILED", results[0].content)

    def test_HTR007_batch_over_limit_refused_without_running(self):
        calls = []
        registry = ToolRegistry(max_batch=2)
        registry.register(_tool("r", run=lambda value="": calls.append(value)))
        results = registry.execute([dict(id=str(n), name="r", arguments={})
                                    for n in range(3)])
        self.assertEqual(len(results), 3)
        self.assertTrue(all(result.is_error for result in results))
        self.assertEqual(calls, [])

    def test_HTR008_result_order_matches_call_order(self):
        def make(value):
            return dict(seen=value)
        registry = ToolRegistry()
        registry.register(_tool("a", run=lambda value="": make("a:" + value)))
        registry.register(_tool("b", run=lambda value="": make("b:" + value)))
        results = registry.execute([
            dict(id="1", name="a", arguments=dict(value="x")),
            dict(id="2", name="b", arguments=dict(value="y")),
            dict(id="3", name="a", arguments=dict(value="z")),
        ])
        self.assertIn("a:x", results[0].content)
        self.assertIn("b:y", results[1].content)
        self.assertIn("a:z", results[2].content)

    def test_HTR009_writes_approver_called_with_tool_and_args(self):
        seen = []
        registry = ToolRegistry(approve=lambda tool, arguments, call_id:
                                seen.append((tool.name, arguments)))
        registry.register(_tool("w", writes=True))
        registry.execute([dict(id="1", name="w", arguments=dict(value="v"))])
        self.assertEqual(seen, [("w", dict(value="v"))])

    def test_HTR010_string_arguments_are_parsed(self):
        registry = ToolRegistry()
        registry.register(_tool("r"))
        results = registry.execute([
            dict(id="1", name="r", arguments='{"value": "parsed"}')])
        self.assertIn("parsed", results[0].content)

    def test_HTR011_non_object_arguments_rejected(self):
        registry = ToolRegistry()
        registry.register(_tool("r"))
        results = registry.execute([dict(id="1", name="r", arguments="[1,2]")])
        self.assertTrue(results[0].is_error)
        self.assertIn("TOOL_ARGUMENTS_MUST_BE_OBJECT", results[0].content)

    def test_HTR012_terminal_flag_marks_result(self):
        registry = ToolRegistry()
        tool = _tool("done")
        registry.register(Tool(name="done", description="d", input_schema=dict(
            type="object", additionalProperties=False), run=tool.run, terminal=True))
        results = registry.execute([dict(id="1", name="done", arguments={})])
        self.assertTrue(results[0].metadata.get("terminal"))


class SpillIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.session = S.Session(self.dir / "s.jsonl", session_id="c")

    def tearDown(self):
        self.session.close()
        self._tmp.cleanup()

    def test_HTR013_oversized_result_replaced_by_locator(self):
        store = SpillStore(self.dir / "spill", max_inline_tokens=10,
                           meter=TokenMeter())
        registry = ToolRegistry(spill=store)
        registry.register(_tool("big", run=lambda value="": "字" * 500))
        results = registry.execute([dict(id="1", name="big", arguments={})],
                                   session=self.session)
        self.assertIn("locator:", results[0].content)
        self.assertTrue(results[0].metadata.get("spilled"))
        self.assertEqual(len(store.written), 1)

    def test_HTR014_small_result_untouched(self):
        store = SpillStore(self.dir / "spill", max_inline_tokens=10_000,
                           meter=TokenMeter())
        registry = ToolRegistry(spill=store)
        registry.register(_tool("small", run=lambda value="": "短"))
        results = registry.execute([dict(id="1", name="small", arguments={})],
                                   session=self.session)
        self.assertNotIn("locator:", results[0].content)
        self.assertEqual(store.written, [])

    def test_HTR015_error_results_never_spilled(self):
        store = SpillStore(self.dir / "spill", max_inline_tokens=1,
                           meter=TokenMeter())
        registry = ToolRegistry(spill=store)

        def run(value=""):
            raise ToolError("BIG_ERROR " + "x" * 400)
        registry.register(_tool("err", run=run))
        results = registry.execute([dict(id="1", name="err", arguments={})],
                                   session=self.session)
        self.assertFalse(results[0].metadata.get("spilled"))
        self.assertEqual(store.written, [])

    def test_HTR016_calls_and_results_are_logged(self):
        store = SpillStore(self.dir / "spill", meter=TokenMeter())
        registry = ToolRegistry(spill=store)
        registry.register(_tool("r"))
        registry.execute([dict(id="c1", name="r", arguments=dict(value="v"))],
                         session=self.session)
        kinds = [record["type"] for record in self.session.events()]
        self.assertIn(S.TOOL_CALL, kinds)
        self.assertIn(S.TOOL_RESULT, kinds)

    def test_HTR018_registry_provides_the_reader_promised_by_every_spill_locator(self):
        from scripts.cohort_construction.p2_checkpoint_harness.harness import Harness, HarnessConfig

        class NoProvider:
            receipts = []

        harness = Harness(case_id="SPILL", run_dir=self.dir / "run",
                          cache_dir=self.dir / "cache", transport=NoProvider(),
                          config=HarnessConfig(max_inline_tool_tokens=10))
        registry = harness.new_registry()
        registry.register(_tool("big", run=lambda value="": "字" * 500))
        result = registry.execute([dict(id="1", name="big", arguments={})])[0]
        locator = result.metadata["locator"]
        read = registry.get("read_spilled").run(path=locator, start=0, end=20)
        self.assertEqual(read["text"], "字" * 20)
        with self.assertRaisesRegex(Exception, "SPILLED_PATH_MISSING"):
            registry.get("read_spilled").run(path=str(self.dir / "secret"))
        harness.close()

    def test_HTR019_spill_read_refuses_existing_path_outside_store(self):
        from scripts.cohort_construction.p2_checkpoint_harness.harness import Harness

        class NoProvider:
            receipts = []

        harness = Harness(case_id="SPILL", run_dir=self.dir / "run",
                          cache_dir=self.dir / "cache", transport=NoProvider())
        registry = harness.new_registry()
        outside = self.dir / "secret"
        outside.write_text("secret", encoding="utf-8")
        with self.assertRaisesRegex(Exception, "OUTSIDE_STORE"):
            registry.get("read_spilled").run(path=str(outside))
        harness.close()


if __name__ == "__main__":
    unittest.main()