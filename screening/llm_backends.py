"""LLM backend: the ONLY module (with the LLM_PROVIDERS preset in config.py) that knows
which vendor/client the app talks to.

screening.llm.LLMClient needs a chat-completions style client:
``client.chat.completions.create(model=, messages=, temperature=, max_tokens=)`` returning
``resp.choices[0].message.content``. This module provides:

* ``build_chat_client``  - constructs that client for ``cfg.provider``
  (today: the hosted NVIDIA endpoint through the ``openai`` SDK);
* ``classify_error`` / ``describe_error`` - vendor exceptions -> retry / fallback / fatal;
* ``KEY_PREFIXES`` / ``KEY_PATTERNS`` - key formats to mask in any user-facing text;
* ``TachyonShim`` - adapter that makes the internal TachyonClient look like that client.

Switching vendors: docs/SWITCH_TO_TACHYON.md.
"""
from __future__ import annotations

import importlib
import re
from types import SimpleNamespace
from typing import Any, Callable

from .config import LLM_PROVIDERS, LLMConfig

# ---------------------------------------------------------------------------
# Provider identity & key hygiene (provider-specific values live ONLY here and in config.py)
# ---------------------------------------------------------------------------
KEY_PREFIXES: tuple[str, ...] = ("nvapi-",)          # shown in masked key hints
KEY_PATTERNS: tuple[re.Pattern[str], ...] = (        # scrubbed from any user-facing text
    re.compile(r"nvapi-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
)

# Error classes returned by classify_error()
FATAL = "fatal"                          # bad key / no permission: stop the batch
MODEL_UNAVAILABLE = "model_unavailable"  # 404 / 410 (retired model): try the next fallback model
TRANSIENT = "transient"                  # 429 / timeouts / connection / 5xx: back off and retry
OTHER = "other"                          # bad request etc.: fail this item only
_TRANSIENT_STATUS = frozenset({408, 409, 425, 429})


def provider_label(cfg: LLMConfig) -> str:
    return str(LLM_PROVIDERS.get(cfg.provider, {}).get("label", "LLM provider"))


def _openai():
    try:
        import openai
    except ImportError:  # pragma: no cover - only when the openai package is removed
        return None
    return openai


def classify_error(exc: BaseException) -> str:
    """Map a client exception to FATAL / MODEL_UNAVAILABLE / TRANSIENT / OTHER."""
    status = getattr(exc, "status_code", None)
    oa = _openai()
    if oa is not None:
        if isinstance(exc, (oa.AuthenticationError, oa.PermissionDeniedError)):
            return FATAL
        if isinstance(exc, oa.NotFoundError) or status == 410:
            return MODEL_UNAVAILABLE
        if isinstance(exc, (oa.RateLimitError, oa.APITimeoutError, oa.APIConnectionError,
                            oa.InternalServerError)):
            return TRANSIENT
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return TRANSIENT
    if isinstance(status, int):      # any HTTP-style error, including other wrappers
        if status in (401, 403):
            return FATAL
        if status in (404, 410):
            return MODEL_UNAVAILABLE
        if status in _TRANSIENT_STATUS or status >= 500:
            return TRANSIENT
    return OTHER


def describe_error(exc: BaseException, cfg: LLMConfig, *, model: str | None = None,
                   detail: str = "") -> str:
    """Friendly, key-free message for a client exception."""
    label, model = provider_label(cfg), model or cfg.model
    where = cfg.base_url or label
    status = getattr(exc, "status_code", None)
    oa = _openai()
    if oa is not None:
        if isinstance(exc, oa.AuthenticationError):
            return f"{label} rejected the API key (401). Check {cfg.api_key_env}."
        if isinstance(exc, oa.PermissionDeniedError):
            return f"The API key is not allowed to use model '{model}' (403)."
        if isinstance(exc, oa.RateLimitError):
            return f"{label} rate limit reached (429); try again shortly or lower max concurrency."
        if isinstance(exc, oa.APITimeoutError):
            return f"{label} did not respond within {cfg.timeout_sec:g} s."
        if isinstance(exc, oa.APIConnectionError):
            return f"Could not connect to {where} (network, proxy or firewall?)."
    if classify_error(exc) == MODEL_UNAVAILABLE:
        return f"Model '{model}' is not available at {where} (HTTP {status or 404}; retired or no access)."
    if isinstance(status, int):
        return f"{label} returned HTTP {status}" + (f": {detail}" if detail else ".")
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return f"Network error talking to {label} ({type(exc).__name__})."
    return f"Unexpected LLM client error ({type(exc).__name__})."


def build_chat_client(cfg: LLMConfig, api_key: str | None) -> Any:
    """Return an OpenAI-shaped client for cfg.provider."""
    if cfg.provider == "tachyon":
        return TachyonShim(cfg, api_key)
    from openai import OpenAI  # local import keeps the engine importable without openai

    if not api_key:
        raise ValueError(f"No API key: set {cfg.api_key_env} in the environment or .streamlit/secrets.toml")
    client = OpenAI(base_url=cfg.base_url, api_key=api_key, timeout=cfg.timeout_sec, max_retries=0)
    extras = dict(getattr(cfg, "request_extras", None) or {})
    return _WithRequestExtras(client, extras) if extras else client


class _WithRequestExtras:
    """Adds vendor-specific body fields (cfg.request_extras) to every chat request."""

    def __init__(self, client: Any, extras: dict[str, Any]) -> None:
        self._client = client
        self._extras = extras
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        body = {**self._extras, **(kwargs.pop("extra_body", None) or {})}
        return self._client.chat.completions.create(extra_body=body, **kwargs)

    def __getattr__(self, name: str) -> Any:   # max_retries, base_url, models, ...
        return getattr(self._client, name)


def _load_factory(path: str) -> Callable[..., Any]:
    """'package.module:Attr' -> the attribute (class or factory function)."""
    if ":" not in path:
        raise ValueError("backend_factory must look like 'package.module:ClassName'")
    module_name, attr = path.split(":", 1)
    return getattr(importlib.import_module(module_name), attr)


class TachyonShim:
    """Makes the internal TachyonClient look like ``openai.OpenAI`` for screening.llm.

    Adapting to TachyonClient = editing ``__init__`` (construction) and ``_call``
    (one request). Everything else (retries, JSON parsing, caching, redaction,
    batching) is handled by screening.llm and stays unchanged.
    """

    def __init__(self, cfg: LLMConfig, api_key: str | None) -> None:
        if not cfg.backend_factory:
            raise ValueError("Set LLMConfig.backend_factory, e.g. 'tachyon:TachyonClient'")
        factory = _load_factory(cfg.backend_factory)
        # --- ADAPT: constructor arguments of TachyonClient -------------------
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self._client = factory(**kwargs)
        # ---------------------------------------------------------------------
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _call(self, *, model: str, messages: list[dict[str, str]], temperature: float,
              max_tokens: int) -> Any:
        # --- ADAPT: one chat request with TachyonClient -----------------------
        # Tries the most common wrapper shapes; replace with the exact call.
        c = self._client
        for name in ("chat", "complete", "generate", "invoke"):
            fn = getattr(c, name, None)
            if callable(fn):
                return fn(messages=messages, model=model, temperature=temperature, max_tokens=max_tokens)
        if hasattr(c, "chat") and hasattr(c.chat, "completions"):  # already OpenAI-shaped
            return c.chat.completions.create(model=model, messages=messages,
                                             temperature=temperature, max_tokens=max_tokens)
        raise TypeError("TachyonClient has no chat/complete/generate/invoke method - edit TachyonShim._call")
        # ---------------------------------------------------------------------

    def _create(self, *, model: str, messages: list[dict[str, str]], temperature: float = 0.2,
                max_tokens: int = 700, **_: Any) -> Any:
        raw = self._call(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
        return SimpleNamespace(
            model=model,
            usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content=extract_text(raw)), finish_reason="stop")],
        )


def extract_text(raw: Any) -> str:
    """Best-effort text extraction from common response shapes."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    choices = getattr(raw, "choices", None) or (raw.get("choices") if isinstance(raw, dict) else None)
    if choices:
        first = choices[0]
        msg = getattr(first, "message", None) or (first.get("message") if isinstance(first, dict) else None)
        if msg is not None:
            content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
            if content:
                return str(content)
        text = getattr(first, "text", None) or (first.get("text") if isinstance(first, dict) else None)
        if text:
            return str(text)
    for key in ("content", "text", "output", "output_text", "response", "answer", "result"):
        val = raw.get(key) if isinstance(raw, dict) else getattr(raw, key, None)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, (dict, list)) and val:
            nested = extract_text(val if isinstance(val, dict) else val[0])
            if nested:
                return nested
    return str(raw)
