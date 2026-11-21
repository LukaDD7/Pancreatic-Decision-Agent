"""Synthetic contract tests for the draft action dictionary and its stdlib API."""
from copy import deepcopy
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages.actions import (
    PHASES, action_ids, action_ids_for_phase, canonical_action, dictionary_hash,
    dictionary_version, get_action, load_dictionary,
)


class ActionDictionaryTests(unittest.TestCase):
    def test_v02_is_a_nonclinical_draft_with_specific_minimum_coverage(self):
        dictionary = load_dictionary()
        self.assertEqual(dictionary["status"], "DRAFT_NOT_CLINICAL_REFERENCE")
        self.assertEqual(dictionary["schema_version"], "action-dictionary-0.2.0-draft")
        self.assertFalse(dictionary["clinical_reference"])
        self.assertFalse(dictionary["required_patient_bundle"])
        self.assertFalse(dictionary["prefill_blind_review_answers"])
        expected = {
            "DIAG_PANCREATIC_CONTRAST_CT", "DIAG_ABDOMINAL_NONCONTRAST_CT",
            "DIAG_CHEST_CT", "DIAG_CHEST_XRAY", "DIAG_PANCREATIC_MR", "DIAG_MRCP",
            "DIAG_EUS_NO_SAMPLING", "DIAG_EUS_FNA_PANCREATIC_LESION",
            "DIAG_IMAGE_GUIDED_PANCREATIC_BIOPSY", "DIAG_IMAGE_GUIDED_LIVER_BIOPSY",
            "DIAG_STAGING_LAPAROSCOPY", "LAB_CA19_9",
            "SOURCE_EXTERNAL_IMAGING_RETRIEVAL", "REVIEW_MDT",
            "SURG_PANCREATICODUODENECTOMY", "SURG_DISTAL_PANCREATECTOMY",
            "SURG_TOTAL_PANCREATECTOMY", "NAT_AG", "NAT_FOLFIRINOX",
            "DRAIN_PTCD", "DRAIN_ERCP", "POSTOP_PATHOLOGY_REVIEW",
            "POSTOP_FOLLOW_UP_CT", "POSTOP_ADJUVANT_MDT",
            "SUPPORT_SYMPTOM_AND_FOLLOW_UP", "OTHER_SPECIFIED",
        }
        self.assertEqual(set(action_ids()), expected)
        self.assertEqual(len(action_ids()), len(expected))
        for action in dictionary["actions"]:
            with self.subTest(action_id=action["action_id"]):
                self.assertTrue(action["action_type"])
                self.assertTrue(action["label"])
                self.assertTrue(action["target"])
                self.assertTrue(action["allowed_phases"])
                self.assertFalse(action["required_for_every_patient"])
                self.assertEqual(
                    {action[key] for key in
                     ("historical_usage", "current_usage", "center_availability")},
                    {"PENDING_FREEZE"})
        eus = get_action("DIAG_EUS_NO_SAMPLING")
        self.assertIn("不穿刺、不取材", eus["target"])
        self.assertNotEqual(eus["action_type"], "OBTAIN_TISSUE_DIAGNOSIS")
        self.assertIn("肝脏可疑转移灶", get_action(
            "DIAG_IMAGE_GUIDED_LIVER_BIOPSY")["target"])

    def test_phase_listing_uses_existing_phases_and_separates_postoperative_actions(self):
        from scripts.cohort_construction.p2_checkpoint_harness.stages.management_trajectory import (
            PHASES as trajectory_phases,
        )
        self.assertEqual(set(PHASES), trajectory_phases)
        postoperative = action_ids_for_phase("POSTOPERATIVE_TRAJECTORY")
        for action_id in ("POSTOP_PATHOLOGY_REVIEW", "POSTOP_FOLLOW_UP_CT",
                          "POSTOP_ADJUVANT_MDT"):
            self.assertIn(action_id, postoperative)
            for phase in set(PHASES) - {"POSTOPERATIVE_TRAJECTORY"}:
                self.assertNotIn(action_id, action_ids_for_phase(phase))
            with self.assertRaisesRegex(ValueError, "ACTION_PHASE_NOT_ALLOWED"):
                canonical_action(action_id, phase="INITIAL_RESECTABILITY_ASSESSMENT")
        with self.assertRaisesRegex(ValueError, "ACTION_PHASE_UNKNOWN"):
            action_ids_for_phase("PREOPERATIVE")

    def test_identity_lookup_and_canonical_expansion_are_stable_and_copied(self):
        dictionary = load_dictionary()
        self.assertEqual(dictionary_version(dictionary=dictionary),
                         "action-dictionary-0.2.0-draft")
        self.assertEqual(len(dictionary_hash(dictionary=dictionary)), 64)
        self.assertEqual(dictionary_hash(dictionary=dictionary),
                         dictionary_hash(dictionary=deepcopy(dictionary)))
        changed = deepcopy(dictionary)
        changed["actions"][0]["label"] += "变化"
        self.assertNotEqual(dictionary_hash(dictionary=dictionary),
                            dictionary_hash(dictionary=changed))
        action = canonical_action("DIAG_PANCREATIC_CONTRAST_CT")
        self.assertEqual(action["action_type"], "ORDER_DIAGNOSTIC_TEST")
        self.assertEqual(action["target"], "胰腺增强CT扫描")
        action["target"] = "调用方修改"
        self.assertEqual(get_action("DIAG_PANCREATIC_CONTRAST_CT")["target"],
                         "胰腺增强CT扫描")
        with self.assertRaisesRegex(ValueError, "ACTION_ID_UNKNOWN"):
            get_action("imaging")

    def test_other_specified_requires_a_nonabstract_target(self):
        action = canonical_action("OTHER_SPECIFIED", target="腹腔镜下腹膜结节活检")
        self.assertEqual(action["target"], "腹腔镜下腹膜结节活检")
        self.assertEqual(action["action_type"], "OTHER_SPECIFIED")
        for target in (None, "", "  ", "完善检查", " 完善 检查 ", "综合治疗", "手术评估",
                       "可切除性再评估", "surgery", "imaging", "手术。"):
            with self.subTest(target=target):
                code = ("ACTION_TARGET_REQUIRED_FOR_OTHER_SPECIFIED"
                        if target is None or not target.strip()
                        else "ACTION_TARGET_ABSTRACT_FOR_OTHER_SPECIFIED")
                with self.assertRaisesRegex(ValueError, code):
                    canonical_action("OTHER_SPECIFIED", target=target)


if __name__ == "__main__":
    unittest.main()
