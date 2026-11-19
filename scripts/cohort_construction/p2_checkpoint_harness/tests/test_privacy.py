"""Synthetic-only tests for direct-identifier de-identification."""
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


if __name__ == "__main__":
    unittest.main()
