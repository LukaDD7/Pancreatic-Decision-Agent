#!/usr/bin/env python3
"""Local Git guard; report file names only, never secret or patient excerpts."""

import argparse
import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERN = re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b")
SENSITIVE_DIRECTORIES = {"data", "private", "outputs", ".venv", ".secrets", "secrets", "credentials"}
LOCAL_DATA_SUFFIXES = {".jsonl", ".csv", ".tsv", ".xlsx", ".xls", ".parquet"}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def patient_identifiers():
    # Optional local audit only. These identifiers are never printed or sent out.
    path = ROOT / "data/100例/structured/patient_source_bundles_100.jsonl"
    if not path.exists():
        return set()
    import json
    identifiers = set()
    with path.open() as f:
        for line in f:
            case = json.loads(line)
            if case.get("case_id"):
                identifiers.add(case["case_id"].encode())
            uid = case.get("state_pre_T0", {}).get("patient_uid")
            if uid:
                identifiers.add(uid.encode())
    return identifiers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    key = os.environ.get("LLM_API_KEY", "").encode()
    identifiers = patient_identifiers()
    violations = []
    if args.history:
        for item in git("rev-list", "--objects", "--all").splitlines():
            oid = item.split(b" ", 1)[0].decode()
            if git("cat-file", "-t", oid).strip() != b"blob":
                continue
            blob = git("cat-file", "blob", oid)
            if (key and key in blob) or SECRET_PATTERN.search(blob) or any(i in blob for i in identifiers):
                violations.append("Sensitive content in reachable Git history")
    names = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z") if args.staged else git("ls-files", "-z")
    for name in names.decode().split("\0"):
        if not name:
            continue
        path = Path(name)
        if any(part in SENSITIVE_DIRECTORIES for part in path.parts) or (path.name.startswith(".env") and path.name != ".env.example") or path.suffix in LOCAL_DATA_SUFFIXES:
            violations.append("Sensitive local-data path is staged/tracked")
            continue
        try:
            content = git("show", ":" + name) if args.staged else (ROOT / name).read_bytes()
        except (OSError, subprocess.CalledProcessError):
            violations.append("Could not inspect a tracked/staged file")
            continue
        if (key and key in content) or SECRET_PATTERN.search(content) or any(i in content for i in identifiers):
            violations.append(f"Sensitive content detected in {name}")
    if violations:
        print("Repository safety check failed:")
        for message in sorted(set(violations)):
            print(message)
        return 1
    print("Repository safety check passed: no protected data paths, detected keys or local patient identifiers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
