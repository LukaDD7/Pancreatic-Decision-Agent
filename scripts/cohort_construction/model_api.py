from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelAPISettings:
    api_key: str
    api_url: str
    model: str

    @property
    def chat_completions_url(self) -> str:
        """Accept the same base URL as the SDK, plus legacy full endpoints."""
        url = self.api_url.rstrip("/")
        return url if url.endswith("/chat/completions") else url + "/chat/completions"


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
