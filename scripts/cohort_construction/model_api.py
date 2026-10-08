from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelAPISettings:
    api_key: str
    api_url: str
    model: str


def load_model_api_settings() -> ModelAPISettings:
    values = {
        "api_key": os.environ.get("LLM_API_KEY", "").strip(),
        "api_url": os.environ.get("LLM_API_URL", "").strip(),
        "model": os.environ.get("LLM_MODEL", "").strip(),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"missing model API settings: {', '.join(missing)}")
    return ModelAPISettings(**values)
