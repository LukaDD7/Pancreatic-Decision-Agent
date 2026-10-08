from __future__ import annotations

import os
from pathlib import Path


def data_root() -> Path:
    value = os.environ.get("PANCREATIC_DATA_ROOT", "").strip()
    if not value:
        raise RuntimeError("PANCREATIC_DATA_ROOT is required")
    return Path(value).expanduser().resolve()
