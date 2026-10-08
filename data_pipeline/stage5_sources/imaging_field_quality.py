"""Field-level syntax checks after safe routing and conservative repair."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .imaging_record_cluster import is_valid_record_head
from .imaging_overlong_repair import RepairProposal
from .imaging_safe_split import FIELD_INVALID, REPAIRED_HIGH, REPAIRED_REVIEW, UNRESOLVED, VALID_15COL


_ID18_RE = re.compile(r"^\d{17}[0-9Xx]$")


@dataclass
class QualityResult:
    proposal: RepairProposal
    status: str
    quality_errors: list[str]
    final_tokens: Optional[list[str]]

    def to_dict(self) -> dict[str, object]:
        result = self.proposal.to_dict()
        result.update(
            {
                "status": self.status,
                "quality_errors": list(self.quality_errors),
                "final_tokens": list(self.final_tokens) if self.final_tokens is not None else None,
            }
        )
        return result


def validate_15col_tokens(tokens: Optional[list[str]]) -> list[str]:
    if tokens is None:
        return ["no_candidate_tokens"]
    errors: list[str] = []
    if len(tokens) != 15:
        errors.append("parsed_column_count_not_15")
        return errors
    if not is_valid_record_head(tokens):
        errors.append("record_head_invalid")
    identity = tokens[9].strip()
    if identity and not _ID18_RE.fullmatch(identity):
        errors.append("identity_card_syntax_invalid")
    return errors


def apply_field_quality(proposal: RepairProposal) -> QualityResult:
    if proposal.status == UNRESOLVED:
        return QualityResult(proposal, UNRESOLVED, [proposal.repair_reason], None)

    errors = validate_15col_tokens(proposal.repaired_tokens)
    if errors:
        return QualityResult(proposal, FIELD_INVALID, errors, proposal.repaired_tokens)

    if proposal.status == VALID_15COL:
        return QualityResult(proposal, VALID_15COL, [], proposal.repaired_tokens)
    if proposal.status == REPAIRED_HIGH:
        return QualityResult(proposal, REPAIRED_HIGH, [], proposal.repaired_tokens)
    return QualityResult(proposal, REPAIRED_REVIEW, [], proposal.repaired_tokens)
