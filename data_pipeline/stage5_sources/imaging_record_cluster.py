"""Parse imaging CSV rows into lossless record fragments and clusters.

This module deliberately stops at clustering.  It does not merge tokens,
repair malformed rows, or produce the 15-column imaging schema.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Sequence, TextIO


IMAGING_COLUMNS = (
    "姓名",
    "年龄",
    "性别",
    "患者编号",
    "影像号",
    "检查日期",
    "检查方法",
    "检查时间",
    "报告结论",
    "身份证号",
    "住院号",
    "检查设备",
    "检查号",
    "报告表现",
    "诊断结果",
)
RECORD_HEAD_SIZE = 8
_EMPTY = {"", "null", "none", "nan", "na", "n/a", "未填写", "无"}
_AGE_RE = re.compile(r"^\d{1,3}(?:\.\d+)?\s*(?:岁|周岁|月|天)?$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
_SEX_VALUES = {"男", "女", "未知", "不详", "未说明", "其他", "M", "F", "MALE", "FEMALE"}


@dataclass
class ParsedFragment:
    """One logical result emitted by ``csv.reader``.

    ``raw_token_array`` is the token array returned by the standard CSV
    parser.  It is retained as parsed and is never padded, truncated, joined,
    or otherwise rewritten by this module.
    """

    source_file: str
    parser_row_number: int
    physical_line_start: int
    physical_line_end: int
    raw_fragment: str
    raw_token_array: list[str]
    parsed_column_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "source_file": self.source_file,
            "parser_row_number": self.parser_row_number,
            "physical_line_start": self.physical_line_start,
            "physical_line_end": self.physical_line_end,
            "raw_fragment": self.raw_fragment,
            "raw_token_array": list(self.raw_token_array),
            "parsed_column_count": self.parsed_column_count,
        }


@dataclass
class RecordCluster:
    """A sequence of parsed fragments belonging to one record boundary."""

    source_file: str
    record_cluster_id: int
    record_head_parser_row_number: int | None
    fragments: list[ParsedFragment] = field(default_factory=list)

    @property
    def fragment_count(self) -> int:
        return len(self.fragments)

    @property
    def has_valid_record_head(self) -> bool:
        return self.record_head_parser_row_number is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "source_file": self.source_file,
            "record_cluster_id": self.record_cluster_id,
            "record_head_parser_row_number": self.record_head_parser_row_number,
            "fragment_count": self.fragment_count,
            "fragments": [fragment.to_dict() for fragment in self.fragments],
        }


class _PhysicalLineReader:
    """Iterable wrapper that retains the exact physical lines consumed."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.physical_line_number = 0
        self._fragment_start: int | None = None
        self._fragment_lines: list[str] = []

    def __iter__(self) -> _PhysicalLineReader:
        return self

    def __next__(self) -> str:
        line = self.stream.readline()
        if line == "":
            raise StopIteration
        if not self._fragment_lines:
            self._fragment_start = self.physical_line_number + 1
        self.physical_line_number += 1
        self._fragment_lines.append(line)
        return line

    def take_fragment(self) -> tuple[int, int, str]:
        if self._fragment_start is None or not self._fragment_lines:
            raise RuntimeError("CSV parser returned a row without consumed physical lines")
        result = (
            self._fragment_start,
            self.physical_line_number,
            "".join(self._fragment_lines),
        )
        self._fragment_start = None
        self._fragment_lines = []
        return result


def _text_for_match(value: str) -> str:
    return value.strip()


def _nonempty(value: str) -> bool:
    return _text_for_match(value).lower() not in _EMPTY


def _valid_date(value: str) -> bool:
    value = _text_for_match(value)
    if not _DATE_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _valid_time(value: str) -> bool:
    value = _text_for_match(value)
    if not _TIME_RE.fullmatch(value):
        return False
    hour, minute, second = (int(part) for part in value.split(":"))
    return hour < 24 and minute < 60 and second < 60


def _valid_identifier(value: str) -> bool:
    value = _text_for_match(value)
    return bool(_ID_RE.fullmatch(value))


