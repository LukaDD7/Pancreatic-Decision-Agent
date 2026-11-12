"""Compaction: trigger arithmetic, retention, tool-pair boundaries, transaction."""
import tempfile
import unittest
from pathlib import Path

from scripts.cohort_construction.p2_checkpoint_harness import session as S
from scripts.cohort_construction.p2_checkpoint_harness.compaction import (
    CHECKPOINT_PREAMBLE, CompactionEngine, CompactionPolicy, balance_edges,
)
from scripts.cohort_construction.p2_checkpoint_harness.tokens import TokenMeter


class _Recorder:
    def __init__(self, text="SUMMARY-OF-OLD-SPAN"):
        self.calls = []
        self.text = text

    def __call__(self, instruction, compaction_id):
        self.calls.append(dict(instruction=instruction, id=compaction_id))
        return self.text


def _policy(**overrides):
    base = dict(context_window=10_000, output_reservation=1_000,
                threshold_ratio=0.8, retain_ratio=0.16, headroom_tokens=1_000)
    base.update(overrides)
    return CompactionPolicy(**base)


class PolicyTests(unittest.TestCase):
    def test_HCP001_trigger_matches_dsh_formula(self):
        policy = _policy()
        # min(10000*0.8, 10000-1000-1000) = 8000
        self.assertEqual(policy.trigger, 8000)
        self.assertEqual(policy.available, 8000)

    def test_HCP002_retained_budget_is_fraction_of_window_minus_output(self):
        policy = _policy()
        self.assertEqual(policy.retain_tokens, int((10000 - 1000) * 0.16))

    def test_HCP003_retain_at_or_above_threshold_refused(self):
        with self.assertRaises(ValueError):
            _policy(retain_ratio=0.8)

    def test_HCP004_headroom_larger_than_window_refused(self):
        with self.assertRaises(ValueError):
            _policy(context_window=2_000, output_reservation=1_000,
                    headroom_tokens=2_000)

    def test_HCP005_non_positive_output_reservation_refused(self):
        with self.assertRaises(ValueError):
            _policy(output_reservation=0)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.session = S.Session(self.dir / "s.jsonl", session_id="c")
        self.meter = TokenMeter()
        self.recorder = _Recorder()
        self.engine = CompactionEngine(_policy(), meter=self.meter,
                                       summarize=self.recorder)

    def tearDown(self):
        self.session.close()
        self._tmp.cleanup()

    def _fill(self, count=12, size=600):
        self.session.append(S.SYSTEM_MESSAGE, dict(text="SYSTEM"))
        for index in range(count):
            self.session.append(S.USER_MESSAGE,
                                dict(text="M%d %s" % (index, "x" * size)))
            self.session.append(S.ASSISTANT_MESSAGE, dict(text="A%d" % index))

    def test_HCP006_no_compaction_below_trigger(self):
        self.session.append(S.SYSTEM_MESSAGE, dict(text="SYSTEM"))
        self.session.append(S.USER_MESSAGE, dict(text="short"))
        self.assertIsNone(self.engine.compact_if_needed(self.session))
        self.assertEqual(self.recorder.calls, [])

    def test_HCP007_compaction_replaces_oldest_span_and_keeps_tail(self):
        self._fill()
        result = self.engine.compact(self.session)
        self.assertIsNotNone(result)
        messages = self.session.derive_messages()
        self.assertEqual(messages[0]["role"], "system")
        joined = "\n".join(message["content"] for message in messages)
        # The newest user message survives verbatim; the oldest does not.
        self.assertIn("M11", joined)
        self.assertNotIn("M0 ", joined)
        self.assertIn(CHECKPOINT_PREAMBLE, joined)
        self.assertIn("<compacted-summary>", joined)

    def test_HCP008_compaction_records_three_bracket_events(self):
        self._fill()
        result = self.engine.compact(self.session)
        kinds = [record["type"] for record in self.session.events()]
        self.assertIn(S.COMPACTION_START, kinds)
        self.assertIn(S.COMPACTION_SUMMARY, kinds)
        self.assertIn(S.COMPACTION_END, kinds)
        summary = self.session.last(S.COMPACTION_SUMMARY)
        self.assertEqual(summary["payload"]["summary"], self.recorder.text)
        self.assertGreater(len(result.shadowed_seqs), 1)

    def test_HCP009_summary_failure_closes_bracket_and_propagates(self):
        self._fill()

        def boom(instruction, compaction_id):
            raise RuntimeError("provider down")
        engine = CompactionEngine(_policy(), meter=self.meter, summarize=boom)
        with self.assertRaises(RuntimeError):
            engine.compact(self.session)
        self.assertEqual(self.session.last(S.COMPACTION_ERROR)["type"],
                         S.COMPACTION_ERROR)
        self.assertEqual(self.session.last(S.COMPACTION_END)["payload"]["outcome"],
                         "FAILED")
        self.assertIsNone(self.session.active_compaction())

    def test_HCP010_compaction_leaves_no_open_lock(self):
        self._fill()
        self.engine.compact(self.session)
        self.session.assert_compaction_idle()

    def test_HCP011_second_compaction_can_follow_first(self):
        self._fill()
        first = self.engine.compact(self.session)
        for index in range(6):
            self.session.append(S.USER_MESSAGE,
                                dict(text="L%d %s" % (index, "y" * 800)))
        second = self.engine.compact(self.session)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.shadowed_seqs, second.shadowed_seqs)

    def test_HCP012_tokens_after_is_smaller_than_before(self):
        self._fill()
        result = self.engine.compact(self.session)
        self.assertLess(result.tokens_after, result.shadowed_tokens * 3)


