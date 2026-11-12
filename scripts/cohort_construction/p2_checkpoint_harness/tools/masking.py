"""Reproducible span projection for later checkpoint/masking material.

This is not a frozen clinician blinding protocol. It never changes source text.
The caller explicitly selects which located candidates to hide and may add
reviewed leakage spans for explanations, results, or later text.
"""
from __future__ import annotations

from .registry import ToolError


def _overlap(left, right):
    return max(left["start"], right["start"]) < min(left["end"], right["end"])


def candidate_spans(candidates, *, source_id):
    """Return verified, non-overlapping intervals; never guess an origin."""
    spans = []
    for candidate in candidates:
        span = candidate.get("span") or {}
        if span.get("source_id") != source_id:
            continue
        if candidate.get("status") != "ACCEPTED":
            raise ToolError("MASK_CANDIDATE_SPAN_NOT_VERIFIED:" +
                            str(candidate.get("event_id")))
        if candidate.get("scope") == "no":
            raise ToolError("MASK_OUT_OF_SCOPE_CANDIDATE_REQUIRES_SEPARATE_POLICY")
        spans.append(dict(span))
    for index, left in enumerate(spans):
        for right in spans[index + 1:]:
            if _overlap(left, right):
                raise ToolError("MASK_SPAN_OVERLAP_REVIEW_REQUIRED")
    return sorted(spans, key=lambda span: (span["start"], span["end"]))


def mask_source(source, spans, *, replacement="[REDACTED]"):
    """Mask only the supplied verified intervals in one immutable source.

    A repeated quote with no explicit start is not enough to build a candidate;
    ProposalStore enforces that upstream. This function additionally checks the
    source hash and exact substring so an offset cannot silently target the
    wrong occurrence. It does not claim that the resulting text is blinded.
    Callers must supply an explicit occurrence with both offsets; ambiguous
    repeats are never resolved for them.
    """
    import hashlib

    source_id = source["source_id"]
    text = source.get("text") or ""
    expected_hash = source.get("source_sha256")
    if expected_hash is not None:
        actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if expected_hash != actual_hash:
            raise ToolError("MASK_SOURCE_CONTENT_HASH_MISMATCH")
    intervals = []
    for span in spans:
        start, end = span.get("start"), span.get("end")
        if (isinstance(start, bool) or not isinstance(start, int) or start < 0
                or isinstance(end, bool) or not isinstance(end, int)):
            raise ToolError("MASK_SPAN_NOT_VERIFIED:EXPLICIT_OFFSETS_REQUIRED")
        if end != start + len(span.get("quote") or ""):
            raise ToolError("MASK_SPAN_NOT_VERIFIED:SPAN_END_MISMATCH")
        if span.get("source_id") != source_id:
            raise ToolError("MASK_SPAN_NOT_VERIFIED:SOURCE_ID_MISMATCH")
        quote = span.get("quote")
        if text[start:end] != quote:
            raise ToolError("MASK_SPAN_NOT_VERIFIED:SPAN_OFFSET_MISMATCH")
        intervals.append(dict(source_id=source_id, quote=quote, start=start, end=end))
    for index, left in enumerate(intervals):
        for right in intervals[index + 1:]:
            if _overlap(left, right):
                raise ToolError("MASK_SPAN_OVERLAP_REVIEW_REQUIRED")
    text = source.get("text") or ""
    pieces = []
    cursor = 0
    for span in sorted(intervals, key=lambda item: item["start"]):
        pieces.extend((text[cursor:span["start"]], replacement))
        cursor = span["end"]
    pieces.append(text[cursor:])
    return dict(source_id=source_id, source_sha256=source.get("source_sha256"),
                text="".join(pieces), masked_spans=intervals,
                original_characters=len(text),
                protocol_status="program_projection_not_frozen_blinding_protocol",
                note=("只遮蔽明确给定且核验的原文区间。其他并存决策、解释、结果或后文"
                      "是否泄漏须由后续方案和材料组织另行判断。"))


def mask_candidates(source, candidates, *, selected_event_ids):
    """Select located candidates explicitly; all other text stays untouched."""
    if not isinstance(selected_event_ids, (set, frozenset, list, tuple)):
        raise ToolError("MASK_SELECTED_CANDIDATE_IDS_INVALID")
    selected_ids = set(selected_event_ids)
    selected = [candidate for candidate in candidates
                if candidate.get("event_id") in selected_ids]
    if len(selected) != len(selected_ids):
        raise ToolError("MASK_SELECTED_CANDIDATE_UNKNOWN")
    return mask_source(source, candidate_spans(
        selected, source_id=source["source_id"]))
