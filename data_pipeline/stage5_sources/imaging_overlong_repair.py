"""Conservative repair proposals for overlong and fragmented records.

Only deterministic anchor layouts are promoted to ``repaired_high``.  The
original fragments and tokens remain attached to every result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .imaging_record_cluster import is_valid_record_head
from .imaging_safe_split import (
    FRAGMENTED_RECORD,
    OVERLONG_RECORD,
    REPAIRED_HIGH,
    REPAIRED_REVIEW,
    RoutedRecord,
    UNRESOLVED,
    VALID_15COL,
)


_ID18_RE = re.compile(r"^\d{17}[0-9Xx]$")
_NUMERIC_RE = re.compile(r"^\d+$")


@dataclass
class RepairProposal:
    source_file: str
    record_cluster_id: int
    input_status: str
    status: str
    original_tokens: list[str]
    repaired_tokens: Optional[list[str]]
    repair_confidence: str
    repair_reason: str
    identity_present: bool
    conclusion_split: bool
    performance_split: bool

    @property
    def changed(self) -> bool:
        return self.repaired_tokens is not None and self.repaired_tokens != self.original_tokens

    def to_dict(self) -> dict[str, object]:
        return {
            "source_file": self.source_file,
            "record_cluster_id": self.record_cluster_id,
            "input_status": self.input_status,
            "status": self.status,
            "original_tokens": list(self.original_tokens),
            "repaired_tokens": list(self.repaired_tokens) if self.repaired_tokens is not None else None,
            "repair_confidence": self.repair_confidence,
            "repair_reason": self.repair_reason,
            "identity_present": self.identity_present,
            "conclusion_split": self.conclusion_split,
            "performance_split": self.performance_split,
        }


def _device_positions(tokens: list[str], start: int) -> list[int]:
    positions = []
    for index in range(start, len(tokens)):
        value = tokens[index].strip()
        upper = value.upper()
        if "机" in value or any(marker in upper for marker in ("CT", "MR", "DR", "DSA")):
            positions.append(index)
    return positions


def _exam_positions(tokens: list[str], start: int) -> list[int]:
    return [
        index
        for index in range(start, len(tokens) - 1)
        if _NUMERIC_RE.fullmatch(tokens[index].strip())
    ]


def _proposal_tokens(tokens: list[str]) -> tuple[Optional[list[str]], str, bool, bool, bool]:
    """Return candidate tokens, reason, identity presence, and split flags."""

    if len(tokens) < 15:
        return None, "flattened token count is below 15", False, False, False
    if not is_valid_record_head(tokens):
        return None, "first eight fields do not form a valid record head", False, False, False

    identity_positions = [
        index for index in range(8, len(tokens)) if _ID18_RE.fullmatch(tokens[index].strip())
    ]
    identity_present = bool(identity_positions)
    if len(identity_positions) > 1:
        return None, "multiple identity-card anchors", True, False, False

    identity_index = identity_positions[0] if identity_positions else None
    device_start = identity_index + 1 if identity_index is not None else 8
    device_positions = _device_positions(tokens, device_start)
    if len(device_positions) != 1:
        return None, "device anchor is missing or ambiguous", identity_present, False, False
    device_index = device_positions[0]

    exam_positions = _exam_positions(tokens, device_index + 1)
    if len(exam_positions) != 1:
        return None, "exam-number anchor is missing or ambiguous", identity_present, False, False
    exam_index = exam_positions[0]

    if identity_index is None:
        if device_index < 9:
            return None, "identity-card field is missing and hospital boundary is unavailable", False, False, False
        hospital_index = device_index - 1
        conclusion_split = hospital_index - 8 > 1
        performance_split = len(tokens) - exam_index - 2 > 1
        candidate = (
            tokens[:8]
            + [
                ",".join(tokens[8:hospital_index]),
                "",
                tokens[hospital_index],
                tokens[device_index],
                tokens[exam_index],
            ]
            + [",".join(tokens[exam_index + 1 : -1]), tokens[-1]]
        )
        return candidate, "identity-card field absent; anchor repair requires review", False, conclusion_split, performance_split

    if device_index - identity_index not in (1, 2):
        return None, "hospital boundary is ambiguous", True, identity_index - 8 > 1, False
    hospital = "" if device_index == identity_index + 1 else tokens[identity_index + 1]
    conclusion_split = identity_index - 8 > 1
    performance_split = len(tokens) - exam_index - 2 > 1
    candidate = (
        tokens[:8]
        + [",".join(tokens[8:identity_index]), tokens[identity_index], hospital, tokens[device_index], tokens[exam_index]]
        + [",".join(tokens[exam_index + 1 : -1]), tokens[-1]]
    )
    return candidate, "unique identity, device, and exam anchors", True, conclusion_split, performance_split


def repair_route(route: RoutedRecord) -> RepairProposal:
    original_tokens = route.flattened_tokens
    if route.status == VALID_15COL:
        return RepairProposal(
            route.source_file,
            route.record_cluster_id,
            route.status,
            VALID_15COL,
            original_tokens,
            list(original_tokens),
            "none",
            "legal 15-column record; no repair applied",
            bool(len(original_tokens) > 9 and _ID18_RE.fullmatch(original_tokens[9].strip())),
            False,
            False,
        )

    candidate, reason, identity_present, conclusion_split, performance_split = _proposal_tokens(original_tokens)
    if candidate is None:
        return RepairProposal(
            route.source_file,
            route.record_cluster_id,
            route.status,
            UNRESOLVED,
            original_tokens,
            None,
            "none",
            reason,
            identity_present,
            conclusion_split,
            performance_split,
        )

    high = identity_present and reason == "unique identity, device, and exam anchors"
    return RepairProposal(
        route.source_file,
        route.record_cluster_id,
        route.status,
        REPAIRED_HIGH if high else REPAIRED_REVIEW,
        original_tokens,
        candidate,
        "high" if high else "review",
        reason,
        identity_present,
        conclusion_split,
        performance_split,
    )
