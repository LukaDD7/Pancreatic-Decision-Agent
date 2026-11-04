"""Synthetic forms exercise mechanics; none defines a clinical protocol."""

import copy
import json
from pathlib import Path
import unittest

from scripts.cohort_construction.p2_checkpoint_review import (
    ReviewValidationError, validate_questionnaire, validate_questionnaire_response,
    validate_submission,
)


def questionnaire(items=None):
    return {"resourceType": "Questionnaire", "url": "urn:synthetic:form", "version": "demo-1", "status": "active",
            "experimental": True, "title": "虚构机制测试表，不能用于临床评审",
            "item": items or [{"linkId": "modes", "type": "choice", "required": True, "repeats": True,
                "text": "虚构多选项", "answerOption": [
                    {"valueCoding": {"system": "urn:synthetic:mode", "code": "A", "display": "虚构甲"}},
                    {"valueCoding": {"system": "urn:synthetic:mode", "code": "B", "display": "虚构乙"}}]},
                {"linkId": "note", "type": "text", "maxLength": 10, "text": "虚构说明"}]}


def response(items=None):
    return {"resourceType": "QuestionnaireResponse", "questionnaire": "urn:synthetic:form|demo-1",
            "status": "completed", "item": items if items is not None else [{"linkId": "modes", "answer": [
                {"valueCoding": {"system": "urn:synthetic:mode", "code": "A"}},
                {"valueCoding": {"system": "urn:synthetic:mode", "code": "B"}}]}]}


