# Switching the LLM layer to the internal TachyonClient

This guide moves every LLM feature (re-scoring near the cutoff, candidate rationales,
interview and integrity questions, risk-group summaries, the executive summary) from the
hosted NVIDIA endpoint to the internal **TachyonClient**. When you're done, the codebase has
**no references to NVIDIA, OpenAI, Gemini or any other wrapper**.

It takes about 30–45 minutes, mostly steps 2 and 5.

---

## How the LLM layer is built

```
UI buttons / pipeline.run_llm_rescore
        │
        ▼
screening/llm.py        LLMClient + features        ← provider-neutral (no edits needed)
  prompts · PII redaction · JSON parsing · retries/backoff · model fallback ·
  disk cache · thread-pool batching · progress callbacks
        │  client.chat.completions.create(model=, messages=, temperature=, max_tokens=)
        │  -> resp.choices[0].message.content
        ▼
screening/llm_backends.py   build_chat_client · classify_error · describe_error ·
                            KEY_PREFIXES/KEY_PATTERNS · TachyonShim         ← EDIT
screening/config.py         LLM_PROVIDERS preset + LLMConfig defaults       ← EDIT
```

The UI reads the provider label from `LLM_PROVIDERS`, the key name from
`cfg.llm.api_key_env`, and the model from `cfg.llm.model`. It has no hard-coded vendor names, so
**no UI file needs editing**.

## Information to collect from the TachyonClient owners first

| # | Question | Used in |
|---|---|---|
| 1 | Import path of the client (e.g. `from tachyon_sdk import TachyonClient`) | `backend_factory` |
| 2 | Constructor arguments and how it authenticates (API key? token? SSO / service account?) | `TachyonShim.__init__`, `api_key_env` |
| 3 | The method that sends chat messages and its argument names (messages, model, temperature, max tokens) | `TachyonShim._call` |
| 4 | Response shape (str / dict / object, and where the text lives) | `extract_text` (already handles the common shapes) |
| 5 | Exception types or HTTP status codes for: bad credentials, rate limit, timeout, model not found, server error | `classify_error` |
| 6 | Model names available, and a sensible fallback order | `LLM_PROVIDERS` |

---

## Step 1: `screening/config.py`

Replace the `LLM_PROVIDERS` dict and the first lines of `LLMConfig` with the following.
Leave everything below `fallback_models` in `LLMConfig` unchanged.

```python
# Active LLM provider preset. Vendor-specific values live ONLY here and in
# screening/llm_backends.py.
LLM_PROVIDERS: dict[str, dict[str, Any]] = {
    "tachyon": {
        "label": "Tachyon",
        "base_url": "",                               # gateway URL if TachyonClient needs one
        "model": "<default-tachyon-model>",           # "" = let TachyonClient pick its default
        "key_env": "TACHYON_API_KEY",                 # "" if TachyonClient authenticates itself
        "fallback_models": [],                        # e.g. ["<smaller-model>"]
        "backend_factory": "tachyon_sdk:TachyonClient",  # "<module>:<ClassOrFactory>"
        "request_extras": {},                         # the NVIDIA "thinking off" switch is not needed
    },
}


@dataclass
class LLMConfig:
    enabled: bool = True
    provider: str = "tachyon"
    api_key_env: str = "TACHYON_API_KEY"   # "" -> no key needed (client handles auth)
    backend_factory: str = "tachyon_sdk:TachyonClient"
    base_url: str = ""
    model: str = "<default-tachyon-model>"
    fallback_models: list[str] = field(default_factory=list)
    request_extras: dict[str, Any] = field(default_factory=dict)  # vendor-specific body fields; {} for Tachyon
    # ... keep temperature, max_tokens, timeout_sec, max_retries, max_concurrency,
    #     max_input_chars, cache_dir, rescore_near_cutoff, cutoff_band, blend_weight,
    #     and apply_provider() exactly as they are.
```

## Step 2: `screening/llm_backends.py`

Replace the whole file with the version below, then fill in the two `ADAPT` blocks in
`TachyonShim` (and `classify_error` if TachyonClient raises its own exception types).

