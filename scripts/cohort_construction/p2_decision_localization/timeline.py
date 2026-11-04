"""Connect source witnesses into provisional chains, without equating visits
or documents with episodes. State-changing statements are never merged merely
because they occur within a fixed number of hours.
"""
from collections import Counter, defaultdict
import hashlib
import re
import statistics
from .documents import observed_time, visit_key, parse_time


def distribution(values):
    values = sorted(values)
    if not values:
        return {'n': 0, 'median': None, 'q1': None, 'q3': None}
    q = statistics.quantiles(values, n=4, method='inclusive') if len(values)>1 else [values[0]]*3
    return {'n': len(values), 'median': statistics.median(values), 'q1': q[0], 'q3': q[2], 'min': values[0], 'max': values[-1]}


def witness_state(hits):
    """A retrieval grouping label, never an Agent/Reference Standard mode."""
    labels = {h['label'] for h in hits if h['candidate_eligible']}
    values = []
    if labels & {'PLAN_RESECTION', 'INDICATION_YES'}: values.append('SURGERY_PATH')
    if 'INDICATION_NO' in labels: values.append('NO_CURRENT_SURGERY')
    if 'ASSESSMENT_PENDING' in labels: values.append('ASSESSMENT_PENDING')
    if 'NAT_PLAN' in labels: values.append('NAT_PLAN')
    return '+'.join(values)


def build_chains(notes, hits, selected, config, clock, hours):
    """Merge nearby same-state witnesses within an encounter; retain changes.

    A chain is a convenient review packet. Same wording across visits is flagged
    rather than silently certified as either an old copy or a new assessment.
    Same-state genuine reassessment inside a window can still be undercounted.
    """
    hi = defaultdict(list)
    for h in hits:
        if h['candidate_eligible']: hi[h['record_key']].append(h)
    start, end = parse_time(config['study_start']), parse_time(config['study_end'])
    groups = defaultdict(list)
    for n in notes:
        time = observed_time(n, clock)
        if n['patient_uid'] in selected and n['record_key'] in hi and time and start <= time <= end:
            groups[(n['patient_uid'], visit_key(n))].append(n)
    chains = []
    for (uid, visit), rows in sorted(groups.items()):
        current = None
        for note in sorted(rows, key=lambda n: (observed_time(n,clock), n['record_key'])):
            state = witness_state(hi[note['record_key']]); time = observed_time(note,clock)
            # Limit total chain width from FIRST witness, not a sliding window
            # that could absorb months of unchanged copied notes.
            if current and current['state'] == state and (time-parse_time(current['first_time'])).total_seconds()/3600 <= hours:
                current['records'].append(note['record_key']); current['last_time'] = str(time)
            else:
                flags=[]
                if current and (('SURGERY_PATH' in current['state'] and 'NO_CURRENT_SURGERY' in state) or ('NO_CURRENT_SURGERY' in current['state'] and 'SURGERY_PATH' in state)):
                    flags.append('OPPOSING_OPINIONS_OR_UPDATE_REVIEW')
                current = {'patient_uid': uid, 'visit_key': visit, 'state': state,
                           'first_time': str(time), 'last_time': str(time), 'records': [note['record_key']],
                           'clock': clock, 'window_hours': hours, 'review_flags':flags, 'status': 'UNADJUDICATED_SOURCE_CHAIN'}
                chains.append(current)
    counts = Counter(c['patient_uid'] for c in chains)
    return chains, {'chains': len(chains), 'per_patient': distribution([counts[u] for u in selected]),
                    'patients_zero': sum(counts[u]==0 for u in selected), 'patients_ge2': sum(counts[u]>=2 for u in selected),
                    'candidate_records': sum(len(c['records']) for c in chains),
                    'opposing_opinions_or_update_review_chains':sum(bool(c['review_flags']) for c in chains)}


def cross_visit_repeats(notes, hits):
    """Exact normalized statement recurrence; does not prove clinical copying."""
    ni = {n['record_key']:n for n in notes}; groups = defaultdict(list)
    for h in hits:
        if h['label'] not in {'PLAN_RESECTION','INDICATION_YES','INDICATION_NO','ASSESSMENT_PENDING','NAT_PLAN'}:
            continue
        key = (h['patient_uid'], h['label'], re.sub(r'\s+', '', h['sentence']))
        groups[key].append(h)
    repeated = []
    for (uid,label,sentence), hs in groups.items():
        if len({visit_key(ni[h['record_key']]) for h in hs}) > 1:
            for h in hs:
                repeated.append({'record_key':h['record_key'], 'patient_uid':uid, 'label':label,
                                 'role':h['role'], 'candidate_eligible':h['candidate_eligible'],
                                 'statement_hash':hashlib.sha256(sentence.encode()).hexdigest()})
    return repeated
