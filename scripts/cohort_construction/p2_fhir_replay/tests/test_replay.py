"""Stable failure cases for replay boundaries; all inputs are synthetic."""
import base64
import copy
import unittest

from scripts.cohort_construction.p2_fhir_replay.core import instant, pack_event, replay, source_bundle


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.prior = pack_event('demo', 'document', 'past', '病程', '昨日计划再次引流。',
                               '2024-01-01 10:00', '2024-01-04 10:00')
        self.anchor = pack_event('demo', 'document', 'anchor', '查房', '指标仍高。今日决定引流。',
                                '2024-01-02 10:00', '2024-01-04 10:01')
        self.events = [self.prior, self.anchor]

    def run_replay(self, events=None, mode='reconstruction', **kw):
        return replay(events or self.events, 'demo', '2024-01-02 10:00', 'anchor',
                      kw.get('marker', '今日决定'), mode)

    def test_FR001_current_choice_removed_and_past_plan_retained(self):
        state, audit = self.run_replay()
        texts = [b['text'] for b in state['source_blocks']]
        self.assertIn('昨日计划再次引流。', texts)
        self.assertIn('指标仍高。', texts)
        self.assertNotIn('今日决定', str(texts))
        self.assertIn('今日决定', audit['held_current_action'])

    def test_FR002_same_time_and_future_labs_not_backfilled(self):
        labs = [pack_event('demo', 'lab', x, '检验', x, release_time=t, result=x) for x, t in
                [('prior', '2024-01-02 09:59'), ('equal', '2024-01-02 10:00'),
                 ('future', '2024-01-03 10:00'), ('unknown', None)]]
        state, _ = self.run_replay(self.events + labs)
        self.assertEqual([b['text'] for b in state['source_blocks'] if b['kind'] == 'lab'], ['prior'])

    def test_FR003_dates_never_padded_to_midnight(self):
        self.assertIsNone(instant('2024-01-02'))
        self.assertIsNone(instant('2024-02-30 12:00'))
        self.assertIsNotNone(instant('2024-01-02 12:00'))

    def test_FR004_late_creation_retained_as_uncertainty(self):
        _, audit = self.run_replay()
        self.assertTrue(all(a['late_creation'] for a in audit['source_selection']))
        state, _ = self.run_replay(mode='certified')
        self.assertEqual(state['source_blocks'], [])
        self.assertFalse(state['formal_blind_evaluation_ready'])

    def test_FR005_replay_does_not_mutate_raw_sources(self):
        original = copy.deepcopy(self.events)
        self.run_replay()
        self.assertEqual(self.events, original)
        attachment = self.anchor['resource']['content'][0]['attachment']['data']
        self.assertEqual(base64.b64decode(attachment).decode(), self.anchor['source_text'])

    def test_FR006_nonunique_or_missing_cut_fails(self):
        for marker in ['', '不存在']:
            with self.assertRaises(ValueError): self.run_replay(marker=marker)
        self.anchor['source_text'] += '今日决定'
        with self.assertRaises(ValueError): self.run_replay()

    def test_FR007_patient_boundary_and_no_summary_payload(self):
        other = pack_event('other', 'document', 'anchor', '查房', 'other secret', '2024-01-01 10:00')
        self.prior['future_pathology'] = 'outcome secret'
        self.prior['reference_answer'] = 'reference secret'
        state, _ = self.run_replay(self.events + [other])
        self.assertNotIn('secret', str(state))
        self.assertNotIn('resource', state['source_blocks'][0])

    def test_FR008_issued_not_effective_or_last_updated(self):
        e = pack_event('demo', 'lab', 'x', '总胆红素', '原始记录', release_time='2024-01-01 12:00',
                       result='<1.00 已复', unit='μmol/L')
        self.assertEqual(e['resource']['valueString'], '<1.00 已复')
        self.assertIn('issued', e['resource'])
        self.assertNotIn('effectiveDateTime', e['resource'])
        self.assertNotIn('meta', e['resource'])
        self.assertEqual(e['resource']['status'], 'unknown')

    def test_FR009_clinical_negation_is_preserved_not_reinterpreted(self):
        text = '未见肠系膜上动脉受侵；与门静脉关系无法判断。'
        e = pack_event('demo', 'imaging', 'im', 'CT', text, '2024-01-01 12:00')
        raw = base64.b64decode(e['resource']['presentedForm'][0]['data']).decode()
        self.assertEqual(raw, text)
        self.assertNotIn('issued', e['resource'])

    def test_FR010_future_revision_cannot_pass_certified_gate(self):
        self.prior.update(version_availability_verified=True, available_time='2024-01-03T10:00:00+08:00')
        state, _ = self.run_replay(mode='certified')
        self.assertEqual(state['source_blocks'], [])
        self.prior['available_time'] = '2024-01-01T11:00:00+08:00'
        state, _ = self.run_replay(mode='certified')
        self.assertEqual(len(state['source_blocks']), 1)

    def test_FR011_conversion_clock_is_not_hospital_clock(self):
        b = source_bundle(self.events, '2026-10-22T12:00:00+08:00')
        provenance = [e['resource'] for e in b['entry'] if e['resource']['resourceType'] == 'Provenance']
        self.assertTrue(all(p['recorded'].startswith('2026-10-22') for p in provenance))
        self.assertEqual(self.prior['event_time'], '2024-01-01T10:00:00+08:00')

    def test_FR012_source_duplicates_and_wrong_anchor_time_fail(self):
        with self.assertRaises(ValueError):
            source_bundle(self.events + [self.prior], '2026-10-22T12:00:00+08:00')
        with self.assertRaises(ValueError):
            replay(self.events, 'demo', '2024-01-03 10:00', 'anchor', '今日决定')

    def test_FR013_all_dates_are_timezone_aware(self):
        self.assertEqual(instant('2024-01-01 12:00'), instant('2024-01-01T04:00:00Z'))

    def test_FR014_unknown_mode_rejected(self):
        with self.assertRaises(ValueError): self.run_replay(mode='latest')


if __name__ == '__main__':
    unittest.main()
