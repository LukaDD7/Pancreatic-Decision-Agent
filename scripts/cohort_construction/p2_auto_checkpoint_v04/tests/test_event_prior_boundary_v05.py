"""V05-PRIOR001: an offered ledger is not an event-specific prior allowlist."""
import unittest

from scripts.cohort_construction.p2_auto_checkpoint_v04 import engine
from scripts.cohort_construction.p2_auto_checkpoint_v04.context import build_units
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_engine import (
    empty_quotes, event, review, select, source,
)
from scripts.cohort_construction.p2_pathway_replay.core import clock


def synthetic_events():
    current = source('CUR', '记录时间：2022-01-03 09:00。于2022-01-01 10:00决定补充检查。', '2022-01-03 09:00')
    before = source('BEFORE', '决定继续观察。', '2022-01-01 09:00')
    after = source('AFTER', '决定调整方案。', '2022-01-02 09:00')
    day = source('DAY', '同日选择，具体时间不明。', '2022-01-01')
    unknown = source('UNKNOWN', '选择时间未记录。', None)
    current_event = event('CURRENT', current, action='决定补充检查。', document_profile='clinical_other',
                          phase='术前', choice_time='2022-01-01 10:00', time_basis='explicit_documented_choice')
    predecessors = [
        event('EARLY', before, action='决定继续观察。', document_profile='clinical_other', phase='术前',
              choice_time='2022-01-01 09:00', time_basis='explicit_documented_choice'),
        event('LATE', after, action='决定调整方案。', document_profile='clinical_other', phase='术前',
              choice_time='2022-01-02 09:00', time_basis='explicit_documented_choice'),
        event('SAME_DAY', day, action='同日选择，具体时间不明。', document_profile='clinical_other', phase='术前',
              time_basis='record_time_proxy'),
        event('UNKNOWN_TIME', unknown, action='选择时间未记录。', document_profile='clinical_other', phase='术前',
              time_basis='unknown'),
    ]
    return [current, before, after, day, unknown], current_event, predecessors


def ledger_for_all(predecessors):
    # Deliberately retain the unit-level superset as reconstruction context.
    return [dict(event_id=e['event_id'], source_id=e['source_id'],
                 source_document_clock=e['source_document_clock'], choice_time=e['choice_time'],
                 time_basis=e['time_basis']) for e in predecessors]


