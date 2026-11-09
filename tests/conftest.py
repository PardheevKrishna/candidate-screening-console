"""Shared test isolation.

Tests never see the machine's real Copilot proxy: a running VS Code extension writes a
connection file (token + address) to the home folder, and a developer may keep a project
.env or a COPILOT_PROXY_TOKEN variable. Each test starts without any of them; tests that
exercise discovery or .env loading point these at their own temporary files.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from screening import env, llm_backends


@pytest.fixture(autouse=True)
def _no_real_proxy_or_env(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    isolated: Path = tmp_path_factory.mktemp("isolated-home")
    monkeypatch.delenv("COPILOT_PROXY_TOKEN", raising=False)
    monkeypatch.setattr(llm_backends, "discovery_path", lambda: isolated / llm_backends.DISCOVERY_FILE_NAME)
    monkeypatch.setattr(env, "DEFAULT_ENV_FILE", isolated / ".env")
