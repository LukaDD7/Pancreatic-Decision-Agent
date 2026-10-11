"""Create a LOCAL clinical-data handoff archive; never stage or upload it."""

import argparse
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import tarfile

from pancreatic_agent.phase_a import now, verify_dataset
from pancreatic_agent.four_stage import ROOT, protected_output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--history", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    dataset = args.dataset.resolve()
    manifest = verify_dataset(dataset)
    target = protected_output(args.output)
    if not target.name.endswith(".tar.gz") or target.exists():
        raise ValueError("Use a new .tar.gz path inside private/ or outputs/")
    roots = [(dataset, "dataset")]
    for history in args.history:
        history = history.resolve()
        protected_output(history)
        roots.append((history, "history/" + history.relative_to(ROOT).as_posix()))
    if any(target.is_relative_to(root) for root, _ in roots):
        raise ValueError("Archive output cannot be inside an archived directory")
    keys = [v.encode() for k, v in os.environ.items() if k.endswith("API_KEY") and v]
    inventory = []
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with tarfile.open(target, "w:gz") as archive:
            for root, prefix in roots:
                if not root.is_dir():
                    raise ValueError("Handoff input must be a directory")
                for path in sorted(root.rglob("*")):
                    if path.is_symlink():
                        raise ValueError("Symlinks are not accepted in handoff archives")
                    if not path.is_file() or ".DS_Store" == path.name or "__pycache__" in path.parts:
                        continue
                    if path.name.startswith(".env") or path.suffix == ".pyc":
                        continue
                    data = path.read_bytes()
                    if any(key in data for key in keys) or re.search(rb"\bsk-[A-Za-z0-9_-]{20,}\b", data):
                        raise ValueError("Credential pattern detected; archive aborted")
                    name = prefix + "/" + path.relative_to(root).as_posix()
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(data), 0o600
                    archive.addfile(info, io.BytesIO(data))
                    inventory.append({"path": name, "bytes": len(data), "sha256": sha256(data).hexdigest()})
            index = json.dumps({"created_at": now(), "contains_patient_data": True,
                                "episodes": manifest["episodes"], "slots": manifest["slots"],
                                "runnable_samples": manifest["runnable_samples"],
                                "unresolved_samples": manifest["unresolved_samples"], "files": inventory},
                               ensure_ascii=False, indent=2).encode()
            info = tarfile.TarInfo("handoff_inventory.json")
            info.size, info.mode = len(index), 0o600
            archive.addfile(info, io.BytesIO(index))
            note = ("本地真实临床样本交接包；仅通过约定渠道交给获授权的研究者。\n"
                    "配套代码和运行说明见公开仓库 docs/phase-a-handoff.md。\n"
                    "解包到仓库 private/phase-a-handoff/；dataset/为可搬迁冻结输入。\n"
                    "history/保留原pilot脚本、复核、API请求、原始回答、失败与统计。\n"
                    "先运行 python -m pancreatic_agent.phase_a verify --dataset private/phase-a-handoff/dataset。\n"
                    "历史文件中的本机绝对路径仅用于留档；新入口只用dataset相对路径。\n"
                    "探索性复核不是独立临床金标准；后见结果只供离线审阅，不作为测试输入。\n").encode()
            info = tarfile.TarInfo("READ_ME.txt")
            info.size, info.mode = len(note), 0o600
            archive.addfile(info, io.BytesIO(note))
        target.chmod(0o600)
        # Verify every archived byte against the inventory, without extracting patient files.
        with tarfile.open(target, "r:gz") as archive:
            for row in inventory:
                data = archive.extractfile(row["path"]).read()
                if sha256(data).hexdigest() != row["sha256"] or len(data) != row["bytes"]:
                    raise ValueError("Archive verification failed")
        print(json.dumps({"archive": str(target), "files": len(inventory), "bytes": target.stat().st_size,
                          "archive_sha256": sha256(target.read_bytes()).hexdigest()}, ensure_ascii=False))
        return 0
    except BaseException:
        target.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
