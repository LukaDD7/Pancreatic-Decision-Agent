"""Explicit-file-list batch audit/output for imaging record clusters.

The batch layer never discovers files.  Callers provide the exact input list;
an optional allowed-file manifest can reject any path outside that manifest.
Audit mode emits aggregate metadata only.  Full mode writes one JSONL file per
source CSV and requires an explicit output directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Sequence

from .imaging_record_cluster import (
    is_valid_record_head,
    iter_imaging_fragments,
    iter_imaging_record_clusters,
)


MODES = {"audit", "full"}
WILDCARD_CHARS = "*?["


def _as_path_list(value: Iterable[str | Path] | str | Path, name: str) -> list[Path]:
    if isinstance(value, (str, Path)):
        return [Path(value)]
    paths = [Path(item) for item in value]
    if not paths:
        raise ValueError(f"{name} must contain at least one explicit file")
    return paths


def _path_key(path: Path) -> str:
    return str(path.resolve(strict=False)).casefold()


def _contains_wildcard(path: Path) -> bool:
    return any(character in str(path) for character in WILDCARD_CHARS)


def _file_sha256(path: Path) -> tuple[int, str]:
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return size, digest.hexdigest()


def _update_token_fingerprint(digest: hashlib._Hash, parsed_column_count: int, tokens: Sequence[str]) -> None:
    digest.update(parsed_column_count.to_bytes(8, "little"))
    for token in tokens:
        encoded = token.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)
    digest.update(b"\xff")


def _age_unit(value: str) -> str:
    age = value.strip()
    if age.endswith("月"):
        return "month"
    if age.endswith("天"):
        return "day"
    if age.endswith("岁") or age.endswith("周岁"):
        return "year"
    return "other"


def _empty_metrics() -> dict[str, object]:
    return {
        "parsed_fragment_count": None,
        "record_cluster_count": None,
        "fragment_count_sum": None,
        "valid_head_count": None,
        "leading_orphan_fragment_count": None,
        "age_record_counts": None,
        "column_distribution": None,
        "multi_fragment_cluster_count": None,
        "max_fragment_count": None,
        "fragment_count_conservation": None,
        "token_conservation": None,
    }


def _scan_fragments(path: Path) -> tuple[dict[str, object], str]:
    parsed_count = 0
    valid_head_count = 0
    columns = Counter()
    age_counts = Counter()
    digest = hashlib.blake2b(digest_size=32)

    for fragment in iter_imaging_fragments(path):
        parsed_count += 1
        columns[fragment.parsed_column_count] += 1
        _update_token_fingerprint(digest, fragment.parsed_column_count, fragment.raw_token_array)
        if is_valid_record_head(fragment.raw_token_array):
            valid_head_count += 1
            age_counts[_age_unit(fragment.raw_token_array[1])] += 1

    metrics = {
        "parsed_fragment_count": parsed_count,
        "valid_head_count": valid_head_count,
        "age_record_counts": {
            "month": age_counts["month"],
            "day": age_counts["day"],
            "year": age_counts["year"],
        },
        "column_distribution": dict(sorted(columns.items())),
    }
    return metrics, digest.hexdigest()


def _cluster_file(
    path: Path,
    *,
    output_path: Path | None = None,
) -> tuple[dict[str, object], str]:
    cluster_count = 0
    fragment_sum = 0
    multi_fragment_count = 0
    max_fragment_count = 0
    leading_orphan_fragment_count = 0
    second_pass_count = 0
    first_cluster = True
    digest = hashlib.blake2b(digest_size=32)
    stream = None
    temp_path = None

    try:
        if output_path is not None:
            temp_path = output_path.with_name(output_path.name + ".tmp")
            stream = temp_path.open("w", encoding="utf-8", newline="\n")

        for cluster in iter_imaging_record_clusters(path):
            cluster_count += 1
            fragment_sum += cluster.fragment_count
            if cluster.fragment_count > 1:
                multi_fragment_count += 1
            max_fragment_count = max(max_fragment_count, cluster.fragment_count)
            if first_cluster:
                first_cluster = False
                if not cluster.has_valid_record_head:
                    leading_orphan_fragment_count = cluster.fragment_count

            if stream is not None:
                json.dump(cluster.to_dict(), stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")

            for fragment in cluster.fragments:
                second_pass_count += 1
                _update_token_fingerprint(digest, fragment.parsed_column_count, fragment.raw_token_array)

        if stream is not None:
            stream.close()
            stream = None
            assert temp_path is not None
            temp_path.replace(output_path)
            temp_path = None
    except Exception:
        if stream is not None:
            stream.close()
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise

    metrics = {
        "record_cluster_count": cluster_count,
        "fragment_count_sum": fragment_sum,
        "leading_orphan_fragment_count": leading_orphan_fragment_count,
        "multi_fragment_cluster_count": multi_fragment_count,
        "max_fragment_count": max_fragment_count,
        "second_pass_fragment_count": second_pass_count,
    }
    return metrics, digest.hexdigest()


def _failure_record(
    path: Path,
    status: str,
    reason: str,
    *,
    file_size: int | None = None,
    sha256: str | None = None,
    output_file: Path | None = None,
) -> dict[str, object]:
    record = {
        "source_file": str(path),
        "source_csv_name": path.name,
        "status": status,
        "failure_reason": reason,
        "file_size": file_size,
        "sha256": sha256,
        "output_file": str(output_file) if output_file is not None else None,
    }
    record.update(_empty_metrics())
    return record


def _process_one_file(
    path: Path,
    *,
    mode: str,
    output_dir: Path | None,
) -> dict[str, object]:
    output_path = None
    file_size = None
    sha256 = None
    if mode == "full":
        assert output_dir is not None
        output_path = output_dir / f"{path.name}.record_clusters.jsonl"

    try:
        if not path.is_file():
            raise FileNotFoundError(f"not a regular file: {path}")
        file_size, sha256 = _file_sha256(path)
        scan_metrics, first_token_hash = _scan_fragments(path)
        cluster_metrics, second_token_hash = _cluster_file(path, output_path=output_path)

        parsed_count = scan_metrics["parsed_fragment_count"]
        fragment_sum = cluster_metrics["fragment_count_sum"]
        second_pass_count = cluster_metrics["second_pass_fragment_count"]
        fragment_count_conservation = parsed_count == fragment_sum == second_pass_count
        token_conservation = first_token_hash == second_token_hash and fragment_count_conservation

        record = {
            "source_file": str(path),
            "source_csv_name": path.name,
            "status": "success",
            "failure_reason": None,
            "file_size": file_size,
            "sha256": sha256,
            "output_file": str(output_path) if output_path is not None else None,
        }
        record.update(
            {
                "parsed_fragment_count": parsed_count,
                "record_cluster_count": cluster_metrics["record_cluster_count"],
                "fragment_count_sum": fragment_sum,
                "valid_head_count": scan_metrics["valid_head_count"],
                "leading_orphan_fragment_count": cluster_metrics["leading_orphan_fragment_count"],
                "age_record_counts": scan_metrics["age_record_counts"],
                "column_distribution": scan_metrics["column_distribution"],
                "multi_fragment_cluster_count": cluster_metrics["multi_fragment_cluster_count"],
                "max_fragment_count": cluster_metrics["max_fragment_count"],
                "fragment_count_conservation": fragment_count_conservation,
                "token_conservation": token_conservation,
            }
        )
        return record
    except Exception as exc:
        return _failure_record(
            path,
            "failed",
            f"{type(exc).__name__}: {exc}",
            file_size=file_size,
            sha256=sha256,
            output_file=output_path,
        )


def run_batch(
    input_files: Sequence[str | Path] | str | Path,
    *,
    allowed_files: Sequence[str | Path] | str | Path | None = None,
    mode: str = "audit",
    output_dir: str | Path | None = None,
) -> dict[str, object]:
    """Process only an explicit file list, one file at a time.

    ``allowed_files`` is an optional explicit manifest.  When supplied, an
    input path not in that manifest is rejected before existence checks,
    metadata reads, hashing, or CSV parsing.  When omitted, the input list
    itself is the manifest.  No directory or wildcard expansion is performed.
    """

    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    paths = _as_path_list(input_files, "input_files")
    allowed_paths = paths if allowed_files is None else _as_path_list(allowed_files, "allowed_files")
    allowed_keys = {_path_key(path) for path in allowed_paths}

    output_path = None
    if mode == "full":
        if output_dir is None:
            raise ValueError("full mode requires an explicit output_dir")
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    for path in paths:
        key = _path_key(path)
        if _contains_wildcard(path):
            records.append(_failure_record(path, "rejected", "wildcard paths are not allowed"))
            continue
        if key not in allowed_keys:
            records.append(_failure_record(path, "rejected", "file is not in the explicit allowed manifest"))
            continue
        if key in seen_keys:
            records.append(_failure_record(path, "rejected", "duplicate file in explicit input list"))
            continue
        seen_keys.add(key)
        records.append(_process_one_file(path, mode=mode, output_dir=output_path))

    successful = [record for record in records if record["status"] == "success"]
    failed = [record for record in records if record["status"] != "success"]
    summary = {
        "requested_file_count": len(paths),
        "reported_file_count": len(records),
        "successful_file_count": len(successful),
        "failed_file_count": len(failed),
        "batch_complete": len(failed) == 0,
        "total_parsed_fragment_count": sum(record["parsed_fragment_count"] for record in successful),
        "total_record_cluster_count": sum(record["record_cluster_count"] for record in successful),
        "total_fragment_count_sum": sum(record["fragment_count_sum"] for record in successful),
        "all_fragment_count_conservation": bool(successful)
        and len(failed) == 0
        and all(record["fragment_count_conservation"] for record in successful),
        "all_token_conservation": bool(successful)
        and len(failed) == 0
        and all(record["token_conservation"] for record in successful),
    }
    return {"files": records, "batch_summary": summary}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-file",
        dest="input_files",
        action="append",
        required=True,
        help="one explicit CSV path; repeat for each file; no glob expansion",
    )
    parser.add_argument(
        "--allowed-file",
        dest="allowed_files",
        action="append",
        help="optional explicit allowlist; repeat for each allowed CSV path",
    )
    parser.add_argument("--mode", choices=sorted(MODES), default="audit")
    parser.add_argument("--output-dir", type=Path, help="required explicitly in full mode")
    args = parser.parse_args(argv)

    try:
        report = run_batch(
            args.input_files,
            allowed_files=args.allowed_files,
            mode=args.mode,
            output_dir=args.output_dir,
        )
    except ValueError as exc:
        parser.error(str(exc))
        return 2

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["batch_summary"]["batch_complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
