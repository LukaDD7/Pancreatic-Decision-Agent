"""Local source/target counterexamples must remain visible in review."""
import unittest
from scripts.cohort_construction.p2_dynamic_pathway_probe.review_context import local_context_flags


class ContextTests(unittest.TestCase):
    def test_DYN005_consent_without_book_suffix_is_flagged(self):
        flags=local_context_flags({'document_type':'输尿管软镜知情同意'},'先抗感染，控制后再手术。')
        self.assertIn('DYN005_CONSENT_DOCUMENT',flags)
        self.assertIn('DYN005_OTHER_TARGET_REVIEW',flags)

    def test_DYN005_ESWL_not_pancreatic_resection_by_default(self):
        flags=local_context_flags({'document_type':'首次查房'},'待结果回报后择期行ESWL术。')
        self.assertIn('DYN005_OTHER_TARGET_REVIEW',flags)

    def test_DYN006_first_diagnosis_not_treatment_order(self):
        flags=local_context_flags({'document_type':'术前讨论'},'目前首先考虑：胰头恶性肿瘤新辅助治疗后，需手术治疗。')
        self.assertIn('DYN006_DIAGNOSTIC_SUMMARY_NOT_A_FUTURE_SEQUENCE',flags)

    def test_explicit_future_treatment_relation_not_diagnostic_summary(self):
        flags=local_context_flags({'document_type':'MDT记录'},'拟先行新辅助治疗后再评估是否手术。')
        self.assertNotIn('DYN006_DIAGNOSTIC_SUMMARY_NOT_A_FUTURE_SEQUENCE',flags)


if __name__=='__main__':unittest.main()