```python
"""LLM backend: the ONLY module (with the LLM_PROVIDERS preset in config.py) that knows
which client the app talks to - the internal TachyonClient.

screening.llm.LLMClient needs a chat-completions style client:
``client.chat.completions.create(model=, messages=, temperature=, max_tokens=)`` returning
``resp.choices[0].message.content``. TachyonShim adapts TachyonClient to that shape.
"""
from __future__ import annotations

import importlib
import re
from types import SimpleNamespace
from typing import Any, Callable

from .config import LLM_PROVIDERS, LLMConfig

# Token formats to mask in any user-facing text (add Tachyon's token prefix if it has one).
KEY_PREFIXES: tuple[str, ...] = ()
KEY_PATTERNS: tuple[re.Pattern[str], ...] = ()

FATAL = "fatal"                          # bad credentials / no permission: stop the batch
MODEL_UNAVAILABLE = "model_unavailable"  # unknown / retired model: try the next fallback model
TRANSIENT = "transient"                  # rate limit / timeout / 5xx: back off and retry
OTHER = "other"                          # bad request etc.: fail this item only
_TRANSIENT_STATUS = frozenset({408, 409, 425, 429})


def provider_label(cfg: LLMConfig) -> str:
    return str(LLM_PROVIDERS.get(cfg.provider, {}).get("label", "LLM provider"))


def _status(exc: BaseException) -> int | None:
    for attr in ("status_code", "status", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    value = getattr(getattr(exc, "response", None), "status_code", None)
    return value if isinstance(value, int) else None


def classify_error(exc: BaseException) -> str:
    # --- ADAPT (optional): map TachyonClient's own exception classes, e.g.
    # from tachyon_sdk.errors import AuthError, RateLimited, ModelNotFound
    # if isinstance(exc, AuthError): return FATAL
    # if isinstance(exc, ModelNotFound): return MODEL_UNAVAILABLE
    # if isinstance(exc, RateLimited): return TRANSIENT
    # -------------------------------------------------------------------------
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return TRANSIENT
    status = _status(exc)
    if status in (401, 403):
        return FATAL
    if status in (404, 410):
        return MODEL_UNAVAILABLE
    if status is not None and (status in _TRANSIENT_STATUS or status >= 500):
        return TRANSIENT
    return OTHER


def describe_error(exc: BaseException, cfg: LLMConfig, *, model: str | None = None,
                   detail: str = "") -> str:
    label, model = provider_label(cfg), model or cfg.model or "default model"
    kind, status = classify_error(exc), _status(exc)
    if kind == FATAL:
        return f"{label} rejected the credentials ({status}). Check {cfg.api_key_env or 'the client auth setup'}."
    if kind == MODEL_UNAVAILABLE:
        return f"Model '{model}' is not available on {label} ({status})."
    if status == 429:
        return f"{label} rate limit reached (429); try again shortly or lower max concurrency."
    if isinstance(exc, TimeoutError):
        return f"{label} did not respond within {cfg.timeout_sec:g} s."
    if isinstance(exc, ConnectionError):
        return f"Could not connect to {label} (network, proxy or firewall?)."
    if status is not None:
        return f"{label} returned HTTP {status}" + (f": {detail}" if detail else ".")
    return f"Unexpected LLM client error ({type(exc).__name__})."


def build_chat_client(cfg: LLMConfig, api_key: str | None) -> Any:
    if cfg.api_key_env and not api_key:
        raise ValueError(f"No API key: set {cfg.api_key_env} in the environment or .streamlit/secrets.toml")
    return TachyonShim(cfg, api_key)


def _load_factory(path: str) -> Callable[..., Any]:
    if ":" not in path:
        raise ValueError("backend_factory must look like 'package.module:ClassName'")
    module_name, attr = path.split(":", 1)
    return getattr(importlib.import_module(module_name), attr)


class TachyonShim:
    """Makes TachyonClient look like a chat-completions client for screening.llm."""

    def __init__(self, cfg: LLMConfig, api_key: str | None) -> None:
        factory = _load_factory(cfg.backend_factory)
        # --- ADAPT: construct TachyonClient exactly as its docs say ----------
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
        # --- ADAPT: ONE chat request with TachyonClient ------------------------
        return self._client.chat(messages=messages, model=model or None,
                                 temperature=temperature, max_tokens=max_tokens)
        # ---------------------------------------------------------------------

    def _create(self, *, model: str, messages: list[dict[str, str]], temperature: float = 0.2,
                max_tokens: int = 700, **_: Any) -> Any:
        raw = self._call(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
        return SimpleNamespace(model=model, usage=None, choices=[
            SimpleNamespace(message=SimpleNamespace(content=extract_text(raw)), finish_reason="stop")])


def extract_text(raw: Any) -> str:
    """Best-effort text extraction from common response shapes."""
    # keep the current extract_text() body unchanged
```

> Keep the current `extract_text()` body when you replace the file. It already handles
> plain strings, dicts with `content`/`text`/`output`/`response`, and `choices[0].message.content`.

