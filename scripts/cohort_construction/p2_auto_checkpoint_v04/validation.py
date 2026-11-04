"""Check verbatim source citations without making clinical semantic decisions.

Offsets are Python Unicode string offsets, with an exclusive end. No whitespace,
identity, Unicode or newline normalization is performed. ``allowed_spans`` is an
optional *complete* source-id allowlist: omitted source ids have no allowed span.
It is supplied by the visibility compiler, not inferred from model assertions.

Source input can be a source-id/alias mapping of raw strings or source records,
or a sequence of records. Records use ``source_id`` and a raw ``text``,
``raw_text`` or ``original_text``; explicit raw fields take precedence over text.
An optional ``masked_text`` is display-only and can never establish raw evidence.
Alias keys, ``alias`` and ``aliases`` resolve to the canonical source identity.

The function deliberately returns per-citation results. A bad secondary quote
does not erase valid primary quotes. Ambiguous occurrences are retained rather
than silently choosing the first match. Ambiguous quotes can establish source
level support only if *every* occurrence passes the visibility allowlist; they
do not establish a precise event boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
from typing import Any


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _index_sources(sources: Any) -> tuple[dict, dict]:
    if isinstance(sources, Mapping):
        items = list(sources.items())
    elif isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)):
        items = [(None, source) for source in sources]
    else:
        raise TypeError("sources must be a mapping or a sequence of source records")
    records: dict[str, dict] = {}
    aliases: dict[str, set[str]] = {}
    for key, value in items:
        if isinstance(value, str):
            if not isinstance(key, str) or not key:
                raise ValueError("raw source strings require a nonempty mapping key")
            record = {"source_id": key, "text": value}
        elif isinstance(value, Mapping):
            record = dict(value)
        else:
            raise TypeError("each source must be a raw string or a source record")
        source_id = record.get("source_id", key)
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("every source needs a nonempty source_id")
        text = next((record[field] for field in ("raw_text", "original_text", "text")
                     if isinstance(record.get(field), str)), None)
        entry = {"source_id": source_id, "text": text,
                 "source_sha256": _sha(text) if text is not None else None,
                 "declared_source_sha256": record.get("source_sha256"),
                 "masked_text": record.get("masked_text")}
        previous = records.get(source_id)
        if previous is not None and previous != entry:
            raise ValueError("conflicting raw records for source_id: " + source_id)
        records[source_id] = entry
        names = [source_id]
        if isinstance(key, str):
            names.append(key)
        if isinstance(record.get("alias"), str):
            names.append(record["alias"])
        extra = record.get("aliases", [])
        if isinstance(extra, str):
            names.append(extra)
        elif isinstance(extra, Sequence):
            names.extend(alias for alias in extra if isinstance(alias, str))
        for name in names:
            if name:
                aliases.setdefault(name, set()).add(source_id)
    return records, aliases


def _all_occurrences(text: str, quote: str) -> list[dict]:
    result = []
    cursor = 0
    while True:
        start = text.find(quote, cursor)
        if start < 0:
            return result
        result.append({"start": start, "end": start + len(quote)})
        cursor = start + 1  # Include overlapping repetitions as well.


def _ranges_for(source_id: str, text: str, allowed_spans: Any) -> list[tuple] | None:
    if allowed_spans is None:
        return None
    if not isinstance(allowed_spans, Mapping):
        raise ValueError("allowed_spans must map source ids to half-open ranges")
    spans = allowed_spans.get(source_id, [])
    if not isinstance(spans, Sequence) or isinstance(spans, (str, bytes)):
        raise ValueError("allowed source ranges must be a sequence")
    result = []
    for span in spans:
        if isinstance(span, Mapping):
            start, end = span.get("start"), span.get("end")
        elif isinstance(span, Sequence) and not isinstance(span, (str, bytes)) and len(span) == 2:
            start, end = span
        else:
            raise ValueError("each allowed range needs start and end")
        if not _integer(start) or not _integer(end) or not 0 <= start <= end <= len(text):
            raise ValueError("allowed range is not within the raw source")
        result.append((start, end))
    return result


def _allowed(start: int, end: int, spans: list[tuple] | None) -> bool:
    return spans is None or any(low <= start and end <= high for low, high in spans)


def resolve_citations(citations: Sequence, sources: Any,
                      allowed_spans: Mapping | None = None) -> dict[str, list]:
    """Resolve each model quote to raw source text, retaining independent errors.

    Returns ``valid``, ``ambiguous`` and ``invalid`` lists. Entries always retain
    ``citation_index`` and ``citation`` (the unmodified submitted citation).
    Valid entries have canonical source id/hash, quote and verified start/end.
    Ambiguous entries have all raw ``occurrences`` with ``allowed`` flags and
    ``eligible_as_source_support``; they never claim a verified start/end.

    An explicit start or end requires *both*, with an exact raw-text match.
    Failed explicit offsets never fall back to searching the text. Source/hash,
    quote and visibility checks are mechanical constraints; this function does
    not decide clinical relevance, evidence evolution or event identity.
    """
    if not isinstance(citations, Sequence) or isinstance(citations, (str, bytes)):
        raise TypeError("citations must be a sequence")
    records, aliases = _index_sources(sources)
    result: dict[str, list] = {"valid": [], "ambiguous": [], "invalid": []}
    for index, submitted in enumerate(citations):
        entry = {"citation_index": index, "citation": deepcopy(submitted)}

        def invalid(status: str, **details: Any) -> None:
            result["invalid"].append({**entry, "status": status, **details})

        if not isinstance(submitted, Mapping):
            invalid("INVALID_CITATION")
            continue
        supplied_id, supplied_alias = submitted.get("source_id"), submitted.get("alias")
        if supplied_id is None and supplied_alias is None:
            invalid("MISSING_SOURCE_REFERENCE")
            continue
        if supplied_id is not None and (not isinstance(supplied_id, str) or supplied_id not in records):
            invalid("UNKNOWN_SOURCE_ID", requested_source_id=supplied_id)
            continue
        resolved_alias = None
        if supplied_alias is not None:
            matches = aliases.get(supplied_alias, set()) if isinstance(supplied_alias, str) else set()
            if not matches:
                invalid("UNKNOWN_SOURCE_ALIAS", requested_alias=supplied_alias)
                continue
            if len(matches) != 1:
                invalid("AMBIGUOUS_SOURCE_ALIAS", source_candidates=sorted(matches))
                continue
            resolved_alias = next(iter(matches))
        if supplied_id is not None and resolved_alias is not None and supplied_id != resolved_alias:
            invalid("SOURCE_ALIAS_ID_CONFLICT")
            continue
        source = records[supplied_id or resolved_alias]
        source_id, text, source_hash = source["source_id"], source["text"], source["source_sha256"]
        entry.update(source_id=source_id, source_sha256=source_hash)
        if text is None:
            invalid("UNKNOWN_RAW_TEXT", reason="No raw source text is available; display text is not evidence.")
            continue
        declared = source["declared_source_sha256"]
        if declared is not None and declared != source_hash:
            invalid("SOURCE_HASH_MISMATCH", declared_source_sha256=declared)
            continue
        if "source_sha256" in submitted and submitted["source_sha256"] != source_hash:
            invalid("CITATION_SOURCE_HASH_MISMATCH", submitted_source_sha256=submitted["source_sha256"])
            continue
        quote = submitted.get("quote")
        if not isinstance(quote, str) or not quote:
            invalid("INVALID_QUOTE")
            continue
        entry["quote"] = quote
        try:
            spans = _ranges_for(source_id, text, allowed_spans)
        except ValueError as error:
            invalid("INVALID_ALLOWED_SPANS", reason=str(error))
            continue
        explicit = "start" in submitted or "end" in submitted
        if explicit:
            start, end = submitted.get("start"), submitted.get("end")
            if (not _integer(start) or not _integer(end)
                    or not 0 <= start < end <= len(text) or text[start:end] != quote):
                invalid("INVALID_EXPLICIT_SPAN", requested_start=start, requested_end=end)
                continue
            if not _allowed(start, end, spans):
                invalid("QUOTE_OUTSIDE_ALLOWED_SPANS", start=start, end=end,
                        raw_quote_verified=True, reason="The cited span is not allowed in this evidence context.")
                continue
            result["valid"].append({**entry, "status": "VALID", "start": start, "end": end,
                                    "raw_quote_verified": True, "span_verified": True,
                                    "eligible_as_source_support": True})
            continue
        occurrences = _all_occurrences(text, quote)
        if not occurrences:
            masked = source["masked_text"]
            if isinstance(masked, str) and quote in masked:
                invalid("UNKNOWN_RAW_QUOTE", reason="Quote matches masked display text but cannot be verified in raw text.",
                        raw_quote_verified=False)
            else:
                invalid("QUOTE_NOT_IN_SOURCE", raw_quote_verified=False)
            continue
        for occurrence in occurrences:
            occurrence["allowed"] = _allowed(occurrence["start"], occurrence["end"], spans)
        eligible = [occurrence for occurrence in occurrences if occurrence["allowed"]]
        if not eligible:
            invalid("QUOTE_OUTSIDE_ALLOWED_SPANS", occurrences=occurrences,
                    raw_quote_verified=True, reason="No matching raw occurrence is allowed in this evidence context.")
        elif len(occurrences) > 1:
            all_allowed = len(eligible) == len(occurrences)
            result["ambiguous"].append({
                **entry, "status": "AMBIGUOUS_SPAN", "occurrences": occurrences,
                "raw_quote_verified": True, "span_verified": False,
                "allowed_status": "ALL_OCCURRENCES_ALLOWED" if all_allowed else "SOME_OCCURRENCES_DISALLOWED",
                "eligible_as_source_support": all_allowed,
            })
        else:
            occurrence = occurrences[0]
            result["valid"].append({**entry, "status": "VALID",
                                    "start": occurrence["start"], "end": occurrence["end"],
                                    "raw_quote_verified": True, "span_verified": True,
                                    "eligible_as_source_support": True})
    return result
