"""Transparent, deliberately permissive retrieval; never a clinical classifier.

Search one sentence or two adjacent sentences in the same paragraph. Terms must
be near an explicit surgery/resection anchor. Preserve the complete source units
for review, rather than trimming qualifiers to the lexical matching window.
"""
import re


def compile_rules(config):
    return {
        'anchor': re.compile(config['surgical_anchor'], re.I),
        'radius': config['radius_characters'],
        'patterns': {name: [re.compile(s, re.I) for s in rule['groups']]
                     for name, rule in config['patterns'].items()},
    }


def source_windows(text):
    """Keep original contiguous spans: no joining facts across paragraphs."""
    for paragraph in re.finditer(r'[^\n]+', text):
        units = list(re.finditer(r'[^。；;]+[。；;]?', paragraph.group()))
        for i, unit in enumerate(units):
            start = paragraph.start() + unit.start()
            yield start, paragraph.start() + unit.end(), 1
            if i + 1 < len(units):
                yield start, paragraph.start() + units[i+1].end(), 2


def find_hits(text, compiled):
    found = []
    for start, end, sentence_count in source_windows(text):
        unit = text[start:end]
        for anchor in compiled['anchor'].finditer(unit):
            lo = max(0, anchor.start() - compiled['radius'])
            hi = min(len(unit), anchor.end() + compiled['radius'])
            for name, groups in compiled['patterns'].items():
                matches = [pattern.search(unit, lo, hi) for pattern in groups]
                if not all(matches):
                    continue
                terms = [{'text': m.group(), 'start': start+m.start(), 'end': start+m.end()}
                         for m in [anchor, *matches]]
                found.append({'pattern': name, 'start': start, 'end': end,
                              'sentence_count': sentence_count,
                              'quote': unit, 'matched_terms': terms})
    # Keep the shortest covering unit, not both a sentence and its parent pair.
    # This is lexical deduplication only; it does not merge clinical events.
    selected = []
    for hit in sorted(found, key=lambda h: (h['end']-h['start'], h['start'], h['pattern'])):
        if not any(h['pattern'] == hit['pattern'] and
                   hit['start'] <= h['start'] and h['end'] <= hit['end'] for h in selected):
            selected.append(hit)
    return sorted(selected, key=lambda h: (h['start'], h['pattern'], h['end']))


def review_flags(text):
    """Clues for sampling/review, not mutually exclusive temporal judgments."""
    tests = {
        'PAST_CUE': r'既往|当时|曾于|曾行|遂于|此前|后于|\d+月前|\d+年前',
        'FUTURE_PLAN_CUE': r'待|拟|建议|再评估|重新评估|择期|计划',
        'QUESTION_CUE': r'是否|能否|有无|可否|[？?]',
        'OTHER_OPERATION_CUE': r'ERCP|PTCD|活检|穿刺|置(?:管|入|换)|支架|神经(?:离断|毁损)',
    }
    return [name for name, pattern in tests.items() if re.search(pattern, text, re.I)]
