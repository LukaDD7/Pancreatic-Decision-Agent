"""Fictional citation counterexamples; these are not clinical gold labels."""

from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import unittest

from scripts.cohort_construction.p2_auto_checkpoint_v04.validation import resolve_citations


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CitationValidationTests(unittest.TestCase):
    def test_Q001_raw_unicode_string_offsets_are_exact(self):
        text = "甲\n证据：新发现。\r\n计划：复查。"
        quote = "证据：新发现。"
        result = resolve_citations([{"source_id": "D1", "quote": quote}], {"D1": text})
        self.assertEqual(result["invalid"], [])
        valid = result["valid"][0]
        self.assertEqual((valid["start"], valid["end"]), (2, 9))
        self.assertEqual(text[valid["start"]:valid["end"]], quote)
        self.assertEqual(valid["source_sha256"], sha(text))

    def test_Q001_no_unicode_or_whitespace_normalization(self):
        result = resolve_citations([
            {"source_id": "D1", "quote": "证据：正常"},
            {"source_id": "D1", "quote": "e\u0301"},
        ], {"D1": "证据： 正常；é"})
        self.assertEqual([x["status"] for x in result["invalid"]], ["QUOTE_NOT_IN_SOURCE"] * 2)

    def test_Q002_repeated_quote_preserves_all_raw_occurrences(self):
        text = "结果稳定。其他记录。结果稳定。"
        result = resolve_citations([{"source_id": "D1", "quote": "结果稳定。"}], {"D1": text})
        self.assertEqual(result["valid"], [])
        self.assertEqual(result["invalid"], [])
        item = result["ambiguous"][0]
        self.assertEqual(item["status"], "AMBIGUOUS_SPAN")
        self.assertEqual(item["occurrences"], [
            {"start": 0, "end": 5, "allowed": True},
            {"start": 10, "end": 15, "allowed": True},
        ])
        self.assertNotIn("start", item)
        self.assertTrue(item["eligible_as_source_support"])
        self.assertFalse(item["span_verified"])

    def test_Q002_overlapping_occurrences_are_not_lost(self):
        result = resolve_citations([{"source_id": "D1", "quote": "aaa"}], {"D1": "aaaa"})
        self.assertEqual([x["start"] for x in result["ambiguous"][0]["occurrences"]], [0, 1])

    def test_Q003_explicit_span_resolves_repetition(self):
        result = resolve_citations([
            {"source_id": "D1", "quote": "稳定", "start": 3, "end": 5},
        ], {"D1": "稳定；稳定"})
        self.assertEqual(result["ambiguous"], [])
        self.assertEqual(result["valid"][0]["start"], 3)

    def test_Q003_wrong_explicit_span_never_falls_back_to_search(self):
        citations = [
            {"source_id": "D1", "quote": "稳定", "start": 1, "end": 3},
            {"source_id": "D1", "quote": "稳定", "start": 0},
            {"source_id": "D1", "quote": "稳定", "start": False, "end": 2},
            {"source_id": "D1", "quote": "稳定", "start": -1, "end": 2},
            {"source_id": "D1", "quote": "稳定", "start": 0, "end": 999},
        ]
        result = resolve_citations(citations, {"D1": "稳定；稳定"})
        self.assertEqual(result["valid"], [])
        self.assertEqual([x["status"] for x in result["invalid"]], ["INVALID_EXPLICIT_SPAN"] * len(citations))

    def test_Q004_aliases_resolve_to_canonical_raw_source(self):
        sources = {"BLOCK1": {"source_id": "D1", "text": "新证据", "aliases": ["OLD1"]}}
        result = resolve_citations([
            {"alias": "BLOCK1", "quote": "新证据"},
            {"alias": "OLD1", "source_id": "D1", "quote": "新证据"},
        ], sources)
        self.assertEqual(len(result["valid"]), 2)
        self.assertEqual({x["source_id"] for x in result["valid"]}, {"D1"})

    def test_Q004_unknown_and_conflicting_aliases_do_not_pass(self):
        sources = [
            {"source_id": "D1", "text": "稳定", "alias": "B1"},
            {"source_id": "D2", "text": "稳定", "alias": "B2"},
        ]
        result = resolve_citations([
            {"alias": "B3", "quote": "稳定"},
            {"source_id": "missing", "quote": "稳定"},
            {"alias": "B1", "source_id": "D2", "quote": "稳定"},
            {"quote": "稳定"},
        ], sources)
        self.assertEqual([x["status"] for x in result["invalid"]], [
            "UNKNOWN_SOURCE_ALIAS", "UNKNOWN_SOURCE_ID", "SOURCE_ALIAS_ID_CONFLICT", "MISSING_SOURCE_REFERENCE",
        ])

    def test_Q004_colliding_alias_does_not_choose_arbitrary_source(self):
        sources = [
            {"source_id": "D1", "text": "稳定", "alias": "B"},
            {"source_id": "D2", "text": "稳定", "alias": "B"},
        ]
        result = resolve_citations([{"alias": "B", "quote": "稳定"}], sources)
        self.assertEqual(result["invalid"][0]["status"], "AMBIGUOUS_SOURCE_ALIAS")
        self.assertEqual(result["invalid"][0]["source_candidates"], ["D1", "D2"])

    def test_Q005_source_and_citation_hashes_are_checked(self):
        good = {"source_id": "good", "text": "稳定", "source_sha256": sha("稳定")}
        bad = {"source_id": "bad", "text": "稳定", "source_sha256": "0" * 64}
        result = resolve_citations([
            {"source_id": "good", "quote": "稳定", "source_sha256": sha("稳定")},
            {"source_id": "good", "quote": "稳定", "source_sha256": "0" * 64},
            {"source_id": "bad", "quote": "稳定"},
        ], [good, bad])
        self.assertEqual(len(result["valid"]), 1)
        self.assertEqual([x["status"] for x in result["invalid"]], ["CITATION_SOURCE_HASH_MISMATCH", "SOURCE_HASH_MISMATCH"])

    def test_Q006_visibility_ranges_block_current_choice_and_future_sources(self):
        text = "探查：局部受侵。决定：改变方案。"
        choice_start = text.index("决定")
        result = resolve_citations([
            {"source_id": "operation", "quote": "局部受侵"},
            {"source_id": "operation", "quote": "改变方案"},
            {"source_id": "tomorrow", "quote": "后续结果"},
        ], {"operation": text, "tomorrow": "后续结果"},
            allowed_spans={"operation": [[0, choice_start]]})
        self.assertEqual(len(result["valid"]), 1)
        self.assertEqual([x["status"] for x in result["invalid"]], ["QUOTE_OUTSIDE_ALLOWED_SPANS"] * 2)
        self.assertTrue(all(x["raw_quote_verified"] for x in result["invalid"]))

    def test_Q006_quote_must_fit_wholly_inside_one_allowed_range(self):
        result = resolve_citations([{"source_id": "D1", "quote": "证据甲乙"}], {"D1": "证据甲乙"},
                                   allowed_spans={"D1": [[0, 2], [2, 4]]})
        self.assertEqual(result["invalid"][0]["status"], "QUOTE_OUTSIDE_ALLOWED_SPANS")

    def test_Q006_explicit_disallowed_occurrence_cannot_fall_back_to_allowed_one(self):
        result = resolve_citations([
            {"source_id": "D1", "quote": "稳定", "start": 3, "end": 5},
        ], {"D1": "稳定；稳定"}, allowed_spans={"D1": [[0, 2]]})
        self.assertEqual(result["valid"], [])
        self.assertEqual(result["invalid"][0]["status"], "QUOTE_OUTSIDE_ALLOWED_SPANS")

    def test_Q007_mixed_allowed_repetition_keeps_uncertainty(self):
        result = resolve_citations([{"source_id": "D1", "quote": "稳定"}], {"D1": "稳定；稳定"},
                                   allowed_spans={"D1": [[0, 2]]})
        item = result["ambiguous"][0]
        self.assertFalse(item["eligible_as_source_support"])
        self.assertEqual(item["allowed_status"], "SOME_OCCURRENCES_DISALLOWED")
        self.assertEqual([x["allowed"] for x in item["occurrences"]], [True, False])
        self.assertEqual(result["valid"], [])

    def test_Q007_all_unallowed_repetitions_are_invalid_prior_evidence(self):
        result = resolve_citations([{"source_id": "D1", "quote": "稳定"}], {"D1": "稳定；稳定"}, allowed_spans={})
        self.assertEqual(result["ambiguous"], [])
        self.assertEqual(result["invalid"][0]["status"], "QUOTE_OUTSIDE_ALLOWED_SPANS")
        self.assertEqual(len(result["invalid"][0]["occurrences"]), 2)

    def test_Q008_one_bad_citation_does_not_erase_valid_or_ambiguous_support(self):
        citations = [
            {"source_id": "D1", "quote": "明确决定", "role": "primary"},
            {"source_id": "D1", "quote": "不存在的引文", "role": "secondary"},
            {"source_id": "D1", "quote": "稳定", "role": "evidence"},
        ]
        result = resolve_citations(citations, {"D1": "明确决定；稳定；稳定"})
        self.assertEqual([x["citation_index"] for x in result["valid"]], [0])
        self.assertEqual([x["citation_index"] for x in result["invalid"]], [1])
        self.assertEqual([x["citation_index"] for x in result["ambiguous"]], [2])
        self.assertEqual(result["valid"][0]["citation"]["role"], "primary")

    def test_Q009_masked_display_is_not_fabricated_raw_evidence(self):
        source = {"source_id": "D1", "raw_text": "姓名张三；拟复查", "masked_text": "姓名某某；拟复查"}
        result = resolve_citations([
            {"source_id": "D1", "quote": "姓名某某"},
            {"source_id": "D1", "quote": "拟复查"},
            {"source_id": "D1", "quote": "姓名某某", "start": 0, "end": 4},
        ], [source])
        self.assertEqual([x["status"] for x in result["invalid"]], ["UNKNOWN_RAW_QUOTE", "INVALID_EXPLICIT_SPAN"])
        self.assertEqual(result["valid"][0]["quote"], "拟复查")
        self.assertEqual(result["valid"][0]["source_sha256"], sha(source["raw_text"]))

    def test_Q009_missing_raw_text_is_explicit_unknown(self):
        result = resolve_citations([{"source_id": "D1", "quote": "拟复查"}],
                                   [{"source_id": "D1", "masked_text": "拟复查"}])
        self.assertEqual(result["invalid"][0]["status"], "UNKNOWN_RAW_TEXT")

    def test_Q009_explicit_raw_field_wins_over_transformed_display(self):
        result = resolve_citations([{"source_id": "D1", "quote": "李四"}],
                                   [{"source_id": "D1", "original_text": "李四", "text": "某某"}])
        self.assertEqual(result["valid"][0]["source_sha256"], sha("李四"))

    def test_Q010_malformed_citations_remain_individual_errors(self):
        result = resolve_citations([None, {"source_id": "D1", "quote": ""},
                                    {"source_id": "D1", "quote": "有效"}], {"D1": "有效"})
        self.assertEqual([x["status"] for x in result["invalid"]], ["INVALID_CITATION", "INVALID_QUOTE"])
        self.assertEqual(len(result["valid"]), 1)

    def test_Q010_malformed_visibility_range_is_fail_closed_per_source(self):
        result = resolve_citations([
            {"source_id": "D1", "quote": "有效"}, {"source_id": "D2", "quote": "有效"},
        ], {"D1": "有效", "D2": "有效"}, allowed_spans={"D1": [[0, 3]], "D2": [[0, 2]]})
        self.assertEqual(result["invalid"][0]["status"], "INVALID_ALLOWED_SPANS")
        self.assertEqual(result["valid"][0]["source_id"], "D2")

    def test_Q011_input_is_not_mutated(self):
        citations = [{"alias": "B", "quote": "稳定", "metadata": {"x": [1]}}]
        sources = [{"source_id": "D1", "text": "稳定", "alias": "B"}]
        allowed = {"D1": [{"start": 0, "end": 2}]}
        before = deepcopy((citations, sources, allowed))
        result = resolve_citations(citations, sources, allowed)
        result["valid"][0]["citation"]["metadata"]["x"].append(2)
        self.assertEqual((citations, sources, allowed), before)

    def test_Q011_duplicate_conflicting_source_identity_is_not_silently_replaced(self):
        with self.assertRaisesRegex(ValueError, "conflicting raw records"):
            resolve_citations([], [{"source_id": "D1", "text": "甲"}, {"source_id": "D1", "text": "乙"}])

    def test_Q012_issue_map_references_existing_tests(self):
        path = Path(__file__).resolve().parents[1] / "regression_issue_map.json"
        issue_map = json.loads(path.read_text())
        self.assertEqual(issue_map["origin"], "synthetic_counterexamples")
        test_names = set(dir(type(self)))
        for issue in issue_map["issues"]:
            self.assertTrue(issue["id"].startswith(("V04-Q", "V04-NET")))
            self.assertTrue(issue["required_tests"])
            if issue["id"].startswith("V04-Q"):
                for name in issue["required_tests"]:
                    if "::" in name:
                        module_name, class_name, method_name = name.split("::")
                        module = importlib.import_module(module_name.removesuffix(".py").replace("/", "."))
                        self.assertTrue(hasattr(getattr(module, class_name), method_name), name)
                    else:
                        self.assertIn(name, test_names)


if __name__ == "__main__":
    unittest.main()
