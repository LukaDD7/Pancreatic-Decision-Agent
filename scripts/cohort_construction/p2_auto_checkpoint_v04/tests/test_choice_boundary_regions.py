"""V04-MASK001: node proof and semantic choice clauses have different roles."""
from copy import deepcopy
import hashlib
import unittest

from scripts.cohort_construction.p2_auto_checkpoint_v04.context import (
    build_evidence_context, choice_boundary_for_source,
)
from scripts.cohort_construction.p2_pathway_replay.core import clock


def fixture(text, proof, clauses=(), facts=(), post=()):
    source = dict(source_id='D', kind='document', text=text, title='术前讨论',
                  source_sha256=hashlib.sha256(text.encode()).hexdigest(),
                  clock=clock('2022-09-03 09:20'), clocks={}, clock_basis='body_record_time')

    def quote(value):
        start = text.index(value)
        return dict(source_id='D', source_sha256=source['source_sha256'],
                    quote=value, start=start, end=start + len(value))

    event = dict(source_id='D', source_ids=['D'], time_basis='record_time_proxy',
                 action_citations={'valid': [quote(proof)]},
                 choice_boundary_citations={'valid': [quote(q) for q in clauses], 'ambiguous': [], 'invalid': []},
                 prechoice_citations={'valid': [quote(q) for q in facts]},
                 postchoice_citations={'valid': [quote(q) for q in post]})
    return source, event, quote


