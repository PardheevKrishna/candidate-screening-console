# Screening Copilot Proxy (VS Code extension)

Lets the candidate screening console use **your GitHub Copilot chat models** for rationales only:
shortlist rationales, borderline gap analyses, the Deep-Dive "explain this candidate", risk-group
(cluster) summaries and the batch executive summary. The LLM never changes scores, ranks, cutoffs
or the shortlist.

The extension runs a small HTTP server inside VS Code that speaks the OpenAI chat-completions
format and forwards each request to a Copilot chat model through VS Code's Language Model API.
The screening console talks to it on `http://127.0.0.1:5757/v1`.

```
Screening console (Streamlit)  --HTTP + bearer token-->  this extension (VS Code)  -->  GitHub Copilot
         127.0.0.1 only                                   vscode.lm.selectChatModels / sendRequest
```

The language model only writes text. Scores, ranks, cut-offs and the shortlist are computed by the
console's rules and are never changed by the model.

Setup, use in the console and troubleshooting for recruiters and IT:
[docs/COPILOT_PROXY.md](../docs/COPILOT_PROXY.md). This README is the reference for the extension
itself.

## Requirements

- VS Code 1.95 or later.
- The **GitHub Copilot Chat** extension, signed in with an account that has Copilot access
  (Accounts menu, bottom-left).
- No Node.js, npm packages or build step: the extension is plain JavaScript.

## Install

### Option A: from this folder (no packaging)

1. In VS Code open the Command Palette (`Ctrl+Shift+P` / `Cmd+Shift+P`).
2. Run **Developer: Install Extension from Location...** and pick the `vscode-copilot-proxy`
   folder of this repository.
3. Reload the window when asked. The status bar shows **Copilot proxy: on**.

### Option B: as a VSIX file

Packaging needs Node.js and downloads the packaging tool from npm:

```bash
cd vscode-copilot-proxy
npx @vscode/vsce package --allow-missing-repository --skip-license
```

Then run **Extensions: Install from VSIX...** and pick the generated
`screening-copilot-proxy-0.1.0.vsix`. Share the `.vsix` file with colleagues the same way.

## First run: allow access to Copilot models

The first time the screening console sends a request, VS Code asks whether
**Screening Copilot Proxy** may use the language models provided by GitHub Copilot Chat
(the dialog shows the justification "Candidate screening console"). Choose **Allow**.

- Until that first request succeeds the proxy handles one request at a time, so only one dialog
  appears; afterwards it runs up to `screeningProxy.maxConcurrency` requests in parallel.
- If the dialog was dismissed, requests fail with HTTP 403 `consent_required` and VS Code shows
  a notification with an **Allow Access** button. You can also run
  **Screening Proxy: Show Status** and choose **Test Copilot Access**, which sends one tiny
  request ("Reply with the single word OK.") so the dialog appears again.
- To review or revoke the permission later: Accounts menu > **Manage Language Model Access**
  (wording varies slightly between VS Code versions).

## Connecting the screening console

Nothing to configure in the common case. While the proxy runs, the extension writes a connection
file to your home folder, `~/.screening-copilot-proxy.json`:

```json
{
  "base_url": "http://127.0.0.1:5757/v1",
  "token": "<64 hex characters>",
  "pid": 12345,
  "started_at": "2026-01-01T09:00:00.000Z",
  "version": "0.1.0"
}
```

When `COPILOT_PROXY_TOKEN` is not set, the console takes both the token and the address from this
file, so a changed port or a rotated token is picked up without editing anything. It accepts only a
loopback address from the file. The file is removed when the proxy stops or VS Code closes, and it
is readable only by your user account on systems that support file permissions.

To pin the token instead (for example when the console runs under another user account), run
**Screening Proxy: Copy Token** and set it as `COPILOT_PROXY_TOKEN` in the console's environment,
its `.env` file or `.streamlit/secrets.toml`. With a pinned token the console uses the file's
address only when the file holds the same token, and otherwise `http://127.0.0.1:5757/v1`, so keep
`screeningProxy.port` at `5757` when the console cannot read your connection file.

## Commands

