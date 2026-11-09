"""Uploaded tuning profiles must never change where the LLM connects; .env files never override
the real environment; dependencies stay on the local Copilot proxy."""
from __future__ import annotations

import json
import os
import re
import sys
import types
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from screening import env
from screening.config import LLM_CONNECTION_FIELDS, AppConfig

PROJECT = Path(__file__).resolve().parent.parent


def test_profile_cannot_redirect_llm_endpoint_or_token():
    hostile = AppConfig().to_dict()
    hostile["llm"].update(base_url="https://attacker.example.net/v1", provider="other", api_key_env="HOME",
                          cache_dir="C:/Windows", fallback_models=["evil/model"])
    hostile["llm"].update(backend_factory="os:system", request_extras={"x": 1},   # retired fields
                          blend_weight=0.3, rescore_near_cutoff=True)
    hostile["weights"]["hackerrank"] = 55.0          # tuning values still apply
    hostile["llm"]["borderline_band"] = 20
    hostile["llm"]["model"] = "gpt-4.1"              # picks among Copilot models only
    cfg = AppConfig.from_json(json.dumps(hostile))
    default = AppConfig().llm
    for name in LLM_CONNECTION_FIELDS:
        assert getattr(cfg.llm, name) == getattr(default, name), name
    for retired in ("backend_factory", "request_extras", "blend_weight", "rescore_near_cutoff", "cutoff_band"):
        assert not hasattr(cfg.llm, retired), retired
    assert cfg.weights.hackerrank == 55.0 and cfg.llm.borderline_band == 20 and cfg.llm.model == "gpt-4.1"


def test_default_llm_endpoint_is_the_local_copilot_proxy():
    llm_cfg = AppConfig().llm
    assert llm_cfg.provider == "copilot"
    assert urlsplit(llm_cfg.base_url).hostname == "127.0.0.1"
    assert llm_cfg.api_key_env == "COPILOT_PROXY_TOKEN"
    assert llm_cfg.model == ""                       # proxy default
    assert {"provider", "base_url", "api_key_env"} <= LLM_CONNECTION_FIELDS


def test_default_profile_round_trips():
    assert AppConfig.from_json(AppConfig().to_json()) == AppConfig()


# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------
ENV_TEXT = """\
# comment line
export SCREENING_TEST_PLAIN=plain value  # trailing comment
SCREENING_TEST_QUOTED="has # hash and \\"quotes\\"\\nnext"
SCREENING_TEST_SINGLE='single # kept'
SCREENING_TEST_EXISTING=from-file
SCREENING_TEST_EMPTY=
not a setting line
"""
ENV_NAMES = ("SCREENING_TEST_PLAIN", "SCREENING_TEST_QUOTED", "SCREENING_TEST_SINGLE",
             "SCREENING_TEST_EXISTING", "SCREENING_TEST_EMPTY")


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(sys.modules, "dotenv", None)   # force the built-in reader
    return monkeypatch


def test_parse_env_text():
    values = env.parse_env_text(ENV_TEXT)
    assert values == {
        "SCREENING_TEST_PLAIN": "plain value",
        "SCREENING_TEST_QUOTED": 'has # hash and "quotes"\nnext',
        "SCREENING_TEST_SINGLE": "single # kept",
        "SCREENING_TEST_EXISTING": "from-file",
        "SCREENING_TEST_EMPTY": "",
    }
    assert env.parse_env_text("") == {} and env.parse_env_text("KEY='unterminated") == {"KEY": "'unterminated"}


def test_load_env_never_overrides_existing_variables(clean_env, tmp_path):
    path = tmp_path / ".env"
    path.write_text("\ufeff" + ENV_TEXT, encoding="utf-8")       # Notepad-style BOM is fine
    clean_env.setenv("SCREENING_TEST_EXISTING", "from-environment")
    added = env.load_env(path)
    assert os.environ["SCREENING_TEST_EXISTING"] == "from-environment"
    assert os.environ["SCREENING_TEST_PLAIN"] == "plain value"
    assert os.environ["SCREENING_TEST_SINGLE"] == "single # kept"
    assert "SCREENING_TEST_EXISTING" not in {n.upper() for n in added}
    assert {"SCREENING_TEST_PLAIN", "SCREENING_TEST_QUOTED"} <= {n.upper() for n in added}
    assert env.load_env(path) == []                              # second call: nothing new


def test_load_env_missing_or_unreadable_file_is_silent(clean_env, tmp_path):
    assert env.load_env(tmp_path / "missing.env") == []
    assert env.load_env(tmp_path) == []                          # a folder, not a file
    assert env.PROJECT_ROOT / ".env" == PROJECT / ".env"            # default file (tests point it elsewhere)


def test_load_env_uses_python_dotenv_when_installed(clean_env, tmp_path):
    calls: list[dict] = []

    def fake_load_dotenv(**kwargs):
        calls.append(kwargs)
        os.environ.setdefault("SCREENING_TEST_PLAIN", "via-dotenv")
        return True

    clean_env.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=fake_load_dotenv))
    path = tmp_path / ".env"
    path.write_text("SCREENING_TEST_PLAIN=via-dotenv\n", encoding="utf-8")
    assert [n.upper() for n in env.load_env(path)] == ["SCREENING_TEST_PLAIN"]
    assert calls == [{"dotenv_path": path, "override": False, "encoding": "utf-8-sig"}]


def test_env_example_documents_settings_without_secrets():
    text = (PROJECT / ".env.example").read_text(encoding="utf-8")
    for name in ("COPILOT_PROXY_TOKEN", "SCREENING_RUNS_DIR", "APP_BRAND_EYEBROW"):
        assert name in text
    assert not re.search(r"[0-9A-Fa-f]{32,}", text)              # no token-like values
    assert not [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
def test_requirements_use_ranges_and_the_proxy_client():
    lines = [ln.split("#", 1)[0].strip() for ln in (PROJECT / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    reqs = {re.split(r"[<>=!~ ]", ln, 1)[0].lower(): ln for ln in lines if ln}
    assert "openai" not in reqs
    assert {"requests", "python-dotenv", "pandas", "streamlit", "pytest"} <= set(reqs)
    assert not [ln for ln in reqs.values() if "==" in ln]          # compatible ranges, not exact pins
    assert all(">=" in ln and "<" in ln for ln in reqs.values())
