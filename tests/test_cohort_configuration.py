from pathlib import Path

import pytest

from scripts.cohort_construction.model_api import load_model_api_settings
from scripts.cohort_construction.paths import data_root


def test_data_root_comes_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("PANCREATIC_DATA_ROOT", str(tmp_path))
    assert data_root() == Path(tmp_path).resolve()


def test_model_api_uses_generic_environment_names(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "placeholder")
    monkeypatch.setenv("LLM_API_URL", "https://example.invalid/v1/chat/completions")
    monkeypatch.setenv("LLM_MODEL", "placeholder-model")
    settings = load_model_api_settings()
    assert settings.api_key == "placeholder"
    assert settings.api_url.startswith("https://example.invalid/")
    assert settings.model == "placeholder-model"


def test_model_api_rejects_missing_settings(monkeypatch):
    for name in ("LLM_API_KEY", "LLM_API_URL", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="missing model API settings"):
        load_model_api_settings()