| Command | What it does |
|---|---|
| Screening Proxy: Start | Starts the server (normally automatic, see `screeningProxy.autoStart`). |
| Screening Proxy: Stop | Stops the server and removes the connection file. |
| Screening Proxy: Show Status | URL, request counts, available Copilot models and the default one; buttons to start/stop, test Copilot access, copy the token and open the log. |
| Screening Proxy: Copy Token | Copies the bearer token to the clipboard. |
| Screening Proxy: Rotate Token | Creates a new token; the old one stops working immediately and the connection file is updated. |

Clicking the status bar item (**Copilot proxy: on/off**) opens Show Status.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `screeningProxy.port` | `5757` | TCP port. |
| `screeningProxy.host` | `127.0.0.1` | Listen address. **Any other value (for example `0.0.0.0`) exposes the proxy, and your Copilot access, to other machines on the network.** |
| `screeningProxy.autoStart` | `true` | Start when VS Code starts. |
| `screeningProxy.modelFamily` | `""` | Model id or family used when a request does not name an available model (for example `gpt-4.1`). Empty = the first model Copilot offers. |
| `screeningProxy.maxConcurrency` | `4` | Requests sent to Copilot at the same time. Up to 16 more wait in a queue; beyond that the proxy answers 429. |
| `screeningProxy.requestTimeoutSec` | `120` | Time limit per request, including queue time; the request is cancelled and answered 504. |

The settings can only be set in user (or remote) settings, never by a workspace, so a cloned
repository cannot change where the proxy listens. Changing a setting restarts a running proxy.

## HTTP API

Every request needs `Authorization: Bearer <token>`.

| Endpoint | Response |
|---|---|
| `GET /health` | `{"ok": true, "models": <count>, "service": "screening-copilot-proxy", "version": "..."}` |
| `GET /v1/models` | OpenAI-style list: `{"object": "list", "data": [{"id", "family", "name", "vendor": "copilot", "max_input_tokens", ...}]}` |
| `POST /v1/chat/completions` | Body `{"model", "messages", "temperature", "max_tokens"}` (non-streaming). Returns `{"id", "object": "chat.completion", "created", "model", "choices": [{"index": 0, "message": {"role": "assistant", "content"}, "finish_reason": "stop"}], "usage"}` |

`GET /v1/health`, `GET /models` and `POST /chat/completions` are accepted as aliases, for clients
whose base URL includes or omits `/v1`.

Model choice: `model` is matched to a Copilot model id or family; otherwise the configured
`screeningProxy.modelFamily`; otherwise the first available model. The response's `model` field
names the model that answered. Copilot's chat API has no system role, so system messages are
prepended to the first user message under `Instructions:`. `temperature` is passed on (clamped to
0-2; a model that only accepts its default temperature is retried once without it). `max_tokens`
is accepted but the model's own output limit applies. Token usage is reported as zeros.

Errors use the OpenAI shape `{"error": {"message", "type", "code"}}`:

| HTTP | `type` / `code` | Meaning |
|---|---|---|
| 400 | `invalid_request_error` | Malformed JSON or messages, `stream: true`, prompt too long (`context_length_exceeded`). |
| 401 | `unauthorized` / `invalid_token` | Missing or wrong token. |
| 403 | `consent_required` | VS Code has not allowed this extension to use Copilot models (see First run). |
| 403 | `forbidden` / `browser_request`, `forbidden_host` | Browser request (an `Origin` header), or a host name that is not a loopback name. |
| 404 | `model_not_found` | Copilot reports the chosen model as unavailable. |
| 404 | `invalid_request_error` / `unknown_endpoint` | Wrong URL (the base URL should end in `/v1`). |
| 405 | `invalid_request_error` / `method_not_allowed` | Wrong HTTP method for the endpoint (the `Allow` header names the right one). |
| 413 | `invalid_request_error` / `body_too_large` | Request body over 2 MB. |
| 429 | `blocked` | Copilot blocked the request (rate limit, quota or policy). Retry later. |
| 429 | `rate_limited` / `queue_full` | Too many requests waiting in the proxy (`Retry-After: 2`). |
| 500 | `server_error` / `internal_error` | Unexpected error inside the proxy. |
| 502 | `upstream_error` / `copilot_error` | Any other Copilot failure. |
| 503 | `no_models` | No Copilot chat models: sign in to GitHub Copilot in VS Code. |
| 504 | `timeout` | No answer within `screeningProxy.requestTimeoutSec`. |

