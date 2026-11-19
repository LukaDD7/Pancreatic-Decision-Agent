"""Synthetic-only tests for direct-identifier de-identification."""
import base64
import copy
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages.privacy import (
    deidentify_text,
    deidentify_value,
)


class PrivacyTests(unittest.TestCase):
    def test_labeled_direct_identifiers_are_replaced(self):
        text = (
            "患者姓名：张测试，性别：女，年龄：58岁，"
            "联系电话：13800138000，邮箱：synthetic@example.invalid，"
            "住址：虚构省虚构市示例路100号2栋301室。"
        )
        result = deidentify_text(text)
        self.assertEqual(
            result,
            "患者姓名：[姓名已去标识化]，性别：女，年龄：58岁，"
            "联系电话：[电话已去标识化]，邮箱：[邮箱已去标识化]，"
            "住址：[地址已去标识化]。",
        )

    def test_labeled_visit_identifiers_and_other_name_forms_are_replaced(self):
        text = ("住院号：12345678，门诊号：MZ20261119，身份证号：110101194001011234。\n"
                "姓名：张；姓名：Alice Smith")
        result = deidentify_text(text)
        for value in ("12345678", "MZ20261119", "110101194001011234", "张；", "Alice Smith"):
            self.assertNotIn(value, result)
        self.assertEqual(result.count("[就诊或证件标识已去标识化]"), 3)
        self.assertEqual(result.count("[姓名已去标识化]"), 2)

    def test_names_require_labels_and_stop_at_adjacent_fields(self):
        self.assertEqual(
            deidentify_text("姓名 张测试 性别 男 年龄 61岁"),
            "姓名 [姓名已去标识化] 性别 男 年龄 61岁",
        )
        self.assertEqual(deidentify_text("患者张测试前来复诊。"), "患者张测试前来复诊。")
        self.assertEqual(deidentify_text("胰腺头部见占位，建议进一步评估。"),
                         "胰腺头部见占位，建议进一步评估。")

    def test_phone_formats_and_numeric_boundaries(self):
        text = "手机：138-0013-8000；备用 +86 13900139000；座机 010-12345678"
        result = deidentify_text(text)
        self.assertEqual(result.count("[电话已去标识化]"), 3)
        self.assertNotIn("138-0013-8000", result)
        self.assertNotIn("13900139000", result)
        self.assertNotIn("010-12345678", result)
        self.assertEqual(deidentify_text("随访12次，指标113800138000。"),
                         "随访12次，指标113800138000。")
        self.assertEqual(deidentify_text("联系电话：12345678"),
                         "联系电话：[电话已去标识化]")
        self.assertEqual(deidentify_text("术后第12345678小时"),
                         "术后第12345678小时")

    def test_address_stops_before_next_field_or_line(self):
        text = "地址：虚构市示例区测试路8号 电话：010-87654321\n年龄：65岁"
        self.assertEqual(
            deidentify_text(text),
            "地址：[地址已去标识化] 电话：[电话已去标识化]\n年龄：65岁",
        )
        self.assertEqual(deidentify_text("虚构市示例区测试路8号可作为研究地点"),
                         "虚构市示例区测试路8号可作为研究地点")

    def test_email_does_not_consume_surrounding_punctuation(self):
        self.assertEqual(
            deidentify_text("联系（synthetic.case+tag@example.invalid）。"),
            "联系（[邮箱已去标识化]）。",
        )

    def test_dates_demographics_and_clinical_numbers_are_retained(self):
        text = "2026-11-19 08:30，男性，72岁；病灶3.5cm，CA19-9为1200 U/mL。"
        self.assertEqual(deidentify_text(text), text)

    def test_ages_over_89_are_grouped_without_changing_other_ages(self):
        self.assertEqual(deidentify_text("年龄：98岁，性别：女"), "年龄：≥90岁，性别：女")
        self.assertEqual(deidentify_text("90 岁；89岁；术后第100天"), "≥90岁；89岁；术后第100天")
        grouped = deidentify_text("年龄：98岁")
        self.assertEqual(deidentify_text(grouped), grouped)

    def test_recursive_values_ignore_dictionary_key_names(self):
        source = {
            "完全未知字段": ["姓名：张测试", "患者张测试前来复诊。"],
            "nested": {"note": "邮箱 synthetic@example.invalid", "age": 58},
        }
        result = deidentify_value(source)
        self.assertEqual(result["完全未知字段"][0], "姓名：[姓名已去标识化]")
        self.assertEqual(result["完全未知字段"][1], "患者张测试前来复诊。")
        self.assertEqual(result["nested"]["note"], "邮箱 [邮箱已去标识化]")
        self.assertEqual(result["nested"]["age"], 58)
        self.assertEqual(source["完全未知字段"][0], "姓名：张测试")

    def test_text_api_rejects_non_string_input(self):
        with self.assertRaises(TypeError):
            deidentify_text(None)

    def test_identifier_literals_do_not_replace_short_clinical_numbers_or_years(self):
        from scripts.cohort_construction.p2_checkpoint_harness.stages.privacy import (
            collect_identifier_values)
        values = collect_identifier_values([], dict(identifier=[{"value": "12"}, {"value": "2026"}]))
        text = "2026-11-19 08:00；CA19-9 result: 12 U/mL；dose 120 mg"
        self.assertEqual(deidentify_text(text, values), text)

    def test_metadata_and_english_names_propagate_across_sources(self):
        from scripts.cohort_construction.p2_checkpoint_harness.stages.privacy import (
            collect_identifier_values)
        sources = [dict(title="姓名：张测试", text="首次记录。",
                        original_fields={"患者姓名": "李示例"}),
                   dict(text="张测试、李示例与 Alice Smith 共同出现在合成文本中。")]
        values = collect_identifier_values(sources, dict(name=[{"family": "Smith", "given": ["Alice"]}]))
        result = deidentify_text(sources[1]["text"], values)
        self.assertNotIn("张测试", result)
        self.assertNotIn("李示例", result)
        self.assertNotIn("Alice Smith", result)

    def test_fhir_ingress_deidentifies_before_sources_are_returned(self):
        from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources

        first = "姓名：张测试，性别：女，年龄：98岁，电话：13800138000。拟行增强CT。"
        second = "张测试，住址：虚构省虚构市示例路8号。性别：女。继续评估。"
        original = copy.deepcopy(first)

        def document(identifier, text):
            return dict(resource=dict(
                resourceType="DocumentReference", id=identifier,
                subject={"reference": "Patient/synthetic"},
                type={"text": "姓名：张测试的病程"},
                content=[{"attachment": {"data": base64.b64encode(
                    text.encode()).decode()}}]))

        bundle = dict(entry=[
            dict(fullUrl="Patient/synthetic", resource=dict(
                resourceType="Patient", id="synthetic",
                name=[{"family": "张", "given": ["测试"]}],
                telecom=[{"system": "phone", "value": "13800138000"}],
                address=[{"text": "虚构省虚构市示例路8号"}],
                identifier=[{"value": "SYNTHETIC-ID-001"}])),
            document("D1", first), document("D2", second),
        ])
        sources, receipt = import_sources(bundle)
        serialized = str(sources)
        for identifier in ("张测试", "13800138000", "虚构省虚构市示例路8号"):
            self.assertNotIn(identifier, serialized)
        self.assertIn("性别：女", sources[0]["text"])
        self.assertIn("年龄：≥90岁", sources[0]["text"])
        self.assertNotEqual(sources[0]["text"], original)
        self.assertEqual(sources[0]["source_sha256"],
                         sources[0]["deidentified_text_sha256"])
        self.assertNotEqual(sources[0]["original_source_sha256"],
                            sources[0]["deidentified_text_sha256"])
        self.assertEqual(receipt["direct_identifier_deidentification"]["stage"],
                         "FHIR_SOURCE_IMPORT_BEFORE_EPISODE_DISCOVERY")
        self.assertFalse(receipt["direct_identifier_deidentification"][
            "hipaa_safe_harbor_claimed"])
        quote = "拟行增强CT。"
        start = sources[0]["text"].index(quote)
        self.assertEqual(sources[0]["text"][start:start + len(quote)], quote)
        unchanged = import_sources(bundle)[0]
        self.assertEqual([source["source_id"] for source in sources],
                         [source["source_id"] for source in unchanged])
        self.assertEqual([source["text"] for source in sources],
                         [source["text"] for source in unchanged])
        from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import build
        roundtripped, _ = import_sources(build(sources, "synthetic", [], []))
        self.assertEqual(roundtripped[0]["original_source_sha256"],
                         sources[0]["original_source_sha256"])
        self.assertEqual(roundtripped[0]["source_sha256"],
                         roundtripped[0]["deidentified_text_sha256"])


if __name__ == "__main__":
    unittest.main()
