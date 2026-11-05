# Switching the LLM layer to the internal TachyonClient

This guide moves every LLM feature (re-scoring near the cutoff, candidate rationales,
interview and integrity questions, risk-group summaries, the executive summary, Ask the Data
query plans and **Test LLM connection**) from the hosted NVIDIA endpoint to the internal
**TachyonClient**. When you're done, the codebase has **no references to NVIDIA, OpenAI, Gemini
or any other wrapper**.

It takes about 30–45 minutes, mostly steps 2 and 5.

---

## How the LLM layer fits together

```
UI buttons / pipeline.run_llm_rescore / Ask the Data
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

Replace the `LLM_PROVIDERS` dict (with the comment above it) and the fields of `LLMConfig` from
`enabled` down to and including `request_extras` (with their comments) with the following. Keep
`temperature` and everything after it, including `apply_provider()`, exactly as it is.

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
        "request_extras": {},                         # vendor-specific request fields: none needed
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
`TachyonShim` (and `classify_error` if TachyonClient raises its own exception types). The error
messages use the same wording as the backend they replace, so the tests and the "Connection
errors" table in `docs/USER_GUIDE.md` stay valid.

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
    """Friendly, key-free message (wording as in "Connection errors" in docs/USER_GUIDE.md)."""
    label, model = provider_label(cfg), model or cfg.model or "default model"
    where = cfg.base_url or label
    kind, status = classify_error(exc), _status(exc)
    code = f" ({status})" if status else ""
    if kind == FATAL and not cfg.api_key_env:
        return (f"{label} rejected the client's credentials{code}. "
                "Check the TachyonClient authentication setup.")
    if kind == FATAL and status == 403:
        return f"The API key is not allowed to use model '{model}' (403)."
    if kind == FATAL:
        return f"{label} rejected the API key{code}. Check {cfg.api_key_env}."
    if kind == MODEL_UNAVAILABLE:
        return f"Model '{model}' is not available at {where} (HTTP {status or 404}; retired or no access)."
    if status == 429:
        return f"{label} rate limit reached (429); try again shortly or lower max concurrency."
    if isinstance(exc, TimeoutError):
        return f"{label} did not respond within {cfg.timeout_sec:g} s."
    if isinstance(exc, ConnectionError):
        return f"Could not connect to {where} (network, proxy or firewall?)."
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
* Remove the old key from your environment: `Remove-Item Env:NVIDIA_API_KEY` (current PowerShell
  session). Also remove it wherever it is set permanently: user or machine environment variables
  (`[Environment]::SetEnvironmentVariable('NVIDIA_API_KEY', $null, 'Machine')`, or `'User'`), the
  NSSM service's `AppEnvironmentExtra`, or `/etc/hiring-screening.env` on Linux (`docs/DEPLOYMENT.md`,
  section 7). Set the new key the same way, then restart the app.
* Revoke the old NVIDIA key at build.nvidia.com.

## Step 4: `requirements.txt`

* Remove the `openai==...` line. The engine imports it only inside `llm_backends.py`, which no
  longer uses it after Step 2, and the tests stop importing it in Step 5.
* Add the Tachyon package, pinned, from your internal index, for example `tachyon-sdk==X.Y.Z`.
* Install it into the app's virtual environment: run `scripts\setup.ps1` / `scripts/setup.sh` again
  (section 4 of `docs/DEPLOYMENT.md`), or `python -m pip install -r requirements.txt`.

## Step 5: tests

List what still names the old vendor:

```powershell
Select-String -Path .\tests\*.py -Pattern "nvidia|nvapi|openai" | ForEach-Object { "{0}:{1}" -f $_.Filename, $_.LineNumber }
```

Six files match. Edit them as follows.

**`tests/test_llm.py`** simulates the old SDK: it imports `openai` to raise its exception classes,
and its test key has the `nvapi-` format.

| Find | Replace with |
|---|---|
| Module docstring: "a fake OpenAI client returns canned replies" | "a fake chat client returns canned replies" |
| `import openai` | *(delete it)*, and add the `HTTPError` helper below next to `status_error()` |
| `API_KEY = "nvapi-TESTKEY1234567890abcdefSECRETwxyz"` | `API_KEY = "test-key-1234567890abcdefSECRETwxyz"` |
| `class FakeOpenAI` and its two uses in `make_client()` | `FakeChatClient` |
| `openai.RateLimitError` (in `rate_limited()`), `openai.AuthenticationError` (in `auth_error()`), `openai.InternalServerError` (503), `openai.BadRequestError` (two tests), `openai.APIStatusError` (410) and `openai.NotFoundError` (two tests), each passed to `status_error(...)` | `HTTPError`: `status_error(HTTPError, CODE, ...)` keeps the status code, headers and body |
| `openai.APIConnectionError(request=None)` (in `test_transient_errors_retry_then_raise` and `test_backoff_is_capped_at_30_seconds`) | `ConnectionError("down")` |
| `openai.APITimeoutError(request=None)` | `TimeoutError("slow")` |
| `nvapi-from-env`, `nvapi-explicit` (in `test_resolve_api_key_prefers_explicit_then_env`) | `test-from-env`, `test-explicit` |
| `"configured (nvapi-…wxyz)"` (in `test_describe_key_shows_at_most_last_four_chars`) | `"configured (…wxyz)"` (`KEY_PREFIXES` is now empty) |
| `test_client_uses_openai_with_no_sdk_retries_and_never_shows_key` | the replacement test below |
| the `nim` and `nim2` variable names | `client` and `client2` (cosmetic) |

```python
class HTTPError(Exception):
    """Client error that carries an HTTP status, headers and body, like most SDK exceptions."""

    def __init__(self, message="error", *, response=None, body=None):
        super().__init__(message)
        self.response, self.body = response, body
        self.status_code = getattr(response, "status_code", None)