class ReviewValidationTests(unittest.TestCase):
    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(ReviewValidationError) as raised:
            function(*args, **kwargs)
        self.assertEqual(code, raised.exception.code)
        self.assertTrue(raised.exception.path.startswith("$"))

    def test_choice_multiple_answers_one_item_and_input_immutable(self):
        q, r = questionnaire(), response()
        original = copy.deepcopy((q, r))
        self.assertEqual("PASS", validate_questionnaire(q)["status"])
        self.assertEqual(1, validate_questionnaire_response(q, r)["answered_item_count"])
        self.assertEqual(original, (q, r))

    def test_prefill_and_unknown_fields_are_rejected_not_ignored(self):
        mutations = [("initial", []), ("answerValueSet", "urn:synthetic:valueset"),
                     ("definition", "urn:synthetic:definition"), ("extension", []),
                     ("modifierExtension", []), ("futureFeature", True)]
        for key, value in mutations:
            with self.subTest(field=key):
                q = questionnaire()
                q["item"][0][key] = value
                self.assert_error("UNSUPPORTED_FIELD", validate_questionnaire, q)
        for value in (True, False):
            q = questionnaire()
            q["item"][0]["answerOption"][0]["initialSelected"] = value
            self.assert_error("UNSUPPORTED_FIELD", validate_questionnaire, q)

    def test_unknown_top_level_resource_metadata_rejected(self):
        q = questionnaire()
        q["contained"] = [{"resourceType": "Binary", "data": "hidden"}]
        self.assert_error("UNSUPPORTED_FIELD", validate_questionnaire, q)
        r = response()
        r["extension"] = []
        self.assert_error("UNSUPPORTED_FIELD", validate_questionnaire_response, questionnaire(), r)

    def test_unsupported_types_and_repetitions_rejected(self):
        for kind in ("open-choice", "attachment", "dateTime", "quantity", "url", ["choice"]):
            with self.subTest(kind=kind):
                self.assert_error("UNSUPPORTED_ITEM_TYPE", validate_questionnaire, questionnaire([{"linkId": "q", "type": kind}]))
        for kind in ("group", "string", "text", "boolean", "integer", "decimal", "date"):
            item = {"linkId": "q", "type": kind, "repeats": True}
            if kind == "group":
                item["item"] = [{"linkId": "x", "type": "string"}]
            with self.subTest(kind=kind):
                self.assert_error("ONLY_CHOICE_REPETITION_SUPPORTED", validate_questionnaire, questionnaire([item]))

    def test_exact_code_system_and_version_are_enforced(self):
        q = questionnaire()
        for key, value in (("system", "urn:wrong"), ("code", "C"), ("version", "unapproved")):
            with self.subTest(key=key):
                r = response()
                r["item"][0]["answer"][0]["valueCoding"][key] = value
                self.assert_error("ANSWER_CHOICE_NOT_PERMITTED", validate_questionnaire_response, q, r)
        q["item"][0]["answerOption"][0]["valueCoding"]["version"] = "v1"
        r = response()
        self.assert_error("ANSWER_CHOICE_NOT_PERMITTED", validate_questionnaire_response, q, r)
        r["item"][0]["answer"][0]["valueCoding"]["version"] = "v1"
        self.assertEqual("PASS", validate_questionnaire_response(q, r)["status"])

    def test_coding_display_is_not_identity_and_missing_system_fails(self):
        r = response()
        r["item"][0]["answer"][0]["valueCoding"]["display"] = "显示文字不影响代码身份"
        validate_questionnaire_response(questionnaire(), r)
        del r["item"][0]["answer"][0]["valueCoding"]["system"]
        self.assert_error("NONEMPTY_STRING_REQUIRED", validate_questionnaire_response, questionnaire(), r)

    def test_empty_and_whitespace_strings_do_not_answer_required(self):
        q = questionnaire([{"linkId": "note", "type": "text", "required": True}])
        for value in ("", " ", "\n\t"):
            with self.subTest(value=value):
                self.assert_error("NONEMPTY_STRING_REQUIRED", validate_questionnaire_response,
                                  q, response([{"linkId": "note", "answer": [{"valueString": value}]}]))
        self.assert_error("REQUIRED_ANSWER_MISSING", validate_questionnaire_response, q,
                          {"resourceType": "QuestionnaireResponse", "questionnaire": "urn:synthetic:form|demo-1", "status": "completed"})

    def test_false_boolean_is_real_answer_and_enables_exists(self):
        q = questionnaire([{"linkId": "flag", "type": "boolean", "required": True},
                           {"linkId": "note", "type": "string", "required": True,
                            "enableWhen": [{"question": "flag", "operator": "exists", "answerBoolean": True}]}])
        r = response([{"linkId": "flag", "answer": [{"valueBoolean": False}]},
                      {"linkId": "note", "answer": [{"valueString": "虚构"}]}])
        validate_questionnaire_response(q, r)
        r["item"].pop()
        self.assert_error("REQUIRED_ANSWER_MISSING", validate_questionnaire_response, q, r)

    def test_strict_primitive_types_bool_is_never_integer(self):
        for kind, key, good, bad in (("boolean", "valueBoolean", False, 0),
                                      ("integer", "valueInteger", 0, True),
                                      ("decimal", "valueDecimal", 0.5, True),
                                      ("date", "valueDate", "2026-10-28", "2026-02-31")):
            q = questionnaire([{"linkId": "x", "type": kind, "required": True}])
            validate_questionnaire_response(q, response([{"linkId": "x", "answer": [{key: good}]}]))
            with self.subTest(kind=kind):
                with self.assertRaises(ReviewValidationError):
                    validate_questionnaire_response(q, response([{"linkId": "x", "answer": [{key: bad}]}]))

    def test_number_and_date_limits_fail_closed(self):
        for kind, key, values in (("integer", "valueInteger", [2**31, -(2**31)-1, 1.0]),
                                 ("decimal", "valueDecimal", [float("inf"), float("nan"), 10**1000]),
                                 ("date", "valueDate", ["2026", "2026-10", "2026-10-28T00:00:00Z"])):
            q = questionnaire([{"linkId": "x", "type": kind}])
            for value in values:
                with self.subTest(kind=kind, value=str(value)[:30]):
                    with self.assertRaises(ReviewValidationError):
                        validate_questionnaire_response(q, response([{"linkId": "x", "answer": [{key: value}]}]))

    def test_choice_string_options(self):
        q = questionnaire([{"linkId": "x", "type": "choice", "required": True, "answerOption": [{"valueString": "虚构甲"}, {"valueString": "虚构乙"}]}])
        validate_questionnaire_response(q, response([{"linkId": "x", "answer": [{"valueString": "虚构乙"}]}]))
        self.assert_error("ANSWER_CHOICE_NOT_PERMITTED", validate_questionnaire_response, q,
                          response([{"linkId": "x", "answer": [{"valueString": "虚构丙"}]}]))

    def test_multiple_answer_limits_duplicates_and_single_value(self):
        q, r = questionnaire(), response()
        q["item"][0]["repeats"] = False
        self.assert_error("MULTIPLE_ANSWERS_NOT_ALLOWED", validate_questionnaire_response, q, r)
        q = questionnaire()
        r["item"][0]["answer"][1] = copy.deepcopy(r["item"][0]["answer"][0])
        self.assert_error("DUPLICATE_ANSWER", validate_questionnaire_response, q, r)
        for bad_answer in ({}, {"valueString": "bad"}, {"valueCoding": {"system": "urn:x", "code": "A"}, "item": []}):
            r = response()
            r["item"][0]["answer"] = [bad_answer]
            with self.assertRaises(ReviewValidationError):
                validate_questionnaire_response(q, r)

    def test_unknown_link_duplicate_item_and_wrong_tree(self):
        r = response()
        r["item"][0]["linkId"] = "unknown"
        self.assert_error("UNKNOWN_RESPONSE_LINK_ID", validate_questionnaire_response, questionnaire(), r)
        r = response()
        r["item"].append(copy.deepcopy(r["item"][0]))
        self.assert_error("DUPLICATE_RESPONSE_ITEM", validate_questionnaire_response, questionnaire(), r)
        q = questionnaire([{"linkId": "g", "type": "group", "item": [{"linkId": "x", "type": "string"}]}])
        self.assert_error("RESPONSE_HIERARCHY_MISMATCH", validate_questionnaire_response, q, response([{"linkId": "x", "answer": [{"valueString": "a"}]}]))

    def test_nested_group_and_display_cannot_receive_answers(self):
        q = questionnaire([{"linkId": "g", "type": "group", "required": True, "item": [
            {"linkId": "info", "type": "display", "text": "虚构说明"},
            {"linkId": "x", "type": "string", "required": True}]}])
        r = response([{"linkId": "g", "item": [{"linkId": "info"}, {"linkId": "x", "answer": [{"valueString": "a"}]}]}])
        validate_questionnaire_response(q, r)
        r["item"][0]["answer"] = []
        self.assert_error("NONQUESTION_CANNOT_HAVE_ANSWERS", validate_questionnaire_response, q, r)
        del r["item"][0]["answer"]
        r["item"][0]["item"][0]["answer"] = [{"valueString": "a"}]
        self.assert_error("NONQUESTION_CANNOT_HAVE_ANSWERS", validate_questionnaire_response, q, r)

    def test_group_omission_cannot_bypass_required_descendant(self):
        q = questionnaire([{"linkId": "g", "type": "group", "item": [{"linkId": "x", "type": "string", "required": True}]}])
        r = response()
        del r["item"]
        self.assert_error("REQUIRED_ANSWER_MISSING", validate_questionnaire_response, q, r)

    def test_required_group_requires_real_answer_not_display_or_empty_item(self):
        q = questionnaire([{"linkId": "g", "type": "group", "required": True, "item": [
            {"linkId": "d", "type": "display", "text": "虚构显示"}, {"linkId": "x", "type": "string"}]}])
        for children in ([{"linkId": "d"}], [{"linkId": "x"}]):
            self.assert_error("REQUIRED_GROUP_MISSING", validate_questionnaire_response, q, response([{"linkId": "g", "item": children}]))
        validate_questionnaire_response(q, response([{"linkId": "g", "item": [{"linkId": "x", "answer": [{"valueString": "真实作答"}]}]}]))
        q["item"][0]["item"].pop()
        self.assert_error("REQUIRED_GROUP_HAS_NO_ANSWERABLE_ITEM", validate_questionnaire, q)

    def test_disabled_question_and_descendants_cannot_be_present(self):
        q = questionnaire([{"linkId": "flag", "type": "boolean"},
            {"linkId": "g", "type": "group", "enableWhen": [{"question": "flag", "operator": "=", "answerBoolean": True}],
             "item": [{"linkId": "x", "type": "string", "required": True}]}])
        r = response([{"linkId": "flag", "answer": [{"valueBoolean": False}]}])
        self.assertNotIn("x", validate_questionnaire_response(q, r)["enabled_link_ids"])
        r["item"].append({"linkId": "g"})
        self.assert_error("DISABLED_ITEM_PRESENT", validate_questionnaire_response, q, r)

    def test_not_equal_multiple_choice_means_no_equal_answer(self):
        q = questionnaire()
        q["item"][1].update(required=True, enableWhen=[{"question": "modes", "operator": "!=", "answerCoding": {"system": "urn:synthetic:mode", "code": "A"}}])
        r = response()
        validate_questionnaire_response(q, r)
        r["item"].append({"linkId": "note", "answer": [{"valueString": "陈旧答案"}]})
        self.assert_error("DISABLED_ITEM_PRESENT", validate_questionnaire_response, q, r)
        r["item"][0]["answer"].pop(0)  # B only: no answer equals A.
        validate_questionnaire_response(q, r)
        r["item"].pop()
        self.assert_error("REQUIRED_ANSWER_MISSING", validate_questionnaire_response, q, r)
        q["item"][0]["required"] = False
        del r["item"]
        self.assert_error("REQUIRED_ANSWER_MISSING", validate_questionnaire_response, q, r)
        r["item"] = [{"linkId": "note", "answer": [{"valueString": "没有等于A的答案"}]}]
        validate_questionnaire_response(q, r)

    def test_order_conditions_and_all_any(self):
        for behavior in ("all", "any"):
            q = questionnaire([{"linkId": "n", "type": "integer"}, {"linkId": "d", "type": "date"},
                {"linkId": "x", "type": "string", "required": True, "enableBehavior": behavior, "enableWhen": [
                    {"question": "n", "operator": ">=", "answerInteger": 3},
                    {"question": "d", "operator": "<", "answerDate": "2026-10-28"}]}])
            r = response([{"linkId": "n", "answer": [{"valueInteger": 4}]}, {"linkId": "d", "answer": [{"valueDate": "2026-10-29"}]}])
            if behavior == "any":
                self.assert_error("REQUIRED_ANSWER_MISSING", validate_questionnaire_response, q, r)
            else:
                validate_questionnaire_response(q, r)
        for operator, answer, expected in ((">", 3, True), ("<", 5, True), (">=", 4, True), ("<=", 4, True), ("=", 4, True), ("!=", 4, False)):
            q = questionnaire([{"linkId": "n", "type": "integer"}, {"linkId": "x", "type": "string", "enableWhen": [{"question": "n", "operator": operator, "answerInteger": answer}]}])
            r = response([{"linkId": "n", "answer": [{"valueInteger": 4}]}])
            self.assertEqual(expected, "x" in validate_questionnaire_response(q, r)["enabled_link_ids"])

    def test_invalid_conditions_and_cyclic_or_forward_reference_rejected(self):
        base = [{"linkId": "flag", "type": "boolean"}, {"linkId": "x", "type": "string", "enableWhen": [{"question": "flag", "operator": "=", "answerBoolean": True}]}]
        cases = [("question", "x", "CONDITION_REQUIRES_PRECEDING_QUESTION"),
                 ("question", "absent", "CONDITION_REQUIRES_PRECEDING_QUESTION"),
                 ("operator", "bad", "UNSUPPORTED_CONDITION_OPERATOR"),
                 ("operator", ">", "ORDER_CONDITION_TYPE_UNSUPPORTED")]
        for key, value, code in cases:
            items = copy.deepcopy(base)
            items[1]["enableWhen"][0][key] = value
            self.assert_error(code, validate_questionnaire, questionnaire(items))
        items = copy.deepcopy(base)
        items[1]["enableWhen"][0] = {"question": "flag", "operator": "exists", "answerString": "true"}
        self.assert_error("EXISTS_REQUIRES_BOOLEAN", validate_questionnaire, questionnaire(items))
        items[1]["enableWhen"][0] = {"question": "flag", "operator": "=", "answerInteger": 1}
        self.assert_error("CONDITION_TYPE_MISMATCH", validate_questionnaire, questionnaire(items))
        items = copy.deepcopy(base)
        items[1]["enableWhen"] *= 2
        self.assert_error("MULTIPLE_CONDITIONS_REQUIRE_BEHAVIOR", validate_questionnaire, questionnaire(items))

    def test_binding_lifecycle_and_partial_draft(self):
        q = questionnaire()
        for canonical in ("urn:synthetic:form", "urn:synthetic:form|wrong", "urn:wrong|demo-1"):
            r = response()
            r["questionnaire"] = canonical
            self.assert_error("RESPONSE_QUESTIONNAIRE_MISMATCH", validate_questionnaire_response, q, r)
        for status in ("in-progress", "amended", "entered-in-error", "stopped", "bad"):
            r = response()
            r["status"] = status
            with self.assertRaises(ReviewValidationError):
                validate_questionnaire_response(q, r)
        r = response()
        r["status"] = "in-progress"
        del r["item"]
        validate_questionnaire_response(q, r, require_completed=False)

    def test_display_flags_child_questions_and_duplicate_options_rejected(self):
        self.assert_error("DISPLAY_CANNOT_HAVE_QUESTION_FLAGS", validate_questionnaire,
                          questionnaire([{"linkId": "d", "type": "display", "required": False}]))
        self.assert_error("ONLY_GROUP_CHILDREN_SUPPORTED", validate_questionnaire,
                          questionnaire([{"linkId": "x", "type": "string", "item": [{"linkId": "y", "type": "string"}]}]))
        q = questionnaire()
        q["item"][0]["answerOption"].append(copy.deepcopy(q["item"][0]["answerOption"][0]))
        self.assert_error("DUPLICATE_CHOICE_OPTION", validate_questionnaire, q)
        q = questionnaire()
        q["item"].append(copy.deepcopy(q["item"][0]))
        self.assert_error("DUPLICATE_QUESTIONNAIRE_LINK_ID", validate_questionnaire, q)

    def test_response_order_text_and_maxlength(self):
        r = response()
        r["item"].append({"linkId": "note", "answer": [{"valueString": "12345678901"}]})
        self.assert_error("ANSWER_EXCEEDS_MAX_LENGTH", validate_questionnaire_response, questionnaire(), r)
        r["item"][1]["answer"][0]["valueString"] = "a"
        r["item"].reverse()
        self.assert_error("RESPONSE_ORDER_MISMATCH", validate_questionnaire_response, questionnaire(), r)
        r = response()
        r["item"][0]["text"] = "篡改的问题文字"
        self.assert_error("RESPONSE_TEXT_MISMATCH", validate_questionnaire_response, questionnaire(), r)

    def test_datetime_validation(self):
        r = response()
        r["authored"] = "2026-10-28T12:34:56Z"
        validate_questionnaire_response(questionnaire(), r)
        for value in ("2026-10-28", "2026-10-28T12:34:56", "2026-02-31T12:34:56Z"):
            r["authored"] = value
            with self.assertRaises(ReviewValidationError):
                validate_questionnaire_response(questionnaire(), r)

    def test_datetime_rejects_offset_normalization_and_out_of_fhir_range(self):
        for offset in ("+00:60", "+00:99", "+14:01", "+14:59", "+15:00", "+23:59", "-00:60", "-14:01", "-23:59"):
            r = response()
            r["authored"] = "2026-10-28T12:34:56" + offset
            self.assert_error("INVALID_DATETIME", validate_questionnaire_response, questionnaire(), r)
        for offset in ("Z", "+00:00", "-00:00", "+13:59", "-13:59", "+14:00", "-14:00"):
            r = response()
            r["authored"] = "2026-10-28T12:34:56" + offset
            validate_questionnaire_response(questionnaire(), r)

    def test_shared_unicode_blank_set_and_nonempty_uri_suffix(self):
        q = questionnaire([{"linkId": "x", "type": "string"}])
        for value in ("\x85", "\x1c", "\ufeff", "\x1c\x85\ufeff", "\u3000"):
            self.assert_error("NONEMPTY_STRING_REQUIRED", validate_questionnaire_response, q,
                              response([{"linkId": "x", "answer": [{"valueString": value}]}]))
            validate_questionnaire_response(q, response([{"linkId": "x", "answer": [{"valueString": "a" + value}]}]))
        for value in ("urn:", "https:", "urn:\x85", "urn:\ufeff", "urn:x\ufeff"):
            q = questionnaire()
            q["url"] = value
            self.assert_error("ABSOLUTE_URI_REQUIRED", validate_questionnaire, q)
        q["url"] = "urn:nonempty"
        validate_questionnaire(q)

    def test_submission_verifies_packet_binding_before_answers(self):
        from scripts.cohort_construction.p2_checkpoint_packet import (
            ContractError, build_source_manifest, canonical_sha256, compile_partition,
            freeze_checkpoint_packet, make_review_submission,
        )
        target = {"checkpoint_id": "CP-fictional", "decision_task_id": "task-fictional", "visibility_policy_version": "demo-1"}
        manifest = build_source_manifest("M-fictional", [{"source_id": "S-fictional", "source_version": "1", "text": "虚构可见资料。"}])
        compiled = compile_partition(manifest, {"schema_version": "0.1.0", "proposal_id": "P-fictional", "proposal_version": "1",
            "manifest_id": manifest["manifest_id"], "target": target,
            "source_bindings": [{k: manifest["sources"][0][k] for k in ("source_id", "source_version", "sha256")}],
            "segments": [{"segment_id": "seg", "source_id": "S-fictional", "disposition": "KEEP", "quote_locator": {"exact_quote": "虚构可见资料。"}}]})
        from scripts.cohort_construction.p2_checkpoint_packet.tests.test_compiler import semantic as semantic_v02
        semantic = semantic_v02(compiled)
        semantic["target"] = target
        q = questionnaire()
        frozen = {"canonical": q["url"], "version": q["version"], "sha256": canonical_sha256(q), "frozen": True, "resource": q}
        packet = freeze_checkpoint_packet(compiled, semantic, frozen, packet_id="PK-fictional", packet_version="1")
        submission = make_review_submission(packet, response(), submission_id="SUB-fictional", reviewer_assignment_id="assignment-fictional", submitted_at="2026-10-28T00:00:00Z")
        self.assertEqual("PASS", validate_submission(submission, packet)["status"])
        submission["questionnaire_response"]["item"][0]["answer"][0]["valueCoding"]["code"] = "not-permitted"
        self.assert_error("ANSWER_CHOICE_NOT_PERMITTED", validate_submission, submission, packet)
        submission["packet_binding"]["packet_id"] = "wrong-packet"
        with self.assertRaisesRegex(ContractError, "SUBMISSION_PACKET_BINDING_MISMATCH"):
            validate_submission(submission, packet)

    def test_issue_map_resolves_named_tests(self):
        path = Path(__file__).resolve().parents[1] / "regression_issue_map.json"
        issues = json.loads(path.read_text())
        for issue in issues["issues"]:
            self.assertTrue(issue["required_tests"])
            for test_name in issue["required_tests"]:
                self.assertTrue(callable(getattr(self, test_name)))

    def test_cross_language_contract_fixtures(self):
        path = Path(__file__).resolve().parent / "fixtures" / "cross_language_cases.json"
        for case in json.loads(path.read_text())["cases"]:
            with self.subTest(case=case["name"]):
                if case["expected_valid"]:
                    validate_questionnaire_response(case["questionnaire"], case["response"])
                else:
                    self.assert_error(case["expected_error_code"], validate_questionnaire_response,
                                      case["questionnaire"], case["response"])

    def test_fixed_seed_mutation_and_raw_number_fixtures(self):
        path = Path(__file__).resolve().parent / "fixtures"
        data = json.loads((path / "differential_mutations.json").read_text())
        cases = data["cases"]
        for raw in json.loads((path / "raw_json_number_cases.json").read_text())["cases"]:
            cases.append({**raw, **json.loads(raw["raw_json"])})
        for case in cases:
            with self.subTest(case=case["name"]):
                if case["expected_valid"]:
                    validate_questionnaire_response(case["questionnaire"], case["response"])
                else:
                    self.assert_error(case["expected_error_code"], validate_questionnaire_response,
                                      case["questionnaire"], case["response"])

    def test_javascript_python_validity_agrees_on_fixed_corpus(self):
        from .differential_review import _node, run_comparison
        if not _node():
            self.skipTest("Node runtime unavailable for cross-language execution")
        receipt = run_comparison()
        self.assertGreaterEqual(receipt["case_count"], 325)
        self.assertEqual([], receipt["validity_differences"])

    def test_unpaired_surrogate_rejected_and_real_astral_character_accepted(self):
        q = questionnaire([{"linkId": "x", "type": "string"}])
        self.assert_error("INVALID_UNICODE_STRING", validate_questionnaire_response, q,
                          response([{"linkId": "x", "answer": [{"valueString": "\ud800"}]}]))
        validate_questionnaire_response(q, response([{"linkId": "x", "answer": [{"valueString": "😀"}]}]))


if __name__ == "__main__":
    unittest.main()
