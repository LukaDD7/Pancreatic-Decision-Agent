"""Offline, target-specific source partition contracts; no clinical inference."""

from .compiler import (
    ContractError,
    build_source_manifest,
    canonical_sha256,
    compile_partition,
    freeze_checkpoint_packet,
    locate_exact_quote,
    make_review_submission,
    text_sha256,
    validate_contract,
    validate_review_submission,
)

__all__ = [
    "ContractError", "build_source_manifest", "canonical_sha256", "compile_partition",
    "freeze_checkpoint_packet", "locate_exact_quote", "make_review_submission",
    "text_sha256", "validate_contract", "validate_review_submission",
]