class EventPriorBoundaryTests(unittest.TestCase):
    def test_PRIOR001_actual_choice_boundary_overrides_later_record_clock(self):
        sources, current, predecessors = synthetic_events()
        unit = next(u for u in build_units(sources) if u['source_ids'] == ['CUR'])
        offered = engine.earlier_ledger(unit, predecessors, {s['source_id']: s for s in sources})
        # The old unit-wide ledger offers Jan02 because the note was signed Jan03.
        self.assertIn('LATE', {x['event_id'] for x in offered})
        context = dict(cutoff=clock('2022-01-01 10:00'), cutoff_basis='explicit_documented_choice')
        eligible = engine.eligible_prior_ids(current, offered, [current, *predecessors], sources, context)
        self.assertEqual(set(eligible), {'EARLY'})

    def test_PRIOR002_same_day_or_unknown_order_is_not_forced(self):
        sources, current, predecessors = synthetic_events()
        eligible = engine.eligible_prior_ids(current, ledger_for_all(predecessors), [current, *predecessors],
                                             sources, dict(cutoff=clock('2022-01-01 10:00')))
        self.assertNotIn('SAME_DAY', eligible)
        self.assertNotIn('UNKNOWN_TIME', eligible)

    def test_PRIOR003_unknown_current_boundary_certifies_no_prior(self):
        sources, current, predecessors = synthetic_events()
        current['choice_time'] = None
        current['time_basis'] = 'historical_unknown'
        eligible = engine.eligible_prior_ids(current, ledger_for_all(predecessors), [current, *predecessors],
                                             sources, dict(cutoff=clock(None)))
        self.assertEqual(set(eligible), set())

    def test_PRIOR004_unoffered_event_not_added_by_chronology_alone(self):
        sources, current, predecessors = synthetic_events()
        eligible = engine.eligible_prior_ids(current, [], [current, *predecessors], sources,
                                             dict(cutoff=clock('2022-01-01 10:00')))
        self.assertEqual(set(eligible), set())

    def test_PRIOR005_proposed_later_parent_downgraded_by_event_allowlist(self):
        sources, current, predecessors = synthetic_events()
        eligible = engine.eligible_prior_ids(current, ledger_for_all(predecessors), [current, *predecessors],
                                             sources, dict(cutoff=clock('2022-01-01 10:00')))
        status, reasons = select(e=current, r=review(previous_event_id='LATE', same_process_previous_id='LATE'),
                                 earlier_ids=set(eligible))
        self.assertEqual(status, 'UNCERTAIN_CONTRACT_OR_SOURCE')
        self.assertIn('UNPROVIDED_OR_NONPRIOR_PREVIOUS_EVENT_ID', reasons)

    def test_PRIOR006_operative_external_start_boundary_excludes_during_operation(self):
        sources, current, predecessors = synthetic_events()
        current.update(choice_time=None, time_basis='operative_phase', phase='探查后')
        # The first candidate is before the operation start; second is later.
        context = dict(cutoff=clock('2022-01-01 09:30'),
                       cutoff_basis='operation_start_conservative_external_source_boundary_not_choice_time')
        eligible = engine.eligible_prior_ids(current, ledger_for_all(predecessors), [current, *predecessors], sources, context)
        self.assertEqual(set(eligible), {'EARLY'})

    def test_PRIOR007_same_minute_unknown_seconds_not_certified(self):
        sources, current, predecessors = synthetic_events()
        predecessors[0]['choice_time'] = '2022-01-01 10:00'
        eligible = engine.eligible_prior_ids(current, ledger_for_all(predecessors), [current, *predecessors], sources,
                                             dict(cutoff=clock('2022-01-01 10:00:30')))
        self.assertNotIn('EARLY', eligible)

    def test_PRIOR008_same_unit_explicit_earlier_choice_allowed_without_array_order(self):
        note = source('SAME_NOTE', '先前选择。此后另一选择。', '2022-01-03 09:00')
        current = event('CURRENT', note, action='此后另一选择。', phase='术前', document_profile='clinical_other',
                        choice_time='2022-01-02 10:00', time_basis='explicit_documented_choice')
        previous = event('PREVIOUS', note, action='先前选择。', phase='术前', document_profile='clinical_other',
                         choice_time='2022-01-01 09:00', time_basis='explicit_documented_choice')
        unit = build_units([note])[0]
        # Deliberately provide current first: output position is not evidence.
        events = [current, previous]
        ledger = engine.earlier_ledger(unit, events, {'SAME_NOTE': note})
        eligible = engine.eligible_prior_ids(current, ledger, events, [note],
                                             dict(cutoff=clock('2022-01-02 10:00')))
        self.assertEqual(set(eligible), {'PREVIOUS'})

    def test_PRIOR009_same_unit_unordered_choice_not_promoted_by_shared_document(self):
        note = source('SAME_NOTE', '先前选择。此后另一选择。', '2022-01-03 09:00')
        current = event('CURRENT', note, action='此后另一选择。', phase='术前', document_profile='clinical_other',
                        choice_time='2022-01-02 10:00', time_basis='explicit_documented_choice')
        previous = event('PREVIOUS', note, action='先前选择。', phase='术前', document_profile='clinical_other',
                         choice_time='2022-01-02', time_basis='explicit_documented_choice')
        unit = build_units([note])[0]
        events = [previous, current]
        ledger = engine.earlier_ledger(unit, events, {'SAME_NOTE': note})
        eligible = engine.eligible_prior_ids(current, ledger, events, [note],
                                             dict(cutoff=clock('2022-01-02 10:00')))
        self.assertEqual(set(eligible), set())
        # A paragraph that appears first but has only a record proxy also
        # cannot become an independently ordered same-document predecessor.
        previous.update(choice_time=None, time_basis='record_time_proxy')
        ledger = engine.earlier_ledger(unit, events, {'SAME_NOTE': note})
        self.assertNotIn('PREVIOUS', {x['event_id'] for x in ledger})
        self.assertEqual(set(engine.eligible_prior_ids(current, ledger, events, [note],
                                                       dict(cutoff=clock('2022-01-02 10:00')))), set())


if __name__ == '__main__':
    unittest.main()
