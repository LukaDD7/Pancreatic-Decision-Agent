from __future__ import annotations

import os
from dataclasses import dataclass, field
import json
from pathlib import Path
import shlex
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelAPISettings:
    api_key: str = field(repr=False)
    api_url: str
    model: str
    thinking: str | None = None

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


def read_env_file(path: Path) -> dict[str, str]:
    """Read literal assignments only; never source or execute a credential file."""
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        words = shlex.split(line, comments=True)
        if words and words[0] == "export":
            words = words[1:]
        if len(words) == 1 and "=" in words[0]:
            key, value = words[0].split("=", 1)
            if "$" not in value and "`" not in value:
                values[key] = value
    return values


def load_boyu_settings(repo: Path | None = None, *, environ=None,
                       credential_file: Path | None = None) -> ModelAPISettings:
    """Shared explicit Boyu profile for P2 and subsequent automation.

    Generic LLM_* callers retain their original explicit environment contract.
    The provider profile contains no credentials or endpoint. Process overrides
    take precedence over project-local settings and the existing external file.
    """
    repo = repo or Path(__file__).resolve().parents[2]
    env = os.environ if environ is None else environ
    profile = json.loads((repo / "config/model_provider.json").read_text())
    if profile["provider"] != "boyu":
        raise ValueError("unsupported provider profile")
    credential_file = credential_file or Path(env.get(
        "BOYU_CREDENTIAL_FILE", "~/.config/pancreatic-decision-agent/credentials.zsh")).expanduser()
    values = read_env_file(credential_file)
    values.update(read_env_file(repo / ".env.local"))
    values.update({k: v for k, v in env.items() if k.startswith("BOYU_") and v})
    key, base = values.get("BOYU_API_KEY", "").strip(), values.get("BOYU_API_BASE", "").strip()
    if not key or not base:
        raise RuntimeError("missing Boyu settings: BOYU_API_KEY / BOYU_API_BASE")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Boyu endpoint must be HTTPS without credentials, query or fragment")
    return ModelAPISettings(key, base, values.get("BOYU_MODEL", profile["model"]), profile.get("thinking"))