class ChoiceClauseTests(unittest.TestCase):
    def evidence(self, source, event):
        return build_evidence_context(event, [source], preserve_current_context=True)

    def test_MASK001_bloodtype_between_two_choice_clauses_survives_without_fact_highlight(self):
        first = '限期行胰体癌根治术。'
        second = '需血库充分备血后行手术治疗。'
        text = f'一般情况可，{first}但患者血型极为特殊，{second}术后风险已经回报。'
        source, event, _ = fixture(text, text, [first, second])
        result = self.evidence(source, event)
        texts = [b['text'] for b in result['blocks']]
        self.assertTrue(any('患者血型极为特殊' in item for item in texts))
        self.assertTrue(any('一般情况可' in item for item in texts))
        self.assertFalse(any(first in item or second in item or '术后风险' in item for item in texts))
        self.assertEqual(len(result['current_choice_boundaries'][0]['exclusions']), 2)
        self.assertFalse(result['blind_replay_ready'])

    def test_MASK002_inflammation_before_mixed_choice_preserved_without_fact_highlight(self):
        state = '患者目前胰腺炎指标较高，症状明显'
        choice = '给予抑酶对症治疗,待炎症控制后限期行胰十二指肠切除术。'
        source, event, _ = fixture(state + ',' + choice, state + ',' + choice, [choice])
        result = self.evidence(source, event)
        self.assertEqual([b['text'] for b in result['blocks']], [state + ','])
        self.assertEqual(result['uncertainty']['current_choice_boundary_missing_or_ambiguous_count'], 0)

    def test_MASK003_missing_boundary_does_not_expand_broad_support(self):
        text = '患者血型特殊，需充分备血后手术。'
        source, event, _ = fixture(text, text)
        result = self.evidence(source, event)
        self.assertEqual(result['blocks'], [])
        self.assertEqual(result['current_choice_boundaries'][0]['reason'], 'MISSING_SEMANTIC_CHOICE_BOUNDARY')
        self.assertEqual(result['uncertainty']['current_choice_boundary_missing_or_ambiguous_count'], 1)

    def test_MASK004_ambiguous_boundary_falls_back_without_selecting_an_occurrence(self):
        text = '状态甲。决定手术。状态乙。决定手术。'
        source, event, _ = fixture(text, text)
        event['choice_boundary_citations']['ambiguous'] = [dict(source_id='D', quote='决定手术。')]
        result = self.evidence(source, event)
        self.assertEqual(result['blocks'], [])
        self.assertEqual(result['current_choice_boundaries'][0]['reason'], 'AMBIGUOUS_SEMANTIC_CHOICE_BOUNDARY')

    def test_MASK005_overlapping_choice_clauses_are_not_merged_into_a_new_boundary(self):
        text = '症状明显，给予抑酶治疗,待炎症控制后手术。'
        source, event, _ = fixture(text, text, ['给予抑酶治疗,待炎症控制后手术。', '待炎症控制后手术。'])
        boundary = choice_boundary_for_source(event, source)
        self.assertFalse(boundary['certified'])
        self.assertEqual(boundary['start'], 0)
        self.assertEqual(boundary['reason'], 'OVERLAPPING_SEMANTIC_CHOICE_BOUNDARIES')

    def test_MASK006_node_proof_required_for_choice_clause(self):
        text = '原先拟检查。症状明显，决定手术。'
        source, event, _ = fixture(text, '原先拟检查。', ['决定手术。'])
        result = self.evidence(source, event)
        self.assertEqual(result['blocks'], [])
        self.assertEqual(result['current_choice_boundaries'][0]['reason'],
                         'CHOICE_BOUNDARY_NOT_CONTAINED_IN_VALID_NODE_PROOF')

    def test_MASK007_postchoice_facts_before_last_choice_still_removed(self):
        first = '决定进一步检查。'
        second = '决定手术。'
        text = f'原有腹痛。{first}术后肝功能结果已回报。患者血型特殊。{second}'
        source, event, _ = fixture(text, text, [first, second], post=['术后肝功能结果已回报。'])
        texts = [b['text'] for b in self.evidence(source, event)['blocks']]
        self.assertTrue(any('患者血型特殊' in item for item in texts))
        self.assertFalse(any('术后肝功能' in item or first in item or second in item for item in texts))

    def test_MASK008_choice_clause_cannot_be_relabelled_as_a_prechoice_fact(self):
        text = '症状明显，给予抑酶治疗。'
        source, event, _ = fixture(text, text, ['给予抑酶治疗。'], facts=['给予抑酶治疗。'])
        result = self.evidence(source, event)
        self.assertEqual([b['text'] for b in result['blocks']], ['症状明显，'])
        self.assertTrue(any(q.get('reason') == 'EXPLICIT_CHOICE_CLAUSE' for q in result['ledger']))

    def test_MASK009_duplicate_identical_clause_is_deduplicated_not_uncertain(self):
        text = '症状明显，给予治疗。'
        source, event, quote = fixture(text, text, ['给予治疗。'])
        event['choice_boundary_citations']['valid'].append(quote('给予治疗。'))
        boundary = choice_boundary_for_source(event, source)
        self.assertTrue(boundary['certified'])
        self.assertEqual(len(boundary['exclusions']), 1)

    def test_MASK010_wrong_hash_or_offset_does_not_create_new_boundary(self):
        text = '症状明显，给予治疗。'
        source, event, _ = fixture(text, text, ['给予治疗。'])
        for field, value in [('source_sha256', 'bad'), ('start', 1)]:
            damaged = deepcopy(event)
            damaged['choice_boundary_citations']['valid'][0][field] = value
            result = self.evidence(source, damaged)
            self.assertEqual(result['blocks'], [])
            self.assertFalse(result['current_choice_boundaries'][0]['certified'])

    def test_MASK011_explicit_boundary_does_not_change_earlier_source_availability(self):
        text = '症状明显，给予治疗。'
        source, event, _ = fixture(text, text, ['给予治疗。'])
        same_day = dict(source, source_id='U', text='同日检查结果', clock=clock('2022-09-03'))
        same_day['source_sha256'] = hashlib.sha256(same_day['text'].encode()).hexdigest()
        future = dict(same_day, source_id='F', clock=clock('2022-09-04 09:00'))
        result = build_evidence_context(event, [source, same_day, future], preserve_current_context=True)
        self.assertEqual({b['source_id'] for b in result['blocks']}, {'D'})
        self.assertTrue(any(q['source_id'] == 'U' and q['disposition'] == 'REVIEW' for q in result['ledger']))
        self.assertTrue(any(q['source_id'] == 'F' and q['disposition'] == 'EXCLUDE_FUTURE' for q in result['ledger']))
        self.assertTrue(all(not b['availability_certified'] for b in result['blocks']))

    def test_MASK012_invalid_secondary_choice_clause_prevents_partial_expansion(self):
        text = '原状态。决定检查。新状态。决定手术。'
        source, event, _ = fixture(text, text, ['决定手术。'])
        event['choice_boundary_citations']['invalid'] = [dict(citation={'source_id': 'D'}, status='UNKNOWN_QUOTE')]
        result = self.evidence(source, event)
        self.assertEqual(result['blocks'], [])
        self.assertEqual(result['current_choice_boundaries'][0]['reason'], 'INVALID_SEMANTIC_CHOICE_BOUNDARY')

    def test_MASK013_additional_later_choice_outside_node_proof_does_not_erase_valid_boundary(self):
        state = '患者血型极为特殊，'
        choice = '需充分备血后手术。'
        extra = '术后完善病理检查。'
        text = state + choice + '后来出现新的结果。' + extra
        source, event, _ = fixture(text, state + choice, [choice, extra])
        result = self.evidence(source, event)
        self.assertEqual([b['text'] for b in result['blocks']], [state])
        boundary = result['current_choice_boundaries'][0]
        self.assertTrue(boundary['certified'])
        self.assertEqual(boundary['start'], text.index(choice))
        self.assertEqual(len(boundary['exclusions']), 2)
        self.assertEqual(len(boundary['warnings']), 1)
        self.assertFalse(boundary['warnings'][0]['can_expand_context'])

    def test_MASK014_additional_earlier_choice_outside_node_proof_is_subtracted(self):
        extra = '原先决定检查。'
        state = '患者血型极为特殊，'
        choice = '需充分备血后手术。'
        text = extra + state + choice
        source, event, _ = fixture(text, state + choice, [extra, choice])
        result = self.evidence(source, event)
        self.assertEqual([b['text'] for b in result['blocks']], [state])
        self.assertTrue(any(q['disposition'] == 'SEMANTIC_CHOICE_CLAUSE_EXCLUDED_WITHOUT_CONTEXT_EXPANSION'
                            for q in result['ledger']))

    def test_MASK015_no_supported_boundary_uses_fallback_but_excludes_exact_extra_choice(self):
        extra = '原先决定检查。'
        state = '患者血型特殊。'
        action = '需充分备血后手术。'
        source, event, _ = fixture(extra + state + action, action, [extra])
        result = self.evidence(source, event)
        self.assertEqual([b['text'] for b in result['blocks']], [state])
        boundary = result['current_choice_boundaries'][0]
        self.assertFalse(boundary['certified'])
        self.assertEqual(boundary['reason'], 'CHOICE_BOUNDARY_NOT_CONTAINED_IN_VALID_NODE_PROOF')


if __name__ == '__main__':
    unittest.main()
