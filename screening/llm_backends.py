"""LLM transport: the ONLY module (with the LLM_PROVIDERS preset in config.py) that knows how the
app reaches a language model.

Models are GitHub Copilot chat models served by the "Screening Copilot Proxy" VS Code extension
(``vscode-copilot-proxy/``): an OpenAI-style endpoint on this machine (``http://127.0.0.1:5757/v1``)
protected by a bearer token. ``screening.llm.LLMClient`` needs a chat-completions style client,
``client.chat.completions.create(model=, messages=, temperature=, max_tokens=)`` returning
``resp.choices[0].message.content``. This module provides:

* ``ProxyChatClient`` / ``build_chat_client`` - that client, over plain HTTP (``requests``);
* ``discover_proxy`` - reads the connection file the extension writes while VS Code runs
  (``~/.screening-copilot-proxy.json``), so normally no token has to be configured;
* ``proxy_status`` - quick reachability / model check for the UI;
* ``classify_error`` / ``describe_error`` - failures -> retry / fallback / fatal, friendly text;
* ``KEY_PREFIXES`` / ``KEY_PATTERNS`` - token formats to mask in any user-facing text.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import NewConnectionError

from .config import LLM_PROVIDERS, LLMConfig

# ---------------------------------------------------------------------------
# Proxy identity & token hygiene
# ---------------------------------------------------------------------------
DISCOVERY_FILE_NAME = ".screening-copilot-proxy.json"
PROXY_GUIDANCE = ("Start VS Code with the Screening Copilot proxy extension (status bar: "
                  "\"Copilot proxy: on\"), or run \"Screening Proxy: Start\" from the VS Code "
                  "Command Palette.")
NO_MODELS_TEXT = ("VS Code is running the Copilot proxy, but no GitHub Copilot chat models are "
                  "available. Sign in to GitHub Copilot in VS Code (Accounts menu) and make sure "
                  "GitHub Copilot Chat is installed and enabled.")

KEY_PREFIXES: tuple[str, ...] = ()                   # proxy tokens are bare hex: no prefix to show
KEY_PATTERNS: tuple[re.Pattern[str], ...] = (        # scrubbed from any user-facing text
    re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])"),   # proxy tokens (64 hex chars)
    re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"),                          # common "sk-..." secret-key format
    re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{16,}"),      # GitHub tokens
)

# Error classes returned by classify_error()
FATAL = "fatal"                          # proxy not running / bad token / no consent / no models: stop
MODEL_UNAVAILABLE = "model_unavailable"  # 404 model_not_found / 410: try the next fallback model
TRANSIENT = "transient"                  # 429 / timeouts / dropped connection / 5xx: back off and retry
OTHER = "other"                          # bad request etc.: fail this item only
_TRANSIENT_STATUS = frozenset({408, 409, 425, 429})
_FATAL_KINDS = frozenset({"no_models", "consent_required", "unauthorized", "invalid_token",
                          "unknown_endpoint", "forbidden_host", "browser_request"})
_MODEL_KINDS = frozenset({"model_not_found"})

_CONNECT_TIMEOUT_SEC = 5.0     # localhost connects at once; Windows needs ~2 s to report "refused"
_STATUS_TIMEOUT_SEC = 3.0
_DEFAULT_TIMEOUT_SEC = 120.0
_MAX_DISCOVERY_BYTES = 64 * 1024
_MAX_MESSAGE_CHARS = 500
_TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/=-]{16,512}")
_USER_AGENT = "screening-console"


def provider_label(cfg: LLMConfig) -> str:
    return str(LLM_PROVIDERS.get(cfg.provider, {}).get("label", "LLM provider"))


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class ProxyError(Exception):
    """An HTTP error answered by the Copilot proxy (body ``{"error": {"message", "type", "code"}}``).

    ``status_code`` / ``error_type`` / ``code`` drive classify_error; ``body`` (parsed JSON) and
    ``response.headers`` (Retry-After) are read by screening.llm. Never carries the token.
    """

    def __init__(self, message: str, *, status_code: int | None = None, error_type: str = "",
                 code: str = "", headers: Any = None, body: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.body = body if isinstance(body, dict) else None
        self.response = SimpleNamespace(status_code=status_code, headers=headers or {})


class ProxyNotRunningError(ConnectionError):
    """Nothing accepts connections at the proxy URL: VS Code or the extension is not running."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