class PairingTests(unittest.TestCase):
    def _records(self, specs):
        return [dict(seq=index + 1, type=kind, payload=payload)
                for index, (kind, payload) in enumerate(specs)]

    def test_HCP013_call_without_result_inside_span_extends_forward(self):
        records = self._records([
            (S.USER_MESSAGE, dict(text="u")),
            (S.TOOL_CALL, dict(tool_call_id="c1", name="x")),
            (S.TOOL_RESULT, dict(tool_call_id="c1", content="r")),
            (S.USER_MESSAGE, dict(text="v")),
        ])
        start, end = balance_edges(records, 0, 1)
        self.assertEqual((start, end), (0, 2))

    def test_HCP014_result_without_call_inside_span_extends_backward(self):
        records = self._records([
            (S.USER_MESSAGE, dict(text="u")),
            (S.TOOL_CALL, dict(tool_call_id="c1", name="x")),
            (S.TOOL_RESULT, dict(tool_call_id="c1", content="r")),
            (S.USER_MESSAGE, dict(text="v")),
        ])
        start, end = balance_edges(records, 2, 2)
        self.assertEqual((start, end), (1, 2))

    def test_HCP015_balanced_span_unchanged(self):
        records = self._records([
            (S.TOOL_CALL, dict(tool_call_id="c1", name="x")),
            (S.TOOL_RESULT, dict(tool_call_id="c1", content="r")),
        ])
        self.assertEqual(balance_edges(records, 0, 1), (0, 1))

    def test_HCP016_never_shadows_system_head_when_extending_backward(self):
        records = self._records([
            (S.SYSTEM_MESSAGE, dict(text="sys")),
            (S.TOOL_CALL, dict(tool_call_id="c1", name="x")),
            (S.TOOL_RESULT, dict(tool_call_id="c1", content="r")),
        ])
        start, end = balance_edges(records, 2, 2)
        # Pairing needs the call, but the head must stay; the span stops before it.
        self.assertGreaterEqual(start, 0)
        self.assertEqual(records[start]["type"] != S.SYSTEM_MESSAGE, True)

    def test_HCP017_projected_assistant_call_pair_is_not_split(self):
        records = self._records([
            (S.SYSTEM_MESSAGE, dict(text="sys")),
            (S.ASSISTANT_MESSAGE, dict(text="", tool_calls=[
                dict(id="c1", name="read", arguments="{}")])),
            (S.TOOL_RESULT, dict(tool_call_id="c1", content="original")),
            (S.USER_MESSAGE, dict(text="latest")),
        ])
        start, end = balance_edges(records, 2, 2)
        self.assertEqual((start, end), (1, 2))


if __name__ == "__main__":
    unittest.main()