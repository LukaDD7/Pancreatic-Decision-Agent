"""Stage prompts: the bound on inlined text is declared, never silent."""
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages import discovery as D
from scripts.cohort_construction.p2_checkpoint_harness.stages.ordering import (
    compare, EventIndex,
)
from scripts.cohort_construction.p2_checkpoint_harness.tokens import estimate_text


def _unit(text, unit_id="DU-1"):
    return dict(unit_id=unit_id, kind="document", document_profile="progress_note",
                clock=dict(value="2026-03-01"), clock_basis="body_record_time",
                source_ids=["S-1"],
                original_texts=[dict(source_id="S-1", text=text)])


class DiscoveryPromptTests(unittest.TestCase):
    def test_HDP001_short_document_is_pasted_whole_and_said_to_be_complete(self):
        body = "短文书，含一次决定。"
        prompt = D.build_prompt(_unit(body))
        self.assertIn(body, prompt)
        self.assertIn("完整原文", prompt)

    def test_HDP002_long_document_declares_the_unseen_range(self):
        body = "慢性病程记录。" * 4000
        prompt = D.build_prompt(_unit(body), inline_token_budget=100)
        self.assertNotIn(body, prompt)
        self.assertIn("其余未随本请求发送", prompt)
        self.assertIn("read_source", prompt)
        # The declared totals are the real ones, not the shown ones.
        self.assertIn("共 %d 字符" % len(body), prompt)

    def test_HDP003_shown_prefix_respects_the_budget(self):
        body = "记录" * 10_000
        prompt = D.build_prompt(_unit(body), inline_token_budget=500)
        shown = D._inline_span(body, 500)[0]
        self.assertLessEqual(estimate_text(shown), 500)
        self.assertTrue(body.startswith(shown))
        self.assertIn("第 0–%d 字符" % len(shown), prompt)

    def test_HDP004_zero_budget_still_inlines_nothing_silently(self):
        body = "内容" * 100
        shown, complete = D._inline_span(body, 0)
        self.assertFalse(complete)
        self.assertEqual(shown, "")


class ClockCompareTests(unittest.TestCase):
    def _event(self, value, precision="date"):
        return dict(event_id="E" + (value or "x"), clock=dict(value=value,
                                                              precision=precision))

    def test_HCO001_unknown_clock_is_not_ordered(self):
        result = compare(self._event(None), self._event("2026-03-01"))
        self.assertEqual(result["relation"], "unknown")
        self.assertIn("不等于", result["note"])

    def test_HCO002_same_day_is_unresolved_not_before(self):
        result = compare(self._event("2026-03-01"), self._event("2026-03-01"))
        self.assertEqual(result["relation"], "same_time_unresolved")

    def test_HCO003_exact_seconds_order(self):
        left = self._event("2026-03-01T10:00:00", precision="second")
        right = self._event("2026-03-01T11:00:00", precision="second")
        result = compare(left, right)
        self.assertEqual(result["relation"], "before")

    def test_HCO004_index_requires_known_event(self):
        index = EventIndex([], source_index=None)
        from scripts.cohort_construction.p2_checkpoint_harness.tools import ToolError
        with self.assertRaises(ToolError):
            index.require("EV-missing")


if __name__ == "__main__":
    unittest.main()