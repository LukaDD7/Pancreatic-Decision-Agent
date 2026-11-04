"""Read frozen development outputs; recomputed routing is never a past receipt."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

from .core import digest, read, require, sha_bytes, sha_text

CASE_FILES = ('source_fhir_r4_bundle.json', 'summary.json', 'discovery.json',
              'decision_events.json', 'episodes.json', 'relations.json')


def _inventory_signature(units):
    result = {}
    for unit in units:
        require(unit['unit_id'] not in result, 'DUPLICATE_INVENTORY_UNIT')
        require(len(set(unit['source_ids'])) == len(unit['source_ids']), 'DUPLICATE_UNIT_SOURCE')
        result[unit['unit_id']] = sorted(unit['source_ids'])
    return result


def _derive_routing(sources, units):
    from scripts.cohort_construction.p2_auto_checkpoint_v04 import context
    expected = _inventory_signature(units)
    matches = []
    for include_support in (False, True):
        audit = []
        generated = context.build_units(sources, {'include_support_documents': include_support}, routing_audit=audit)
        if _inventory_signature(generated) == expected:
            matches.append((include_support, audit))
    provenance = dict(kind='reconstructed_routing', original_run_receipt=False,
                      router_module='p2_auto_checkpoint_v04.context.build_units',
                      router_file_sha256=sha_bytes(Path(context.__file__).read_bytes()),
                      inventory_unit_membership_verified=bool(matches),
                      matching_include_support_documents=[item[0] for item in matches])
    if not matches:
        return [], {**provenance, 'gap': 'FROZEN_UNIT_MEMBERSHIP_NOT_REPRODUCED_BY_CURRENT_ROUTER'}
    # If both configurations match, no additional support unit exists. Their
    # route dispositions must still agree before using any inferred exclusion.
    if len(matches) > 1 and matches[0][1] != matches[1][1]:
        return [], {**provenance, 'gap': 'AMBIGUOUS_RECONSTRUCTED_ROUTING'}
    return matches[0][1], provenance


def load_case(directory):
    from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources
    directory = Path(directory).resolve()
    paths = {name: directory / name for name in CASE_FILES}
    require(all(path.is_file() for path in paths.values()), 'FROZEN_CASE_FILE_MISSING')
    data = {name: read(path) for name, path in paths.items()}
    summary = data['summary.json']
    require(summary['case_id'] == directory.name, 'CASE_ID_DIRECTORY_MISMATCH')
    inventory = directory / 'unit_inventory.json'
    inventory_origin = 'case_directory'
    if not inventory.is_file():
        original = summary.get('original_input_directory')
        require(isinstance(original, str) and original, 'FROZEN_UNIT_INVENTORY_UNAVAILABLE')
        inventory = Path(original) / 'unit_inventory.json'
        inventory_origin = 'summary_original_input_directory'
    require(inventory.is_file(), 'FROZEN_UNIT_INVENTORY_UNAVAILABLE')
    paths['unit_inventory.json'] = inventory.resolve()
    units = read(inventory)['units']
    membership = _inventory_signature(units)
    sources, source_receipt = import_sources(data['source_fhir_r4_bundle.json'])
    indexed = {source['source_id']: source for source in sources}
    require(len(indexed) == len(sources), 'DUPLICATE_SOURCE_ID')
    for source in sources:
        require(sha_text(source['text']) == source['source_sha256'], 'SOURCE_TEXT_HASH_MISMATCH')
    for unit in units:
        require(unit['source_ids'] and not set(unit['source_ids']) - indexed.keys(), 'INVENTORY_SOURCE_MISSING')
        original_texts = unit.get('original_texts')
        require(isinstance(original_texts, list) and
                {item['source_id'] for item in original_texts} == set(unit['source_ids']) and
                len(original_texts) == len(unit['source_ids']), 'INVENTORY_ORIGINAL_TEXT_COVERAGE_MISMATCH')
        for original in original_texts:
            source = indexed[original['source_id']]
            require(original.get('text') == source['text'] and original.get('source_sha256') == source['source_sha256'],
                    'INVENTORY_SOURCE_VERSION_MISMATCH')
    discovered = data['discovery.json']
    by_unit = {}
    for result in discovered['units']:
        uid = result['unit_id']
        require(uid in membership and uid not in by_unit, 'DISCOVERY_UNIT_INVENTORY_MISMATCH')
        by_unit[uid] = result
    flattened = [event for result in by_unit.values() for event in result['events']]
    ids = [event['event_id'] for event in flattened]
    require(len(set(ids)) == len(ids) and sorted(ids) == sorted(event['event_id'] for event in discovered['events']),
            'DISCOVERY_EVENT_INVENTORY_MISMATCH')
    require({event['event_id']: event for event in flattened} ==
            {event['event_id']: event for event in discovered['events']}, 'DISCOVERY_EVENT_CONTENT_MISMATCH')
    events = data['decision_events.json']
    require(len({event['event_id'] for event in events}) == len(events), 'DUPLICATE_FINAL_EVENT_ID')
    for event in flattened + events:
        require(event['unit_id'] in membership and sorted(event.get('source_ids', [])) == membership[event['unit_id']] and
                event.get('source_id') in membership[event['unit_id']],
                'EVENT_SOURCE_UNIT_MISMATCH')
    routing, routing_provenance = _derive_routing(sources, units)
    optional = ('failures.json', 'final_graph_audit.json', 'model_receipts.json')
    for name in optional:
        if (directory / name).is_file():
            paths[name] = directory / name
            data[name] = read(paths[name])
    return dict(case_id=summary['case_id'], directory=directory, sources=sources,
                units=units, discoveries=by_unit, events=events,
                episodes=data['episodes.json'], relations=data['relations.json'],
                final_graph_audit=data.get('final_graph_audit.json'),
                paths=paths, routing=routing, routing_provenance=routing_provenance,
                inventory_origin=inventory_origin, source_import_receipt=source_receipt,
                failures=deepcopy(discovered.get('failures', [])))


def load_release(directory):
    directory = Path(directory).resolve()
    require(directory.is_dir(), 'RELEASE_DIRECTORY_MISSING')
    case_dirs = sorted(path for path in directory.glob('SCALE-*') if path.is_dir())
    require(case_dirs, 'NO_FROZEN_CASES')
    return [load_case(path) for path in case_dirs]


def population(cases):
    """Disjoint sampled tasks, with overlapping eligibility recorded separately."""
    strata = {name: [] for name in ('discovered_units', 'zero_discovery_units',
                                   'rejected_proposal_units', 'support_sources_not_discovered')}
    case_index = {case['case_id']: case for case in cases}
    for case in cases:
        for unit in case['units']:
            result = case['discoveries'].get(unit['unit_id'])
            if result is None:
                continue  # An absent/failed request is not a zero-event result.
            item = dict(case_id=case['case_id'], unit_id=unit['unit_id'],
                        source_ids=unit['source_ids'], selection_kind='frozen_unit')
            strata['discovered_units' if result['events'] else 'zero_discovery_units'].append(item)
            if result.get('rejected_mentions'):
                strata['rejected_proposal_units'].append(item)
        for route in case['routing']:
            if route['disposition'] == 'SUPPORT_DOCUMENT_CONTEXT_ONLY' and route['source_characters'] > 0:
                strata['support_sources_not_discovered'].append(dict(case_id=case['case_id'],
                    unit_id=None, source_ids=[route['source_id']], selection_kind='reconstructed_support_source'))
    return strata, case_index


def _instant(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?', value
        ):
            return None
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        # Follow the existing site's explicit +08:00 convention only for
        # retrieval. Never add midnight to a date-only value.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone(timedelta(hours=8)))
        return value.astimezone(timezone.utc)
    except ValueError:
        return None


def scope_sources(case, item, context_days):
    require(type(context_days) is int and 0 <= context_days <= 365, 'INVALID_CONTEXT_DAYS')
    indexed = {source['source_id']: source for source in case['sources']}
    anchors = [indexed[sid] for sid in item['source_ids']]
    selected = set(item['source_ids'])
    dates = [_instant((source.get('clock') or {}).get('value')) for source in anchors]
    dates = [value for value in dates if value is not None]
    unresolved_anchor = False
    if context_days:
        if not dates:
            # Do not silently expand to all patient records on an unknown anchor.
            unresolved_anchor = True
        else:
            low, high = min(dates) - timedelta(days=context_days), max(dates) + timedelta(days=context_days)
            for source in case['sources']:
                at = _instant((source.get('clock') or {}).get('value'))
                if at is None or low <= at <= high:
                    selected.add(source['source_id'])
    documents = [dict(source_id=source['source_id'], source_sha256=source['source_sha256'],
                      kind=source.get('kind'), title=source.get('title'),
                      source_clock=deepcopy(source.get('clock')), clock_basis=source.get('clock_basis'),
                      text=source['text']) for source in case['sources'] if source['source_id'] in selected]
    scope = dict(kind='complete_source_documents', source_ids=sorted(selected),
                 context_days=context_days, proxy_clock_only=True,
                 naive_proxy_clock_timezone='+08:00 (explicit retrieval convention only)',
                 anchor_time_unresolved=unresolved_anchor,
                 description=('Current complete source unit only.' if context_days == 0 else
                     'Current complete source unit plus all delivered sources within the explicitly registered proxy-clock days, and sources with unresolved clocks; an unresolved anchor stays current-unit-only.'),
                 limitation='Retrieval scope only; not an Episode definition, clinical availability proof, or full patient record. Findings apply only to the supplied complete sources.')
    return documents, scope