**What you get for free:** retries with exponential backoff (and `Retry-After` support when the
exception carries `.response.headers`), automatic fallback to the next model on 404/410, a corrective
re-ask on invalid JSON, PII redaction of every prompt, the disk cache, the thread pool, and progress bars.

## Step 3: secrets

* `.streamlit/secrets.toml`: delete the `NVIDIA_API_KEY = ...` line. If TachyonClient needs a key,
  add `TACHYON_API_KEY = "..."`. If it authenticates itself, set `api_key_env = ""` in Step 1
  and leave the file without a key.
* `.streamlit/secrets.toml.example`: replace its content with:
  ```toml
  # Copy to .streamlit/secrets.toml (gitignored) or set the env var. Never commit real keys.
  TACHYON_API_KEY = "REPLACE_ME"
  ```
* Remove the old key from your environment: `Remove-Item Env:NVIDIA_API_KEY` (PowerShell).
* Revoke the old NVIDIA key at build.nvidia.com.

## Step 4: `requirements.txt`

* Remove the `openai==...` line. The engine imports it only inside `llm_backends.py`, which no longer uses it after Step 2.
* Add the Tachyon package, pinned, from your internal index, for example `tachyon-sdk==X.Y.Z`.

## Step 5: tests

`tests/test_llm.py` simulates the old vendor's exceptions. Make these edits:

| Find | Replace with |
|---|---|
| `import openai` | *(delete)* |
| `API_KEY = "nvapi-TESTKEY1234567890abcdefSECRETwxyz"` | `API_KEY = "test-key-1234567890abcdefSECRETwxyz"` |
| `status_error(openai.<AnyError>, CODE, ...)` | `status_error(HTTPError, CODE, ...)` using the helper below |
| `openai.APIConnectionError(request=None)` | `ConnectionError("down")` |
| `openai.APITimeoutError(request=None)` | `TimeoutError("slow")` |
| test `test_client_uses_openai_with_no_sdk_retries_and_never_shows_key` | delete it, or rewrite it to assert `isinstance(client._client, llm_backends.TachyonShim)` with a fake `backend_factory` |
| `"configured (nvapi-…wxyz)"` (describe_key test) | `"configured (…wxyz)"` |
| `nvapi-from-env`, `nvapi-explicit` | `test-from-env`, `test-explicit` |
| the `nim` variable name | `client` (cosmetic) |
| `FakeOpenAI` | `FakeChatClient` (cosmetic) |

Add this generic error helper to `tests/test_llm.py` in place of the vendor exceptions:

```python
class HTTPError(Exception):
    def __init__(self, message="error", *, response=None, body=None):
        super().__init__(message)
        self.response, self.body = response, body
        self.status_code = getattr(response, "status_code", None)
```

`tests/test_app_smoke.py`: replace `NVIDIA_API_KEY` with `TACHYON_API_KEY` (three places), and
replace `nvapi-abc123XYZ` in the `safe_error` test with a token that matches your new `KEY_PATTERNS`.
If `KEY_PATTERNS` is empty, use `Bearer abc123` and expect `[redacted]`.

## Step 6: docs

* `README.md`, "LLM integration" section: replace the provider and fallback rows with Tachyon's,
  and remove the NVIDIA mention in item 4 near the top.
* `docs/ARCHITECTURE.md`: the module table, `## screening/llm.py (...)` heading, the `openai`
  version in "Global rules", and the "API key resolved as env ..." line in the UI section.
* Once the switch is done you can delete this file, or keep it as history.

## Step 7: clear the old cache

```powershell
Remove-Item -Recurse -Force .cache\llm
```

Cache keys include the model name, so old entries would never be reused anyway. Deleting them
removes text written by the previous vendor's model.

## Step 8: verify

```powershell
# 1. No vendor references left (only this guide and the HackerRank research doc may match)
Select-String -Path .\*.py, .\*.txt, .\*.toml, .\*.md, .\screening\*.py, .\ui\*.py, .\tests\*.py, .\.streamlit\*, .\docs\*.md -Pattern "nvidia|nvapi|openai|gemini|nemotron|integrate\.api" | Where-Object { $_.Path -notmatch "SWITCH_TO_TACHYON|HACKERRANK_DATA_REFERENCE" }

# 2. Tests
python -m pytest

# 3. Live check
streamlit run app.py
#    Config & Rubric Tuning -> "Test LLM connection"  -> expect "LLM connection OK: Connected to <model>"
#    Individual Deep-Dive    -> "Explain this candidate" -> summary, strengths, gaps, questions
#    Shortlist tab           -> "LLM: re-score near cutoff" -> statuses update, rationale column filled
```

The first command should print nothing.