def test_client_uses_tachyon_shim_and_never_shows_key(llm_cfg, monkeypatch):
    import sys
    import types

    from screening import llm_backends

    class FakeTachyon:                  # give it the method TachyonShim._call uses
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def chat(self, **kwargs):
            return "OK"

    sdk = types.ModuleType("fake_tachyon_sdk")
    sdk.TachyonClient = FakeTachyon
    monkeypatch.setitem(sys.modules, "fake_tachyon_sdk", sdk)
    llm_cfg.backend_factory = "fake_tachyon_sdk:TachyonClient"
    client = llm.LLMClient(llm_cfg, API_KEY)
    assert isinstance(client._client, llm_backends.TachyonShim)
    assert API_KEY not in repr(client)
    assert client.test_connection()[0]
    with pytest.raises(llm.LLMError):   # a blank key is refused while api_key_env names a key
        llm.LLMClient(llm_cfg, "  ")
```

**`tests/test_app_smoke.py`**

* `test_inputs_fingerprint_is_stable_and_sensitive`: replace the token in
  `state.safe_error(RuntimeError("bad key nvapi-abc123XYZ"))` with one that matches your new
  `KEY_PATTERNS`. If `KEY_PATTERNS` is empty, use `"bad key Bearer abc123"`; the expected result
  stays `"RuntimeError: bad key [redacted]"`.
* `NVIDIA_API_KEY` (three places: the `_isolated_env` fixture, `_app()` and
  `test_llm_actions_with_fake_client`): see the next item.

**`tests/test_app_ask.py`** (five places), **`tests/test_app_compare.py`** (three),
**`tests/test_app_campus_config.py`** (two), **`tests/test_app_history.py`** (two) and
**`tests/test_app_smoke.py`** (three): `"NVIDIA_API_KEY"` in `monkeypatch.delenv(...)`,
`monkeypatch.setenv(...)` and `at.secrets[...] = ""`. Replace the literal with `KEY_ENV`, defined
once per file as `KEY_ENV = AppConfig().llm.api_key_env` (import `AppConfig` from
`screening.config` where the file does not already), so these tests always use the key name set
in `config.py`.

**If TachyonClient needs no key** (`api_key_env = ""`), the app treats the LLM as always configured,
and a few tests rely on a missing key to switch it off:

* `test_app_smoke.py::test_llm_buttons_disabled_without_key`, and in `test_app_ask.py`
  `test_tab_without_llm_is_disabled_with_a_friendly_message` and `test_full_app_ask_tab`: switch
  the LLM off with `cfg.llm.enabled = False` in the injected configuration, and expect "LLM features
  are switched off in Config & Rubric Tuning." where they expect "No LLM API key configured".
* Drop the `monkeypatch.setenv(KEY_ENV, ...)` lines (an empty variable name is invalid) and guard
  `monkeypatch.delenv(KEY_ENV, ...)` and `at.secrets[KEY_ENV]` with `if KEY_ENV:`.
* `test_llm.py`: in `test_resolve_api_key_prefers_explicit_then_env`, pass a name explicitly
  (`monkeypatch.setenv("TEST_LLM_KEY", ...)` and `llm.resolve_api_key(..., env_name="TEST_LLM_KEY")`)
  and drop the final `create_client(LLMConfig()) is None` assertion; drop the `pytest.raises` block
  of the replacement test; and in `test_test_connection_ok_and_auth_failure`,
  `test_auth_error_fails_fast_without_retry` and `test_auth_error_aborts_rest_of_batch` expect
  `"credentials"` instead of `"API key"`.

## Step 6: docs

These are all the places in the documentation that name the NVIDIA provider, its models, its key
or the `openai` SDK (found with the search in Step 8). Rewrite them for Tachyon, or delete them.

`README.md`

* Intro, item 4: "NVIDIA NIM by default; switchable to the internal TachyonClient".
* Quick start (Windows / PowerShell), step 3: the `$env:NVIDIA_API_KEY = "nvapi-..."` comment.
* LLM integration: the "Default provider", "Fallbacks" and "Switch to the internal TachyonClient"
  rows.

`docs/ARCHITECTURE.md`

* Module overview at the top: the `llm_backends.py` line.
* Global rules (all modules): `openai 3.46` in the package list.
* `## screening/llm.py (OpenAI-compatible chat client; NVIDIA endpoint by default)`: the heading,
  the `resolve_api_key` comment (`NVIDIA_API_KEY`) and the bullet on `build_chat_client` /
  `openai.OpenAI(...)`.