def _namespace(value: Any) -> Any:
    """JSON -> attribute access (``resp.choices[0].message.content``), lists kept as lists."""
    if isinstance(value, dict):
        return SimpleNamespace(**{str(k): _namespace(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_namespace(v) for v in value]
    return value


def _refused(exc: BaseException) -> bool:
    """True when no TCP connection could be made (nothing listening, wrong port or host)."""
    seen: set[int] = set()
    stack: list[Any] = [exc]
    while stack:
        err = stack.pop()
        if not isinstance(err, BaseException) or id(err) in seen:
            continue
        seen.add(id(err))
        if isinstance(err, (ConnectionRefusedError, NewConnectionError)):
            return True
        stack.extend(arg for arg in getattr(err, "args", ()) if isinstance(arg, BaseException))
        stack.extend([getattr(err, "reason", None), err.__cause__, err.__context__])
    return False


class ProxyChatClient:
    """OpenAI-shaped client for the Screening Copilot proxy.

    ``chat.completions.create(model, messages, temperature, max_tokens)`` and ``models.list()``.
    Failures raise ProxyError (HTTP errors), TimeoutError, ProxyNotRunningError (connection
    refused) or ConnectionError (connection dropped). Retries are left to screening.llm.LLMClient.
    """

    def __init__(self, base_url: str, token: str, timeout: float = _DEFAULT_TIMEOUT_SEC) -> None:
        url = (base_url or "").strip().rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"Invalid Copilot proxy URL {url!r}; expected for example "
                             "http://127.0.0.1:5757/v1")
        self.base_url = url
        self._token = (token or "").strip()
        try:
            seconds = float(timeout)
        except (TypeError, ValueError):
            seconds = _DEFAULT_TIMEOUT_SEC
        self.timeout = seconds if seconds > 0 else _DEFAULT_TIMEOUT_SEC
        self.max_retries = 0                     # retries happen in screening.llm.LLMClient
        self._session = requests.Session()
        # Local traffic must never go through HTTP(S)_PROXY, and .netrc credentials are not ours.
        self._session.trust_env = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create_chat))
        self.models = SimpleNamespace(list=self._list_models)

    def __repr__(self) -> str:                   # never shows the token
        return f"ProxyChatClient(base_url={self.base_url!r})"

    def close(self) -> None:
        self._session.close()

    # ---- endpoints ---------------------------------------------------------
    def _create_chat(self, *, model: str = "", messages: list[dict[str, Any]],
                     temperature: float | None = None, max_tokens: int | None = None,
                     **_ignored: Any) -> Any:
        payload: dict[str, Any] = {"model": model or "", "messages": messages}
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        return _namespace(self._request("POST", "chat/completions", payload=payload))

    def _list_models(self, *, timeout: float | None = None) -> Any:
        return _namespace(self._request("GET", "models", timeout=timeout))

    # ---- transport ---------------------------------------------------------
    def _request(self, method: str, path: str, *, payload: Any = None,
                 timeout: float | None = None) -> dict[str, Any]:
        read_timeout = float(timeout) if timeout and timeout > 0 else self.timeout
        headers = {"Accept": "application/json", "Connection": "close", "User-Agent": _USER_AGENT}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = self._session.request(
                method, f"{self.base_url}/{path}", json=payload, headers=headers,
                timeout=(_CONNECT_TIMEOUT_SEC, read_timeout), allow_redirects=False)
        except requests.Timeout:
            raise TimeoutError(f"The Copilot proxy did not answer within {read_timeout:g} s.") from None
        except requests.ConnectionError as exc:
            if _refused(exc):
                raise ProxyNotRunningError(
                    f"Could not reach the Copilot proxy at {self.base_url}. {PROXY_GUIDANCE}") from None
            raise ConnectionError(
                f"The connection to the Copilot proxy at {self.base_url} was interrupted.") from None
        except requests.RequestException as exc:
            raise ProxyError(f"The request to the Copilot proxy failed ({type(exc).__name__}).",
                             error_type="request_error") from None
        return self._parse(response)

    @staticmethod
    def _parse(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            data = None
        status = response.status_code
        if 200 <= status < 300:
            if not isinstance(data, dict):
                raise ProxyError("The Copilot proxy returned an unreadable response.",
                                 status_code=status, error_type="invalid_response")
            return data
        error = data.get("error") if isinstance(data, dict) else None
        message = error_type = code = ""
        if isinstance(error, dict):
            message = str(error.get("message") or "")
            error_type = str(error.get("type") or "")
            code = str(error.get("code") or "")
        elif isinstance(error, str):
            message = error
        message = " ".join(message.split())[:_MAX_MESSAGE_CHARS] or f"HTTP {status} from the Copilot proxy."
        raise ProxyError(message, status_code=status, error_type=error_type, code=code,
                         headers=response.headers, body=data)


# ---------------------------------------------------------------------------
# Discovery (connection file written by the VS Code extension)
# ---------------------------------------------------------------------------
def discovery_path() -> Path:
    """``~/.screening-copilot-proxy.json`` (the extension writes it while the proxy runs)."""
    return Path.home() / DISCOVERY_FILE_NAME


def is_loopback_url(url: Any) -> bool:
    """True for http(s) URLs whose host is localhost / 127.0.0.0/8 / ::1."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def discover_proxy(path: str | Path | None = None) -> dict[str, Any] | None:
    """Connection details published by the running proxy, or None (missing / unreadable / invalid).

    -> {"base_url": str | None, "token": str, "pid": int | None, "started_at": str, "version": str}.
    ``base_url`` is kept only when it points at this machine (loopback), so a tampered file can
    never send candidate data elsewhere.
    """
    try:
        target = Path(path) if path is not None else discovery_path()
        if target.stat().st_size > _MAX_DISCOVERY_BYTES:
            return None
        raw = target.read_text(encoding="utf-8-sig")
    except (OSError, RuntimeError, ValueError):   # RuntimeError: home folder cannot be determined
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    token = token.strip() if isinstance(token, str) else ""
    if not _TOKEN_RE.fullmatch(token):
        return None
    base_url = data.get("base_url")
    pid = data.get("pid")
    return {
        "base_url": base_url.strip().rstrip("/") if is_loopback_url(base_url) else None,
        "token": token,
        "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        "started_at": str(data.get("started_at") or ""),
        "version": str(data.get("version") or ""),
    }


def _same_token(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def resolve_connection(cfg: LLMConfig, api_key: str | None = None) -> tuple[str, str, str]:
    """(base_url, token, source) for the proxy; source is "configured", "discovered" or "".

    A configured token (env / secrets) wins. Without one, the connection file supplies both the
    token and the URL (so a changed port is followed). The file's URL is used only together with
    the file's own token.
    """
    token = (api_key or "").strip()
    found = discover_proxy()
    if token:
        if found and found["base_url"] and _same_token(found["token"], token):
            return found["base_url"], token, "configured"
        return cfg.base_url, token, "configured"
    if found:
        return found["base_url"] or cfg.base_url, found["token"], "discovered"
    return cfg.base_url, "", ""


def build_chat_client(cfg: LLMConfig, api_key: str | None) -> ProxyChatClient:
    """ProxyChatClient for the configured or discovered proxy; ValueError when no token exists."""
    base_url, token, _ = resolve_connection(cfg, api_key)
    if not token:
        raise ValueError(f"No Copilot proxy token found. {PROXY_GUIDANCE} The extension shares its "
                         f"token automatically; or set {cfg.api_key_env or 'COPILOT_PROXY_TOKEN'}.")
    return ProxyChatClient(base_url, token, timeout=cfg.timeout_sec)


def _scrub(text: str, token: str = "") -> str:
    out = str(text)
    if token:
        out = out.replace(token, "***")
    for pattern in KEY_PATTERNS:
        out = pattern.sub("***", out)
    return out


def proxy_status(cfg: LLMConfig, token: str | None = None, *,
                 timeout: float = _STATUS_TIMEOUT_SEC) -> dict[str, Any]:
    """Quick check for the UI (one GET /v1/models). Never raises and never includes the token.

    -> {"reachable": bool, "models": [model ids], "error": "" or friendly text,
        "base_url": str, "token_source": "configured" | "discovered" | ""}
    """
    base_url, resolved, source = resolve_connection(cfg, token)
    out: dict[str, Any] = {"reachable": False, "models": [], "error": "", "base_url": base_url,
                           "token_source": source}
    try:
        client = ProxyChatClient(base_url, resolved, timeout=timeout)
    except ValueError as exc:
        out["error"] = str(exc)
        return out
    try:
        listing = client.models.list(timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        out["reachable"] = isinstance(getattr(exc, "status_code", None), int)
        if out["reachable"] and not resolved and getattr(exc, "status_code", None) == 401:
            out["error"] = (f"The Copilot proxy is running but no token was found. Set "
                            f"{cfg.api_key_env or 'COPILOT_PROXY_TOKEN'} (\"Screening Proxy: Copy "
                            "Token\" in VS Code) or let the app read the connection file.")
        else:
            out["error"] = _scrub(describe_error(exc, cfg), resolved)
        return out
    finally:
        client.close()
    data = getattr(listing, "data", None)
    ids = [str(getattr(m, "id", "")) for m in data] if isinstance(data, list) else []
    out.update(reachable=True, models=[i for i in ids if i])
    if not out["models"]:
        out["error"] = NO_MODELS_TEXT
    return out


# ---------------------------------------------------------------------------
# Error classification & friendly text
# ---------------------------------------------------------------------------
def _kinds(exc: BaseException) -> set[str]:
    return {str(getattr(exc, attr, "") or "").strip().lower() for attr in ("error_type", "code")} - {""}


def classify_error(exc: BaseException) -> str:
    """Map a client exception to FATAL / MODEL_UNAVAILABLE / TRANSIENT / OTHER."""
    if isinstance(exc, ProxyNotRunningError):
        return FATAL                     # proxy not running: retrying cannot help
    kinds = _kinds(exc)
    if kinds & _FATAL_KINDS:
        return FATAL
    if kinds & _MODEL_KINDS:
        return MODEL_UNAVAILABLE
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return TRANSIENT
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        if status in (401, 403):
            return FATAL
        if status in (404, 410):
            return MODEL_UNAVAILABLE
        if status in _TRANSIENT_STATUS or status >= 500:
            return TRANSIENT
    return OTHER


def describe_error(exc: BaseException, cfg: LLMConfig, *, model: str | None = None,
                   detail: str = "") -> str:
    """Friendly, token-free message for a client exception."""
    label = provider_label(cfg)
    model = cfg.model if model is None else model
    model_txt = f"'{model}'" if model else "the proxy's default Copilot model"
    where = cfg.base_url or label
    status = getattr(exc, "status_code", None)
    kinds = _kinds(exc)
    key_name = cfg.api_key_env or "COPILOT_PROXY_TOKEN"
    suffix = f": {detail}" if detail else "."
    if isinstance(exc, ProxyNotRunningError):
        return f"Could not reach the Copilot proxy at {where}. {PROXY_GUIDANCE}"
    if "no_models" in kinds:
        return NO_MODELS_TEXT
    if "consent_required" in kinds:
        return ("VS Code has not yet allowed the Screening Copilot proxy to use GitHub Copilot models "
                "(403). In VS Code choose Allow on the model-access prompt, or run \"Screening Proxy: "
                "Show Status\" > Test Copilot Access, then try again.")
    if status == 401 or kinds & {"unauthorized", "invalid_token"}:
        return (f"The Copilot proxy rejected the token (401). Unset {key_name} to use the token VS Code "
                "shares automatically, or paste a fresh one from \"Screening Proxy: Copy Token\".")
    if "unknown_endpoint" in kinds:
        return f"{where} is not a Copilot proxy endpoint (404). The LLM base URL should end in /v1."
    if "blocked" in kinds:
        return ("GitHub Copilot blocked or rate-limited the request (429). Wait a moment, or lower "
                "the LLM max concurrency.")
    if "queue_full" in kinds:
        return "The Copilot proxy is busy (429). Lower the LLM max concurrency or retry shortly."
    if classify_error(exc) == MODEL_UNAVAILABLE:
        return (f"Copilot model {model_txt} is not available (HTTP {status or 404}). Pick \"(proxy default)\" "
                "as the model in Config & Rubric Tuning so the proxy chooses, or pick one the proxy lists.")
    if isinstance(exc, TimeoutError) or status == 504 or "timeout" in kinds:
        return f"GitHub Copilot did not answer within {cfg.timeout_sec:g} s (timeout)."
    if status == 403:
        return f"The Copilot proxy refused the request (403){suffix}"
    if isinstance(exc, ConnectionError):
        return f"The connection to the Copilot proxy at {where} was interrupted."
    if isinstance(status, int):
        return f"{label} returned HTTP {status}{suffix}"
    return f"Unexpected LLM client error ({type(exc).__name__})."


__all__ = [
    "DISCOVERY_FILE_NAME", "FATAL", "KEY_PATTERNS", "KEY_PREFIXES", "MODEL_UNAVAILABLE",
    "NO_MODELS_TEXT", "OTHER", "PROXY_GUIDANCE", "ProxyChatClient", "ProxyError",
    "ProxyNotRunningError", "TRANSIENT", "build_chat_client", "classify_error", "describe_error",
    "discover_proxy", "discovery_path", "is_loopback_url", "provider_label", "proxy_status",
    "resolve_connection",
]
