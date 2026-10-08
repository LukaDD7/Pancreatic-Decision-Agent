"""Safe routing from record clusters into the repair pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

from .imaging_record_cluster import ParsedFragment, RecordCluster


VALID_15COL = "valid_15col"
OVERLONG_RECORD = "overlong_record"
FRAGMENTED_RECORD = "fragmented_record"
REPAIRED_HIGH = "repaired_high"
REPAIRED_REVIEW = "repaired_review"
UNRESOLVED = "unresolved"
FIELD_INVALID = "field_invalid"
PROCESS_STATUSES = (
    VALID_15COL,
    OVERLONG_RECORD,
    FRAGMENTED_RECORD,
    REPAIRED_HIGH,
    REPAIRED_REVIEW,
    UNRESOLVED,
    FIELD_INVALID,
)


@dataclass
class RoutedRecord:
    """A lossless route decision for one record cluster."""

    source_file: str
    record_cluster_id: int
    fragments: list[ParsedFragment]
    status: str

    @property
    def fragment_count(self) -> int:
        return len(self.fragments)

    @property
    def parsed_column_counts(self) -> list[int]:
        return [fragment.parsed_column_count for fragment in self.fragments]

    @property
    def flattened_tokens(self) -> list[str]:
        tokens: list[str] = []
        for fragment in self.fragments:
            tokens.extend(fragment.raw_token_array)
        return tokens

    def to_dict(self) -> dict[str, object]:
        return {
            "source_file": self.source_file,
            "record_cluster_id": self.record_cluster_id,
            "status": self.status,
            "fragment_count": self.fragment_count,
            "parsed_column_counts": self.parsed_column_counts,
            "fragments": [fragment.to_dict() for fragment in self.fragments],
        }


def route_cluster(cluster: RecordCluster) -> RoutedRecord:
    """Route without slicing, padding, joining, or normalizing any token."""

    if cluster.fragment_count > 1:
        status = FRAGMENTED_RECORD
    elif cluster.fragments[0].parsed_column_count == 15:
        status = VALID_15COL
    elif cluster.fragments[0].parsed_column_count > 15:
        status = OVERLONG_RECORD
    else:
        status = FRAGMENTED_RECORD
    return RoutedRecord(
        source_file=cluster.source_file,
        record_cluster_id=cluster.record_cluster_id,
        fragments=list(cluster.fragments),
        status=status,
    )


def iter_safe_routes(clusters: Iterable[RecordCluster]) -> Iterator[RoutedRecord]:
    for cluster in clusters:
        yield route_cluster(cluster)
