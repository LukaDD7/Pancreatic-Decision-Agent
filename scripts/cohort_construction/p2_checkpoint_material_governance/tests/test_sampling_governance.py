"""Regression for frozen packet sampling and human-only descriptive summaries."""
from copy import deepcopy
import unittest

from scripts.cohort_construction.p2_checkpoint_material_governance.sampling import (
    freeze_sampling_plan, select_packets, summarize_human_sample,
)
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256


class HumanSamplingTests(unittest.TestCase):
    def packets(self):
        return [dict(packet_id='a', packet_sha256='1' * 64, stratum_id='operative'),
                dict(packet_id='b', packet_sha256='2' * 64, stratum_id='support')]

    def plan(self):
        return freeze_sampling_plan(dict(plan_id='fictional-plan', protocol_version='p1',
            population_version='synthetic-v1', seed=20261029, sample_size=2,
            strata=[dict(stratum_id='operative', n=1), dict(stratum_id='support', n=1)],
            critical_defect_limit=0, ordinary_defect_limit=1, confidence_level=0.95,
            unresolved_policy='not_accept', reviewer_independence_policy='actual independent human',
            expansion_policy='predefined review of all failures'), self.packets())

    def response(self, plan, assignment, **changes):
        return dict(packet_id=assignment['packet_id'], packet_sha256=assignment['packet_sha256'],
                    reviewer_type='human', reviewer_id='fictional-reviewer',
                    reviewed_at='2026-10-29T12:00:00+08:00',
                    frozen_plan_sha256=plan['frozen_plan_sha256'],
                    assignment_manifest_sha256=plan['assignment_manifest_sha256'],
                    independence_attested=True, status='HUMAN_SAMPLE_ACCEPT',
                    critical_defects=0, ordinary_defects=0, **changes)

    def test_frozen_stratified_selection_and_actual_denominator(self):
        plan = self.plan()
        assignments = select_packets(plan, self.packets())
        self.assertEqual(len(assignments), 2)
        empty = summarize_human_sample(plan, assignments, [])
        self.assertEqual(empty['received'], 0)
        self.assertIsNone(empty['accept_rate_wilson'])
        self.assertFalse(empty['meets_frozen_count_thresholds'])
        response = self.response(plan, assignments[0])
        partial = summarize_human_sample(plan, assignments, [response])
        self.assertEqual(partial['not_received'], 1)
        self.assertFalse(partial['statistical_validation_complete'])
        self.assertFalse(partial['descriptive_sample_summary_complete'])
        second = self.response(plan, assignments[1])
        complete = summarize_human_sample(plan, assignments, [response, second])
        self.assertTrue(complete['meets_frozen_count_thresholds'])
        self.assertTrue(complete['descriptive_sample_summary_complete'])
        self.assertFalse(complete['statistical_validation_complete'])
        self.assertFalse(complete['clinical_release_permitted'])

    def test_cannot_change_plan_or_substitute_auto_reviewer(self):
        plan = self.plan()
        plan['critical_defect_limit'] = 100
        with self.assertRaisesRegex(Exception, 'NOT_FROZEN_OR_CHANGED'):
            select_packets(plan, [])
        plan = self.plan()
        assignments = select_packets(plan, self.packets())
        response = self.response(plan, assignments[0])
        response['reviewer_type'] = 'llm_material_review'
        with self.assertRaisesRegex(Exception, 'ACTUAL_INDEPENDENT'):
            summarize_human_sample(plan, assignments, [response])

    def test_duplicate_strata_or_assignment_cannot_reduce_denominator(self):
        plan = self.plan()
        plan['strata'].append(dict(stratum_id='operative', n=0))
        plan['frozen_plan_sha256'] = canonical_sha256(
            {k: v for k, v in plan.items() if k != 'frozen_plan_sha256'})
        with self.assertRaisesRegex(Exception, 'DISJOINT'):
            select_packets(plan, self.packets())
        plan = self.plan()
        assignment = deepcopy(plan['assignment_manifest'][0])
        with self.assertRaisesRegex(Exception, 'BINDING'):
            summarize_human_sample(plan, [assignment, assignment], [])