* UI (app.py + ui/): "(default `NVIDIA_API_KEY`)".

`docs/DEPLOYMENT.md`

* 5.1 Environment variables: the `NVIDIA_API_KEY` row (delete it) and the `TACHYON_API_KEY` row
  ("Replaces `NVIDIA_API_KEY` after switching ...").
* 5.2 Secrets file: the `# NVIDIA_API_KEY = "<key>"` comments in both code blocks.
* 5.4 LLM provider, or no LLM: the "Hosted provider (default configuration)" bullet, which sets
  `NVIDIA_API_KEY`; the internal wrapper becomes the default.
* 7.4 Linux: systemd: the `# NVIDIA_API_KEY ...` comment in `/etc/hiring-screening.env`.
* No vendor name, but they describe the hosted provider as the default: the diagram in section 1
  ("hosted provider, or the internal wrapper") and the last bullet of section 9 ("With the hosted
  provider (default configuration) ...").

`docs/USER_GUIDE.md`

* Screen layout and sidebar > LLM status badge: the "Key found" row (default model and provider
  "NVIDIA NIM"), the "No key" row ("Set NVIDIA_API_KEY ...") and the "Under the hood" note.
* Config and Rubric Tuning tab > How the Config tab works: row 7, "LLM (NVIDIA NIM)".
* Config and Rubric Tuning tab > LLM settings: the default in the **Model** row.
* Individual Deep-Dive tab > LLM assessment panel: the "Greyed-out button" row's message.
* LLM features > When the LLM runs: the message in the "Two requirements" bullet.
* LLM features > Models and caching: the Provider, Default model, Fallback models and API key rows.
* FAQ and troubleshooting > LLM questions: the row for "No LLM API key configured. …".
* Compare, Campus Distribution, Ask the Data and Run History in detail > Tab: Ask the Data > When
  the tab is unavailable: the "No LLM API key configured. Set NVIDIA_API_KEY ..." message.
* Compare, Campus Distribution, Ask the Data and Run History in detail > Deploying on a company VM
  (summary): the "Configure" bullet.

The quoted messages ("Set NVIDIA_API_KEY ...") are built from `api_key_env`, so after the switch the
app shows the new name by itself; only the docs need the change. If TachyonClient needs no key, the
app never shows them: delete those rows instead. The "Connection errors" table in
`docs/USER_GUIDE.md` needs no change, because Step 2 keeps its messages.

Once the switch is done you can delete this file, or keep it as history.

## Step 7: clear the old cache

```powershell
Remove-Item -Recurse -Force .cache\llm
```

Cache keys include the model name, so old entries would never be reused anyway. Deleting them
removes text written by the previous vendor's model.

## Step 8: verify

```powershell
# Run from the project folder.
# 1. No vendor references left outside this guide. Prints file:line only, so a key still in
#    .streamlit\secrets.toml is never echoed to the screen.
Select-String -Path .\*.py, .\*.txt, .\*.ini, .\*.md, .\.streamlit\*, .\screening\*.py, .\ui\*.py, .\tests\*.py, .\scripts\*, .\docs\*.md -Pattern "nvidia|nvapi|openai|nemotron|integrate\.api|meta/llama|google/gemma|gemini" | Where-Object { $_.Path -notmatch "SWITCH_TO_TACHYON" } | ForEach-Object { "{0}:{1}" -f $_.Path, $_.LineNumber }

# 2. Tests
python -m pytest

# 3. Live check
streamlit run app.py
#    Sidebar                           -> "LLM: configured", caption "Key from environment · model <model>"
#                                         ("Key from secrets ..." or "No API key needed ..." also fine)
#    Config & Rubric Tuning            -> the LLM section is headed "LLM (Tachyon)";
#                                         "Test LLM connection" -> "LLM connection OK: Connected to <model> (x.x s)."
#    Individual Deep-Dive              -> "LLM: explain this candidate" -> summary, strengths, gaps,
#                                         interview questions
#    Candidate Shortlist & Leaderboard -> "LLM: re-score near cutoff" -> toast "LLM re-scored X/Y candidates
#                                         near the cutoff; Z changed status."; the re-scored candidates show
#                                         Experience source "hybrid", and their Individual Deep-Dive shows
#                                         "LLM rationale: ..." under "Rubric points"
#    Ask the Data                      -> an example question returns a table
```

The first command should print nothing.
