"""Counterexamples for located discovery candidates and later mask projection.

All text is synthetic. No provider, clinical reference, or blinding protocol is
involved. The tests deliberately distinguish a verified span from an adequate
final masking policy.
"""
import hashlib
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.tools import ToolError
from scripts.cohort_construction.p2_checkpoint_harness.tools.masking import (
    mask_candidates,
    mask_source,
)
from scripts.cohort_construction.p2_checkpoint_harness.tools.proposals import (
    ProposalStore,
)
from scripts.cohort_construction.p2_checkpoint_harness.tools.sources import SourceIndex

TEXT = ("先减黄，复查CT后评估根治性切除。同时讨论手术时机。"
        "现决定先行胰十二指肠切除术，术后转ICU。今已行手术。")
REPEATED = "拟复查CT。其他记录。拟复查CT。"


class DiscoverySpanTests(unittest.TestCase):
    def setUp(self):
        self.source = dict(source_id="S", text=TEXT,
                           source_sha256=hashlib.sha256(TEXT.encode("utf-8")).hexdigest(),
                           clock=dict(value="2026-03-01", precision="date"),
                           clock_basis="body_record_time")
        self.store = ProposalStore(source_index=SourceIndex([self.source]))

    def candidate(self, local_id, quote, **kwargs):
        return self.store.tool_propose_candidate().run(
            local_id=local_id, scope="yes", record_function="new_decision",
            action_id="DIAG_PANCREATIC_CONTRAST_CT",
            source_id="S", quote=quote, **kwargs)

    def test_HPL027_multiple_coexisting_candidates_keep_distinct_verified_spans(self):
        quotes = ["先减黄，复查CT后评估根治性切除。", "现决定先行胰十二指肠切除术，"]
        first = self.candidate("drainage", quotes[0])
        second = self.candidate("surgery", quotes[1])
        self.assertTrue(first["accepted"] and second["accepted"])
        spans = [entry["record"]["span"] for entry in self.store.events()]
        self.assertEqual([TEXT[span["start"]:span["end"]] for span in spans], quotes)
        self.assertLess(spans[0]["end"], spans[1]["start"])
        masked = mask_candidates(self.source, [
            dict(event_id="drainage", status="ACCEPTED", scope="yes",
                 span=spans[0]),
            dict(event_id="surgery", status="ACCEPTED", scope="yes",
                 span=spans[1]),
        ], selected_event_ids={"drainage"})
        self.assertNotIn(quotes[0], masked["text"])
        self.assertIn(quotes[1], masked["text"])
        self.assertIn("术后转ICU", masked["text"])
        self.assertEqual(self.source["text"], TEXT)

    def test_HPL028_coexisting_decisions_can_be_masked_independently(self):
        spans = []
        for local_id, quote in enumerate(("先减黄，", "现决定先行胰十二指肠切除术，")):
            result = self.candidate(str(local_id), quote)
            spans.append(dict(event_id=str(local_id), status="ACCEPTED",
                              scope="yes", span=result["span_verdict"] |
                              dict(quote=quote)))
        only_surgery = mask_candidates(
            self.source, spans, selected_event_ids={"1"})
        self.assertIn("先减黄，", only_surgery["text"])
        self.assertNotIn("胰十二指肠切除术", only_surgery["text"])
        both = mask_candidates(self.source, spans, selected_event_ids={"0", "1"})
        self.assertNotIn("先减黄", both["text"])
        self.assertNotIn("胰十二指肠切除术", both["text"])

    def test_HPL029_measure_name_mask_does_not_claim_to_remove_result_leakage(self):
        quote = "现决定先行胰十二指肠切除术，"
        result = self.candidate("surgery", quote)
        projected = mask_source(self.source, [dict(
            source_id="S", quote=quote,
            start=result["span_verdict"]["start"],
            end=result["span_verdict"]["end"])])
        self.assertIn("今已行手术", projected["text"])
        self.assertEqual(projected["protocol_status"],
                         "program_projection_not_frozen_blinding_protocol")
        self.assertIn("是否泄漏", projected["note"])

    def test_HPL030_repeated_quote_requires_an_explicit_occurrence(self):
        store = ProposalStore(source_index=SourceIndex([
            dict(self.source, text=REPEATED)]))
        tool = store.tool_propose_candidate()
        result = tool.run(local_id="repeat", scope="yes", record_function="repeat",
                          source_id="S", quote="拟复查CT。")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["span_verdict"]["reason"],
                         "SPAN_START_REQUIRED_FOR_REPEATED_QUOTE")
        start = REPEATED.rindex("拟复查CT。")
        result = tool.run(local_id="repeat", scope="yes", record_function="repeat",
                          source_id="S", quote="拟复查CT。", start=start,
                          end=start + len("拟复查CT。"))
        self.assertTrue(result["accepted"])
        self.assertEqual(result["span_verdict"]["start"], start)

    def test_HPL031_bad_offsets_empty_quotes_and_unknown_sources_are_not_located(self):
        quote = "先减黄，"
        start = TEXT.index(quote)
        cases = [
            dict(source_id="S", quote=quote, start=start + 1),
            dict(source_id="S", quote=quote, start=start, end=start + 1),
            dict(source_id="S", quote=""),
            dict(source_id="unknown", quote=quote),
        ]
        reasons = []
        for index, arguments in enumerate(cases):
            result = self.store.tool_propose_candidate().run(
                local_id=str(index), scope="uncertain", record_function="uncertain",
                uncertainty="test", **arguments)
            self.assertFalse(result["accepted"])
            reasons.append(result["span_verdict"]["reason"])
        self.assertEqual(reasons, [
            "SPAN_OFFSET_MISMATCH", "SPAN_END_MISMATCH", "QUOTE_REQUIRED",
            "UNKNOWN_SOURCE_ID: 'unknown' is not in this case. Use list_sources or search_sources to find the right id."])

    def test_HPL032_cross_source_and_noncontiguous_quotes_are_rejected(self):
        for quote in ("先减黄，现决定", "先减黄，术后转ICU。"):
            result = self.candidate("bad-" + str(len(quote)), quote)
            self.assertFalse(result["accepted"])
            self.assertEqual(result["span_verdict"]["reason"], "QUOTE_NOT_IN_SOURCE")

    def test_HPL033_overlap_requires_review_not_silent_mutation(self):
        first = TEXT.index("先减黄，")
        with self.assertRaisesRegex(ToolError, "MASK_SPAN_OVERLAP_REVIEW_REQUIRED"):
            mask_source(self.source, [
                dict(source_id="S", quote="先减黄，复查CT", start=first,
                     end=first + len("先减黄，复查CT")),
                dict(source_id="S", quote="复查CT后评估", start=TEXT.index("复查CT后评估"),
                     end=TEXT.index("复查CT后评估") + len("复查CT后评估"))])

    def test_HPL035_mask_rechecks_content_hash_offsets_and_selected_ids(self):
        import hashlib
        quote = "现决定先行胰十二指肠切除术，"
        start = TEXT.index(quote)
        span = dict(source_id="S", quote=quote, start=start, end=start + len(quote))
        source = dict(self.source, source_sha256=hashlib.sha256(
            TEXT.encode("utf-8")).hexdigest())
        masked = mask_source(source, [span])
        self.assertNotIn(quote, masked["text"])
        empty = mask_source(source, [])
        self.assertEqual(empty["text"], TEXT)
        self.assertEqual(empty["masked_spans"], [])
        with self.assertRaisesRegex(ToolError, "MASK_SOURCE_CONTENT_HASH_MISMATCH"):
            mask_source(dict(source, text=TEXT + "今已行手术"), [span])
        with self.assertRaisesRegex(ToolError, "MASK_SPAN_NOT_VERIFIED"):
            mask_source(source, [dict(span, start=None, end=None)])
        candidate = dict(event_id="surgery", status="ACCEPTED", scope="yes", span=span)
        with self.assertRaisesRegex(ToolError, "MASK_SELECTED_CANDIDATE_IDS_INVALID"):
            mask_candidates(source, [candidate], selected_event_ids="surgery")
        with self.assertRaisesRegex(ToolError, "MASK_SELECTED_CANDIDATE_UNKNOWN"):
            mask_candidates(source, [candidate, candidate], selected_event_ids={"surgery"})

    def test_HPL034_scope_no_is_retained_but_not_active_or_deleted(self):
        from scripts.cohort_construction.p2_checkpoint_harness.harness import (
            Harness, HarnessConfig,
        )
        from scripts.cohort_construction.p2_checkpoint_harness.pipeline import CaseRunner
        from scripts.cohort_construction.p2_checkpoint_harness.tests.test_pipeline import (
            StubTransport,
            _finish,
            _sources,
            _units,
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(
                case_id="T", run_dir=Path(directory) / "run",
                cache_dir=Path(directory) / "cache",
                transport=StubTransport([
                    dict(tool_calls=[dict(id="no", name="propose_candidate", arguments=dict(
                        local_id="excluded", scope="no", record_function="generic",
                        source_id="S-A", quote="生命体征平稳，继续观察。",
                        reason="非研究范围内常规管理"))]),
                    _finish("f1"), _finish("f2"), _finish("f3"), _finish("ordering"),
                ]), config=HarnessConfig(context_window=1000000))
            self.addCleanup(harness.close)
            runner = CaseRunner(harness=harness, sources=_sources(),
                                units=_units(), case_id="T")
            runner.run_discovery()
            self.assertEqual(runner.events, [])
            self.assertEqual(len(runner.rejected), 1)
            self.assertEqual(runner._organization_objects(), [])
            self.assertEqual(runner.discovery["DU-A"]["summary"]["out_of_scope"], 1)
            self.assertEqual(runner.artifacts()["counts"]["out_of_scope"], 1)
            runner.run_ordering()
            self.assertEqual(runner.relations, [])


if __name__ == "__main__":
    unittest.main()
