"""P2 v0.6.1 automated material governance; candidate research outputs only."""

from .review import (
    AUTO_PROTOCOL_VERSION,
    MaterialReviewError,
    build_review_request,
    run_review_chain,
    validate_material_review,
)
from .chain_validation import bind_candidate_graph, validate_review_chain
from .episode import (
    build_episode_material_set, graph_revision_handoff,
    restart_material_review_after_graph_revision, validate_episode_material_set,
)

__all__ = [
    "AUTO_PROTOCOL_VERSION", "MaterialReviewError", "build_review_request",
    "run_review_chain", "validate_material_review", "build_episode_material_set",
    "graph_revision_handoff",
    "bind_candidate_graph", "validate_review_chain", "validate_episode_material_set",
    "restart_material_review_after_graph_revision",
]