def is_valid_record_head(tokens: Sequence[str]) -> bool:
    """Return whether the first eight parsed fields identify a new record.

    The check intentionally does not use the total parsed column count.  A
    row with 9, 15, or more columns can start a cluster if its first eight
    fields form a valid record head; a row with any other length is attached
    to the current cluster when its first eight fields do not qualify.
    """

    if len(tokens) < RECORD_HEAD_SIZE:
        return False

    name, age, sex, patient_id, image_id, check_date, method, check_time = (
        _text_for_match(value) for value in tokens[:RECORD_HEAD_SIZE]
    )
    if not name or name == IMAGING_COLUMNS[0]:
        return False
    if not _AGE_RE.fullmatch(age):
        return False
    if sex and sex.upper() not in _SEX_VALUES:
        return False
    if not _valid_identifier(patient_id) or not _valid_identifier(image_id):
        return False
    if not _valid_date(check_date) or not _valid_time(check_time):
        return False
    if method == IMAGING_COLUMNS[6]:
        return False
    return True


def _is_header(tokens: Sequence[str]) -> bool:
    if len(tokens) < len(IMAGING_COLUMNS):
        return False
    normalized = list(tokens[: len(IMAGING_COLUMNS)])
    normalized[0] = normalized[0].lstrip("\ufeff")
    return tuple(normalized) == IMAGING_COLUMNS


def iter_imaging_fragments(
    source_file: str | Path,
    *,
    encoding: str = "gb18030",
) -> Iterator[ParsedFragment]:
    """Yield parsed data rows while retaining raw and physical-line metadata.

    The first CSV row is the 15-column header and is treated as schema
    metadata rather than a medical fragment.  ``parser_row_number`` remains
    one-based and includes that header, so the first data row is row 2.
    """

    path = Path(source_file)
    with path.open("r", encoding=encoding, newline="") as stream:
        tracked_lines = _PhysicalLineReader(stream)
        reader = csv.reader(tracked_lines)
        try:
            header = next(reader)
        except StopIteration:
            return
        tracked_lines.take_fragment()
        if not _is_header(header):
            raise ValueError(
                f"Unexpected imaging CSV header in {path}: {header!r}; "
                f"expected {list(IMAGING_COLUMNS)!r}"
            )

        for parser_row_number, tokens in enumerate(reader, start=2):
            physical_start, physical_end, raw_fragment = tracked_lines.take_fragment()
            yield ParsedFragment(
                source_file=str(path),
                parser_row_number=parser_row_number,
                physical_line_start=physical_start,
                physical_line_end=physical_end,
                raw_fragment=raw_fragment,
                raw_token_array=list(tokens),
                parsed_column_count=len(tokens),
            )


def iter_record_clusters(
    fragments: Iterable[ParsedFragment],
) -> Iterator[RecordCluster]:
    """Group fragments until the next valid eight-field record head.

    A leading fragment without a valid head is retained in cluster id 0 with
    ``has_valid_record_head=False`` so every parsed fragment has exactly one
    cluster owner.  Normal medical clusters start at id 1 and each starts
    with a valid record head.
    """

    current: RecordCluster | None = None
    next_cluster_id = 1

    for fragment in fragments:
        if is_valid_record_head(fragment.raw_token_array):
            if current is not None:
                yield current
            current = RecordCluster(
                source_file=fragment.source_file,
                record_cluster_id=next_cluster_id,
                record_head_parser_row_number=fragment.parser_row_number,
                fragments=[fragment],
            )
            next_cluster_id += 1
            continue

        if current is None:
            current = RecordCluster(
                source_file=fragment.source_file,
                record_cluster_id=0,
                record_head_parser_row_number=None,
                fragments=[],
            )
        current.fragments.append(fragment)

    if current is not None:
        yield current


def iter_imaging_record_clusters(
    source_file: str | Path,
    *,
    encoding: str = "gb18030",
) -> Iterator[RecordCluster]:
    """Stream one imaging CSV into record clusters."""

    return iter_record_clusters(iter_imaging_fragments(source_file, encoding=encoding))


def write_record_clusters_jsonl(
    source_file: str | Path,
    output_file: str | Path,
    *,
    encoding: str = "gb18030",
) -> None:
    """Write lossless clusters as UTF-8 JSONL without altering raw tokens."""

    output_path = Path(output_file)
    with output_path.open("w", encoding="utf-8", newline="\n") as stream:
        for cluster in iter_imaging_record_clusters(source_file, encoding=encoding):
            json.dump(cluster.to_dict(), stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_file", type=Path)
    parser.add_argument("output_file", type=Path)
    args = parser.parse_args(argv)
    write_record_clusters_jsonl(args.source_file, args.output_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