Quick check from a terminal (PowerShell):

```powershell
$c = Get-Content "$HOME/.screening-copilot-proxy.json" | ConvertFrom-Json
Invoke-RestMethod "$($c.base_url)/models" -Headers @{ Authorization = "Bearer $($c.token)" }
```

bash / zsh (needs `jq`):

```bash
TOKEN=$(jq -r .token ~/.screening-copilot-proxy.json)
curl -s http://127.0.0.1:5757/v1/models -H "Authorization: Bearer $TOKEN"
```

## Security

- **Local only by default.** The server binds `127.0.0.1`. When bound to a loopback address it
  also rejects requests whose `Host` header is not a loopback name, which blocks DNS-rebinding
  tricks from web pages.
- **Token on every request.** A random 32-byte token (64 hex characters) is created on first
  start and kept in VS Code's extension storage on this machine (it is not synced). Tokens are
  compared in constant time. **Rotate Token** replaces it at once.
- **No browser access.** Requests carrying an `Origin` header (sent by web pages) are refused and
  no CORS headers are ever sent.
- **No content in logs.** The output channel ("Screening Copilot Proxy") records method, path,
  status, duration, model and error code only, never prompts, replies or the token.
- **Bounded work.** Bodies over 2 MB are refused, concurrency is capped with a small queue, and
  every request has a time limit after which the Copilot request is cancelled. A client that
  disconnects cancels its Copilot request.
- **What leaves the machine.** Prompts go to GitHub Copilot under your Copilot account and your
  organisation's Copilot policies. The screening console redacts personal details (names,
  emails, phone numbers, links, IP addresses) and refers to candidates by ID before sending.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Console: "Could not reach the Copilot proxy" | VS Code is closed or the proxy is stopped. Open VS Code (status bar **Copilot proxy: on**) or run **Screening Proxy: Start**. |
| Status bar shows **Copilot proxy: off** after start | Run **Screening Proxy: Show Status** > **Open Log**. A message names the problem, for example a port used by another program: change `screeningProxy.port` (the console follows through the connection file; reload its page). |
| 401 `invalid_token` | A pinned `COPILOT_PROXY_TOKEN` is outdated (for example after **Rotate Token**). Remove it (environment, `.env`, `.streamlit/secrets.toml`) to use the connection file, or copy the token again; then restart the console. |
| 403 `consent_required` | Allow access (see First run). |
| 503 `no_models` | Sign in to GitHub Copilot in VS Code and make sure GitHub Copilot Chat is installed and enabled. Right after VS Code starts, Copilot may need a few seconds to load its models. |
| 404 `model_not_found` | Copilot reports the chosen model as unavailable for your account. Choose **(proxy default)** as the model in the console's LLM settings, or pick another model listed by **Show Status**. (A model id Copilot does not offer at all is not an error: the proxy then answers with its default model.) |
| 429 `blocked` | Copilot rate limit, quota or policy. Wait, or lower **Max concurrency** in the console's LLM settings. |
| 504 `timeout` | The model was slow or the queue was long. Raise `screeningProxy.requestTimeoutSec` together with the console's **Timeout (s)** (the shorter one applies), or lower concurrency. |
| Several VS Code windows | The first window serves the proxy; others show **Copilot proxy: on** with "served by another VS Code window" and take over within about 30 seconds after the serving window closes. Start/Stop/Rotate Token act in the serving window. |
| Remote windows (WSL, SSH, containers) | The extension prefers to run on your local machine (`extensionKind: ui`), next to a locally running console. If the console runs on the remote side, install the extension on that side too: the console only connects to a proxy on its own host (`127.0.0.1`). |
| Corporate HTTP proxy | The console never routes its calls to `127.0.0.1` through `HTTP_PROXY`/`HTTPS_PROXY`, so no `NO_PROXY` entry is needed. |

## Development

The request path (authentication, model selection, message mapping, error mapping, limits,
connection file) lives in `lib/proxy-core.js` and has no dependency on VS Code; `extension.js`
injects the VS Code APIs. Unit tests use Node's built-in test runner with fake language-model
objects:

```bash
cd vscode-copilot-proxy
node --test "test/*.test.js"      # or: npm test
```
