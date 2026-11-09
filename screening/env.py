"""Optional project ``.env`` file -> environment variables.

``load_env()`` reads ``<project>/.env`` (see ``.env.example``) and sets only variables that are not
already set: real environment variables always win. It uses python-dotenv when installed and a
small built-in reader otherwise (``KEY=VALUE`` lines, ``export`` prefixes, quotes and ``#``
comments; no ``${VAR}`` expansion). A missing file is not an error. Values are never logged.

Call it once at startup, before modules that read settings at import time (ui/theme.py reads
APP_BRAND_EYEBROW when it is imported).
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
_MAX_ENV_BYTES = 256 * 1024

_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.*)$")
_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}


def parse_env_text(text: str) -> dict[str, str]:
    """``KEY=VALUE`` pairs of a .env file (later duplicates win). Never raises."""
    values: dict[str, str] = {}
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            while quote == '"' and end > 0 and value[end - 1] == "\\":
                end = value.find(quote, end + 1)
            if end > 0:
                value = value[1:end]
                if quote == '"':
                    value = re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), "\\" + m.group(1)), value)
            # an unterminated quote keeps the raw text
        else:
            value = _INLINE_COMMENT_RE.sub("", value).strip()
        values[key] = value
    return values


def load_env(path: str | os.PathLike[str] | None = None) -> list[str]:
    """Load ``path`` (default ``<project>/.env``) without overriding existing variables.

    -> sorted names of the variables that were newly set (never their values). Never raises.
    """
    target = Path(path) if path is not None else DEFAULT_ENV_FILE
    try:
        if not target.is_file() or target.stat().st_size > _MAX_ENV_BYTES:
            return []
    except OSError:
        return []
    before = set(os.environ)
    try:
        try:
            from dotenv import load_dotenv  # type: ignore[import-not-found]
        except ImportError:
            load_dotenv = None
        if load_dotenv is not None:
            load_dotenv(dotenv_path=target, override=False, encoding="utf-8-sig")
        else:
            for key, value in parse_env_text(target.read_text(encoding="utf-8-sig")).items():
                if key not in os.environ:
                    os.environ[key] = value
    except Exception as exc:  # noqa: BLE001 - a broken .env must not stop the app
        log.warning("Could not read %s (%s); continuing without it", target.name, type(exc).__name__)
        return []
    added = sorted(set(os.environ) - before)
    if added:
        log.info("Loaded %d setting(s) from %s", len(added), target.name)
    return added


__all__ = ["DEFAULT_ENV_FILE", "load_env", "parse_env_text"]
