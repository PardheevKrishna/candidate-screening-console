"""Tests for screening.llm_backends against a fake Copilot proxy (http.server on a free port)."""
from __future__ import annotations

import ast
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

from screening import llm
from screening import llm_backends as backends
from screening.config import LLMConfig

TOKEN = "7c1e" * 16                       # fictional proxy token (64 hex chars)
SCREENING_DIR = Path(backends.__file__).resolve().parent
REAL_DISCOVERY_PATH = backends.discovery_path   # captured before the autouse fixture patches it
MESSAGES = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Say OK."}]


def completion_body(content: str = "OK", model: str = "gpt-test") -> dict[str, Any]:
    return {"id": "chatcmpl-abc", "object": "chat.completion", "created": 1_700_000_000, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def error_body(error_type: str, message: str = "proxy error", code: str = "") -> dict[str, Any]:
    return {"error": {"message": message, "type": error_type, "code": code or error_type}}


Reply = tuple[int, Any, dict[str, str]]   # (status, JSON body | bytes, headers)


class FakeProxy:
    """OpenAI-style fake of the VS Code proxy. Replies come from a queue (FIFO), else `default`."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.replies: list[Reply | Callable[[dict[str, Any]], Reply]] = []
        self.default: Reply = (200, completion_body(), {})
        self.release = threading.Event()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                record = {"method": self.command, "path": self.path, "headers": dict(self.headers),
                          "body": json.loads(raw) if raw else None}
                proxy.requests.append(record)
                reply = proxy.replies.pop(0) if proxy.replies else proxy.default
                status, body, headers = reply(record) if callable(reply) else reply
                payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass                      # the client gave up (timeout tests)

            do_GET = do_POST = _serve

            def log_message(self, *args: Any) -> None:   # keep test output clean
                pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False

            def handle_error(self, request: Any, client_address: Any) -> None:
                pass

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def slow(self, seconds: float) -> Callable[[dict[str, Any]], Reply]:
        def reply(_record: dict[str, Any]) -> Reply:
            self.release.wait(seconds)
            return 200, completion_body("late"), {}
        return reply

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def proxy():
    fake = FakeProxy()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def isolated_proxy(monkeypatch, tmp_path):
    """Never read the developer's real connection file or token variable."""
    monkeypatch.setattr(backends, "discovery_path", lambda: tmp_path / "no-proxy-file.json")
    monkeypatch.delenv("COPILOT_PROXY_TOKEN", raising=False)


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", calls.append)
    return calls


def cfg_for(url: str, tmp_path: Path, **extra: Any) -> LLMConfig:
    return LLMConfig(base_url=url, cache_dir=str(tmp_path / "llm-cache"), **extra)


def closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_connection_file(path: Path, base_url: str, token: str = TOKEN, **extra: Any) -> Path:
    data = {"base_url": base_url, "token": token, "pid": 4242, "started_at": "2026-01-01T09:00:00.000Z",
            "version": "0.1.0", **extra}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Chat completions over HTTP
# ---------------------------------------------------------------------------
def test_chat_success_sends_the_bearer_token_and_an_openai_body(proxy):
    proxy.default = (200, completion_body("Hello", model="gpt-4.1"), {})
    client = backends.ProxyChatClient(proxy.base_url + "/", TOKEN, timeout=5)
    response = client.chat.completions.create(model="gpt-4.1", messages=MESSAGES, temperature=0.2, max_tokens=64)
    assert response.choices[0].message.content == "Hello"
    assert response.choices[0].finish_reason == "stop"
    assert response.model == "gpt-4.1"
    sent = proxy.requests[0]
    assert sent["method"] == "POST" and sent["path"] == "/v1/chat/completions"
    assert sent["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert sent["body"] == {"model": "gpt-4.1", "messages": MESSAGES, "temperature": 0.2, "max_tokens": 64}
    assert TOKEN not in repr(client)


def test_llm_client_round_trip_through_the_proxy(proxy, tmp_path):
    proxy.default = (200, completion_body('```json\n{"summary": "fine"}\n```'), {})
    client = llm.LLMClient(cfg_for(proxy.base_url, tmp_path), TOKEN)
    assert client.chat_json("system", "user", required_keys=("summary",)) == {"summary": "fine"}
    assert proxy.requests[0]["body"]["model"] == ""             # proxy default model
    ok, message = client.test_connection()
    assert ok and "gpt-test" in message


@pytest.mark.parametrize("status, body, kind, phrase", [
    (401, error_body("unauthorized", "Missing or invalid proxy token.", "invalid_token"), backends.FATAL, "token"),
    (403, error_body("consent_required", "not allowed"), backends.FATAL, "Allow"),
    (503, error_body("no_models", "no models"), backends.FATAL, "Sign in to GitHub Copilot"),
    (404, error_body("model_not_found", 'The Copilot model "x" is not available.'), backends.MODEL_UNAVAILABLE,
     "not available"),
    (404, error_body("invalid_request_error", "Unknown endpoint.", "unknown_endpoint"), backends.FATAL, "/v1"),
    (429, error_body("blocked", "blocked"), backends.TRANSIENT, "rate-limited"),
    (429, error_body("rate_limited", "busy", "queue_full"), backends.TRANSIENT, "busy"),
    (504, error_body("timeout", "slow"), backends.TRANSIENT, "did not answer"),
    (502, error_body("upstream_error", "boom", "copilot_error"), backends.TRANSIENT, "HTTP 502"),
    (400, error_body("invalid_request_error", "bad messages", "invalid_messages"), backends.OTHER, "HTTP 400"),
])
def test_http_errors_are_classified_and_described(proxy, tmp_path, status, body, kind, phrase):
    proxy.default = (status, body, {})
    client = backends.ProxyChatClient(proxy.base_url, TOKEN, timeout=5)
    with pytest.raises(backends.ProxyError) as info:
        client.chat.completions.create(model="x", messages=MESSAGES)
    err = info.value
    assert err.status_code == status
    assert err.error_type == body["error"]["type"] and err.code == body["error"]["code"]
    assert err.message == body["error"]["message"] and err.body == body
    assert backends.classify_error(err) == kind
    text = backends.describe_error(err, cfg_for(proxy.base_url, tmp_path), model="x",
                                   detail=llm._error_detail(err))
    assert phrase in text and TOKEN not in text


def test_non_json_error_bodies_and_redirects(proxy):
    proxy.replies = [(500, b"<html>oops</html>", {}), (302, b"", {"Location": "http://example.com/"})]
    client = backends.ProxyChatClient(proxy.base_url, TOKEN, timeout=5)
    with pytest.raises(backends.ProxyError) as info:
        client.chat.completions.create(messages=MESSAGES)
    assert info.value.status_code == 500 and "HTTP 500" in info.value.message
    assert backends.classify_error(info.value) == backends.TRANSIENT
    with pytest.raises(backends.ProxyError) as info:
        client.chat.completions.create(messages=MESSAGES)
    assert info.value.status_code == 302 and backends.classify_error(info.value) == backends.OTHER
    assert len(proxy.requests) == 2                              # the redirect was not followed


def test_429_is_retried_with_retry_after_then_succeeds(proxy, tmp_path, sleeps):
    proxy.replies = [(429, error_body("blocked", "slow down"), {"Retry-After": "0"}),
                     (200, completion_body('{"summary": "after retry"}'), {})]
    client = llm.LLMClient(cfg_for(proxy.base_url, tmp_path, max_retries=2), TOKEN)
    assert client.chat_json("system", "user") == {"summary": "after retry"}
    assert len(proxy.requests) == 2
    assert len(sleeps) == 1 and sleeps[0] <= 0.01               # Retry-After: 0 honoured


def test_timeout_raises_timeout_error_and_is_retried(proxy, tmp_path, sleeps):
    proxy.default = proxy.slow(3.0)
    client = backends.ProxyChatClient(proxy.base_url, TOKEN, timeout=0.3)
    started = time.perf_counter()
    with pytest.raises(TimeoutError) as info:
        client.chat.completions.create(messages=MESSAGES)
    assert time.perf_counter() - started < 2.5
    assert backends.classify_error(info.value) == backends.TRANSIENT
    llm_client = llm.LLMClient(cfg_for(proxy.base_url, tmp_path, timeout_sec=0.3, max_retries=1), TOKEN)
    with pytest.raises(llm.LLMError) as err:
        llm_client.chat_json("system", "user")
    assert not err.value.fatal and "Gave up after 2 attempts" in str(err.value)
    assert len(sleeps) == 1


def test_connection_refused_is_fatal_and_not_retried(tmp_path, sleeps):
    url = f"http://127.0.0.1:{closed_port()}/v1"
    client = backends.ProxyChatClient(url, TOKEN, timeout=10)
    with pytest.raises(backends.ProxyNotRunningError) as info:
        client.chat.completions.create(messages=MESSAGES)
    assert isinstance(info.value, ConnectionError)
    assert backends.classify_error(info.value) == backends.FATAL
    assert "Start VS Code with the Screening Copilot proxy extension" in str(info.value)

    llm_client = llm.LLMClient(cfg_for(url, tmp_path, max_retries=3), TOKEN)
    with pytest.raises(llm.LLMError) as err:
        llm_client.chat_json("system", "user")
    assert err.value.fatal and "Start VS Code" in str(err.value) and url in str(err.value)
    assert sleeps == []


def test_environment_proxies_are_ignored(proxy, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{closed_port()}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    client = backends.ProxyChatClient(proxy.base_url, TOKEN, timeout=5)
    assert client.chat.completions.create(messages=MESSAGES).choices[0].message.content == "OK"


def test_invalid_urls_are_rejected():
    for bad in ("", "127.0.0.1:5757/v1", "ftp://127.0.0.1/v1", "http:///v1"):
        with pytest.raises(ValueError):
            backends.ProxyChatClient(bad, TOKEN)


# ---------------------------------------------------------------------------
# Model list & status
# ---------------------------------------------------------------------------
def test_models_list_and_proxy_status(proxy, tmp_path):
    proxy.default = (200, {"object": "list", "data": [
        {"id": "gpt-4.1", "object": "model", "family": "gpt-4.1", "vendor": "copilot", "max_input_tokens": 111000},
        {"id": "reasoner-large-2", "object": "model", "family": "reasoner-large", "vendor": "copilot"},
    ]}, {})
    client = backends.ProxyChatClient(proxy.base_url, TOKEN, timeout=5)
    listing = client.models.list()
    assert [m.id for m in listing.data] == ["gpt-4.1", "reasoner-large-2"]
    assert listing.data[0].max_input_tokens == 111000
    assert proxy.requests[-1]["method"] == "GET" and proxy.requests[-1]["path"] == "/v1/models"

    status = backends.proxy_status(cfg_for(proxy.base_url, tmp_path), TOKEN)
    assert status == {"reachable": True, "models": ["gpt-4.1", "reasoner-large-2"], "error": "",
                      "base_url": proxy.base_url, "token_source": "configured"}

    proxy.default = (200, {"object": "list", "data": []}, {})
    empty = backends.proxy_status(cfg_for(proxy.base_url, tmp_path), TOKEN)
    assert empty["reachable"] and empty["models"] == [] and "Sign in to GitHub Copilot" in empty["error"]


def test_proxy_status_reports_problems_without_raising(proxy, tmp_path):
    proxy.default = (401, error_body("unauthorized", "bad token", "invalid_token"), {})
    rejected = backends.proxy_status(cfg_for(proxy.base_url, tmp_path), "0" * 64)
    assert rejected["reachable"] and rejected["models"] == [] and "token" in rejected["error"]
    no_token = backends.proxy_status(cfg_for(proxy.base_url, tmp_path), None)
    assert no_token["reachable"] and "no token was found" in no_token["error"] and no_token["token_source"] == ""

    down = backends.proxy_status(cfg_for(f"http://127.0.0.1:{closed_port()}/v1", tmp_path), TOKEN, timeout=5)
    assert down["reachable"] is False and "Start VS Code" in down["error"]

    proxy.default = proxy.slow(3.0)
    slow = backends.proxy_status(cfg_for(proxy.base_url, tmp_path), TOKEN, timeout=0.3)
    assert slow["reachable"] is False and "did not answer" in slow["error"]
    for result in (rejected, no_token, down, slow):
        assert TOKEN not in json.dumps(result) and "0" * 64 not in json.dumps(result)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_discover_proxy_reads_the_connection_file(tmp_path):
    path = write_connection_file(tmp_path / "conn.json", "http://127.0.0.1:6001/v1/")
    found = backends.discover_proxy(path)
    assert found == {"base_url": "http://127.0.0.1:6001/v1", "token": TOKEN, "pid": 4242,
                     "started_at": "2026-01-01T09:00:00.000Z", "version": "0.1.0"}
    assert backends.DISCOVERY_FILE_NAME == ".screening-copilot-proxy.json"


def test_discovery_path_is_in_the_home_folder():
    assert REAL_DISCOVERY_PATH() == Path.home() / ".screening-copilot-proxy.json"


@pytest.mark.parametrize("content", [
    "{not json", "[1, 2]", json.dumps({"base_url": "http://127.0.0.1:6001/v1"}),
    json.dumps({"base_url": "http://127.0.0.1:6001/v1", "token": "short"}),
    json.dumps({"base_url": "http://127.0.0.1:6001/v1", "token": "has spaces in the token value"}),
])
def test_discover_proxy_ignores_unusable_files(tmp_path, content):
    path = tmp_path / "conn.json"
    path.write_text(content, encoding="utf-8")
    assert backends.discover_proxy(path) is None


def test_discover_proxy_missing_or_huge_file(tmp_path):
    assert backends.discover_proxy(tmp_path / "missing.json") is None
    huge = tmp_path / "huge.json"
    huge.write_text(json.dumps({"token": TOKEN, "pad": "x" * 70_000}), encoding="utf-8")
    assert backends.discover_proxy(huge) is None


@pytest.mark.parametrize("url", ["https://attacker.example.net/v1", "http://192.168.1.20:5757/v1",
                                 "http://0.0.0.0:5757/v1", "file:///tmp/x", 42])
def test_discovered_urls_must_point_at_this_machine(tmp_path, url):
    found = backends.discover_proxy(write_connection_file(tmp_path / "conn.json", url))
    assert found is not None and found["base_url"] is None and found["token"] == TOKEN


def test_loopback_url_check():
    for url in ("http://127.0.0.1:5757/v1", "http://localhost:1/v1", "http://[::1]:5757/v1", "https://127.8.9.10/x"):
        assert backends.is_loopback_url(url), url
    for url in ("http://example.com/v1", "http://10.0.0.5/v1", "ftp://127.0.0.1/", "", None, "http://[::1"):
        assert not backends.is_loopback_url(url), url


def test_build_chat_client_uses_the_discovered_token_and_port(monkeypatch, tmp_path):
    path = write_connection_file(tmp_path / "conn.json", "http://127.0.0.1:6100/v1")
    monkeypatch.setattr(backends, "discovery_path", lambda: path)
    cfg = LLMConfig()
    discovered = backends.build_chat_client(cfg, None)
    assert discovered.base_url == "http://127.0.0.1:6100/v1" and discovered._token == TOKEN
    assert backends.resolve_connection(cfg, None) == ("http://127.0.0.1:6100/v1", TOKEN, "discovered")
    same = backends.build_chat_client(cfg, TOKEN)                  # configured token = the file's token
    assert same.base_url == "http://127.0.0.1:6100/v1"
    other = backends.build_chat_client(cfg, "a" * 64)              # a different configured token
    assert other.base_url == cfg.base_url and other._token == "a" * 64

    tampered = write_connection_file(tmp_path / "bad.json", "https://attacker.example.net/v1")
    monkeypatch.setattr(backends, "discovery_path", lambda: tampered)
    assert backends.build_chat_client(cfg, None).base_url == cfg.base_url


def test_build_chat_client_without_any_token_explains_what_to_do():
    with pytest.raises(ValueError) as info:
        backends.build_chat_client(LLMConfig(), None)
    assert "Start VS Code with the Screening Copilot proxy extension" in str(info.value)
    assert "COPILOT_PROXY_TOKEN" in str(info.value)


def test_discovered_token_reaches_the_running_proxy(proxy, monkeypatch, tmp_path):
    path = write_connection_file(tmp_path / "conn.json", proxy.base_url)
    monkeypatch.setattr(backends, "discovery_path", lambda: path)
    proxy.default = (200, completion_body('{"ok": true}'), {})
    client = llm.create_client(LLMConfig(base_url="http://127.0.0.1:1/v1", cache_dir=""))
    assert client is not None
    assert client.chat_json("s", "u") == {"ok": True}             # the file's port wins over cfg.base_url
    assert proxy.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    status = backends.proxy_status(LLMConfig(), None)
    assert status["token_source"] == "discovered" and status["base_url"] == proxy.base_url


# ---------------------------------------------------------------------------
# Token hygiene & dependency policy
# ---------------------------------------------------------------------------
def test_key_patterns_mask_long_hex_tokens():
    github = "ghp_" + "Zz9y" * 9
    text = f"failed with Bearer {TOKEN} and token={TOKEN.upper()} sk-abcdef123456 {github}"
    scrubbed = llm._scrub(text)
    assert TOKEN not in scrubbed and TOKEN.upper() not in scrubbed
    assert "abcdef123456" not in scrubbed and "Zz9y" not in scrubbed
    assert all(p.sub("***", TOKEN) == "***" for p in backends.KEY_PATTERNS[:1])
    assert "deadbeef" in llm._scrub("short hex deadbeef stays")          # short hex is not a token
    assert backends.KEY_PREFIXES == ()


def test_proxy_error_never_carries_the_token(proxy):
    proxy.default = (401, error_body("unauthorized", "Missing or invalid proxy token.", "invalid_token"), {})
    client = backends.ProxyChatClient(proxy.base_url, TOKEN, timeout=5)
    with pytest.raises(backends.ProxyError) as info:
        client.chat.completions.create(messages=MESSAGES)
    assert TOKEN not in repr(info.value) and TOKEN not in str(info.value)
    assert TOKEN not in json.dumps(info.value.body)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_no_openai_import_anywhere_in_screening():
    offenders = [p.name for p in sorted(SCREENING_DIR.glob("*.py"))
                 if any(m == "openai" or m.startswith("openai.") for m in _imported_modules(p))]
    assert offenders == []
    assert not hasattr(backends, "extract_text")
    requirements = (SCREENING_DIR.parent / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "openai" not in requirements
