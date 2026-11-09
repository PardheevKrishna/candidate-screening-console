'use strict';

/**
 * Core of the Screening Copilot proxy: a small OpenAI-style HTTP server that forwards chat
 * requests to GitHub Copilot chat models through an injected language-model interface.
 *
 * Nothing in this file imports `vscode`. extension.js passes in `vscode.lm`, the chat-message
 * factory and a cancellation factory, so the whole request path runs (and is unit-tested) with
 * plain Node.
 *
 * Endpoints (every request needs "Authorization: Bearer <token>"):
 *   GET  /health               -> {"ok": true, "models": <count>, ...}
 *   GET  /v1/models            -> OpenAI-style model list
 *   POST /v1/chat/completions  -> OpenAI-style chat completion (non-streaming)
 *
 * Logging: one line per request with method, path, status, duration and model. Message
 * content, responses and tokens are never logged.
 */

const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const SERVICE_NAME = 'screening-copilot-proxy';
const JUSTIFICATION = 'Candidate screening console';
const COPILOT_VENDOR = 'copilot';
const MAX_BODY_BYTES = 2 * 1024 * 1024;
const CONNECTION_FILE_NAME = '.screening-copilot-proxy.json';
const DEFAULT_PORT = 5757;
const DEFAULT_HOST = '127.0.0.1';
const DEFAULT_MAX_CONCURRENCY = 4;
const DEFAULT_QUEUE_LIMIT = 16;
const DEFAULT_TIMEOUT_SEC = 120;
const MAX_ERROR_DETAIL_CHARS = 300;

const JSON_HEADERS = Object.freeze({
  'Content-Type': 'application/json; charset=utf-8',
  'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff',
});

// path -> { METHOD: handler kind }. The /v1-less aliases help clients whose base URL omits /v1.
const ROUTES = Object.freeze({
  '/health': { GET: 'health' },
  '/v1/health': { GET: 'health' },
  '/v1/models': { GET: 'models' },
  '/models': { GET: 'models' },
  '/v1/chat/completions': { POST: 'chat' },
  '/chat/completions': { POST: 'chat' },
});

// ---------------------------------------------------------------------------------------------
// Errors (OpenAI-style JSON: {"error": {"message", "type", "code"}})
// ---------------------------------------------------------------------------------------------
class ProxyHttpError extends Error {
  constructor(status, type, code, message, headers = {}) {
    super(message);
    this.name = 'ProxyHttpError';
    this.status = status;
    this.type = type;
    this.code = code;
    this.headers = headers;
  }

  toJSON() {
    return { error: { message: this.message, type: this.type, code: this.code } };
  }
}

/** Raised internally when a request is abandoned (timeout or client disconnect). */
class AbortedError extends Error {
  constructor() {
    super('The request was aborted.');
    this.name = 'AbortError';
  }
}

const Errors = Object.freeze({
  unauthorized: () => new ProxyHttpError(401, 'unauthorized', 'invalid_token',
    'Missing or invalid proxy token. Send "Authorization: Bearer <token>"; in VS Code run '
    + '"Screening Proxy: Copy Token" to get it.'),
  forbiddenHost: () => new ProxyHttpError(403, 'forbidden', 'forbidden_host',
    'Address the proxy by a loopback host name (127.0.0.1, localhost or ::1).'),
  browserRequest: () => new ProxyHttpError(403, 'forbidden', 'browser_request',
    'Browser requests are not accepted by the proxy.'),
  unknownEndpoint: () => new ProxyHttpError(404, 'invalid_request_error', 'unknown_endpoint',
    'Unknown endpoint. Use GET /health, GET /v1/models or POST /v1/chat/completions.'),
  methodNotAllowed: (allow) => new ProxyHttpError(405, 'invalid_request_error', 'method_not_allowed',
    `Method not allowed here; use ${allow}.`, { Allow: allow }),
  badRequest: (code, message) => new ProxyHttpError(400, 'invalid_request_error', code, message),
  tooLarge: () => new ProxyHttpError(413, 'invalid_request_error', 'body_too_large',
    `The request body is larger than ${MAX_BODY_BYTES / (1024 * 1024)} MB.`, { Connection: 'close' }),
  noModels: () => new ProxyHttpError(503, 'no_models', 'no_models',
    'No GitHub Copilot chat models are available. Sign in to GitHub Copilot in VS Code (Accounts '
    + 'menu) and make sure GitHub Copilot Chat is installed and enabled.'),
  consent: () => new ProxyHttpError(403, 'consent_required', 'consent_required',
    'VS Code has not allowed "Screening Copilot Proxy" to use GitHub Copilot language models. '
    + 'In VS Code choose Allow when asked (or run "Screening Proxy: Show Status" and pick '
    + '"Test Copilot Access"), then retry.'),
  blocked: () => new ProxyHttpError(429, 'blocked', 'blocked',
    'GitHub Copilot blocked this request (rate limit, quota or policy). Wait a moment and try again.'),
  modelNotFound: (id) => new ProxyHttpError(404, 'model_not_found', 'model_not_found',
    `The Copilot model ${id ? `"${oneLine(id, 80)}" ` : ''}is not available.`),
  queueFull: () => new ProxyHttpError(429, 'rate_limited', 'queue_full',
    'The proxy is busy: too many requests are waiting. Retry shortly.', { 'Retry-After': '2' }),
  timeout: (seconds) => new ProxyHttpError(504, 'timeout', 'timeout',
    `GitHub Copilot did not answer within ${formatSeconds(seconds)} s.`),
  contextTooLong: () => new ProxyHttpError(400, 'invalid_request_error', 'context_length_exceeded',
    'The prompt is too long for this Copilot model.'),
  upstream: (detail) => new ProxyHttpError(502, 'upstream_error', 'copilot_error',
    detail ? `GitHub Copilot request failed: ${detail}` : 'GitHub Copilot request failed.'),
  internal: () => new ProxyHttpError(500, 'server_error', 'internal_error', 'Unexpected proxy error.'),
});

// ---------------------------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------------------------
function oneLine(value, limit = MAX_ERROR_DETAIL_CHARS) {
  // eslint-disable-next-line no-control-regex
  const text = String(value ?? '').replace(/[\u0000-\u001f\u007f]+/g, ' ').replace(/\s+/g, ' ').trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function formatSeconds(seconds) {
  const n = Number(seconds);
  if (!Number.isFinite(n)) return String(seconds);
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

function positiveInt(value, fallback) {
  const n = Number(value);
  return Number.isInteger(n) && n > 0 ? n : fallback;
}

function nonNegativeInt(value, fallback) {
  const n = Number(value);
  return Number.isInteger(n) && n >= 0 ? n : fallback;
}

function safeCall(fn, ...args) {
  try {
    return fn(...args);
  } catch {
    return undefined;
  }
}

// ---------------------------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------------------------
/** Random 32-byte token as 64 hex characters. */
function generateToken() {
  return crypto.randomBytes(32).toString('hex');
}

/** Constant-time comparison (both sides hashed first, so lengths never leak). */
function tokensEqual(provided, expected) {
  if (typeof provided !== 'string' || typeof expected !== 'string' || !provided || !expected) {
    return false;
  }
  const a = crypto.createHash('sha256').update(provided, 'utf8').digest();
  const b = crypto.createHash('sha256').update(expected, 'utf8').digest();
  return crypto.timingSafeEqual(a, b);
}

/** Token from an "Authorization: Bearer <token>" header value ("" when absent/malformed). */
function bearerToken(header) {
  if (typeof header !== 'string') return '';
  const match = /^\s*Bearer\s+(\S+)\s*$/i.exec(header);
  return match ? match[1] : '';
}

/** True for loopback host names/addresses: localhost, 127.0.0.0/8, ::1 (brackets allowed). */
function isLoopbackHost(host) {
  let h = String(host ?? '').trim().toLowerCase();
  if (h.startsWith('[') && h.endsWith(']')) h = h.slice(1, -1);
  if (h === 'localhost') return true;
  if (net.isIPv4(h)) return h.split('.')[0] === '127';
  if (net.isIPv6(h)) {
    if (h.startsWith('::ffff:')) return isLoopbackHost(h.slice(7));
    const groups = h.split(':');
    // ::1 and its long forms (0:0:0:0:0:0:0:1)
    return h === '::1' || (groups.length === 8 && groups.slice(0, 7).every((g) => /^0{1,4}$/.test(g))
      && /^0{0,3}1$/.test(groups[7]));
  }
  return false;
}

/** Host name of a Host header value ("127.0.0.1:5757" -> "127.0.0.1", "[::1]:5757" -> "::1"). */
function hostnameOf(hostHeader) {
  const value = String(hostHeader ?? '').trim().toLowerCase();
  if (!value) return '';
  if (value.startsWith('[')) {
    const end = value.indexOf(']');
    return end > 0 ? value.slice(1, end) : value;
  }
  const first = value.indexOf(':');
  if (first === -1) return value;
  return first === value.lastIndexOf(':') ? value.slice(0, first) : value; // bare IPv6 stays as-is
}

// ---------------------------------------------------------------------------------------------
// Models and messages
// ---------------------------------------------------------------------------------------------
/** The Copilot chat models the language-model API exposes right now. */
async function listCopilotModels(lm) {
  const models = await lm.selectChatModels({ vendor: COPILOT_VENDOR });
  return Array.isArray(models) ? models.filter(Boolean) : [];
}

/**
 * Model for a request: body.model matched to an id or family (exact, then case-insensitive),
 * else the configured family, else the first available model. Throws 503 when there are none.
 */
function pickModel(models, requested, configuredFamily) {
  const list = Array.isArray(models) ? models.filter(Boolean) : [];
  if (list.length === 0) throw Errors.noModels();
  const find = (wanted) => {
    const key = String(wanted ?? '').trim();
    if (!key) return undefined;
    const lower = key.toLowerCase();
    return list.find((m) => m.id === key)
      || list.find((m) => m.family === key)
      || list.find((m) => String(m.id ?? '').toLowerCase() === lower)
      || list.find((m) => String(m.family ?? '').toLowerCase() === lower);
  };
  return find(requested) || find(configuredFamily) || list[0];
}

/** OpenAI-style model entry. */
function describeModel(model) {
  return {
    id: String(model.id ?? ''),
    object: 'model',
    created: 0,
    owned_by: COPILOT_VENDOR,
    vendor: COPILOT_VENDOR,
    family: String(model.family ?? ''),
    name: String(model.name ?? model.id ?? ''),
    version: String(model.version ?? ''),
    max_input_tokens: Number.isFinite(model.maxInputTokens) ? model.maxInputTokens : null,
  };
}

function contentText(content) {
  if (typeof content === 'string') return content;
  if (content === null || content === undefined) return '';
  if (Array.isArray(content)) {
    return content.map((part) => {
      if (typeof part === 'string') return part;
      if (part && typeof part === 'object' && (part.type === undefined || part.type === 'text')
          && typeof part.text === 'string') {
        return part.text;
      }
      throw Errors.badRequest('unsupported_content', 'Only text message content is supported.');
    }).join('');
  }
  throw Errors.badRequest('invalid_messages', 'Message content must be a string or a list of text parts.');
}

/**
 * OpenAI messages -> language-model chat messages. The chat API has no system role, so system
 * (and "developer") content is prepended to the first user message under "Instructions:".
 */
function mapMessages(messages, factory) {
  if (!Array.isArray(messages) || messages.length === 0) {
    throw Errors.badRequest('invalid_messages', '"messages" must be a non-empty array.');
  }
  const instructions = [];
  const turns = [];
  for (const message of messages) {
    if (!message || typeof message !== 'object' || Array.isArray(message)) {
      throw Errors.badRequest('invalid_messages', 'Each message must be an object with "role" and "content".');
    }
    const role = String(message.role ?? '').trim().toLowerCase();
    const text = contentText(message.content);
    if (role === 'system' || role === 'developer') {
      if (text.trim()) instructions.push(text.trim());
    } else if (role === 'user' || role === 'assistant') {
      turns.push({ role, text });
    } else {
      throw Errors.badRequest('unsupported_role',
        `Unsupported message role "${oneLine(role, 20)}"; use system, user or assistant.`);
    }
  }
  if (instructions.length > 0) {
    const prefix = `Instructions:\n${instructions.join('\n\n')}`;
    const first = turns.findIndex((t) => t.role === 'user');
    if (first >= 0) {
      turns[first] = { role: 'user', text: `${prefix}\n\n${turns[first].text}` };
    } else {
      turns.unshift({ role: 'user', text: prefix });
    }
  }
  if (!turns.some((t) => t.role === 'user')) {
    throw Errors.badRequest('invalid_messages', 'At least one user message is required.');
  }
  return turns.map((t) => (t.role === 'user' ? factory.User(t.text) : factory.Assistant(t.text)));
}

function readTemperature(value) {
  if (value === undefined || value === null) return undefined;
  const t = typeof value === 'number' ? value : Number.NaN;
  if (!Number.isFinite(t)) {
    throw Errors.badRequest('invalid_temperature', '"temperature" must be a number between 0 and 2.');
  }
  return Math.min(2, Math.max(0, t));
}

/** OpenAI-style chat.completion response. Token usage is not reported by the chat API. */
function completionResponse(modelId, content) {
  return {
    id: `chatcmpl-${crypto.randomBytes(12).toString('hex')}`,
    object: 'chat.completion',
    created: Math.floor(Date.now() / 1000),
    model: modelId,
    choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
}

// ---------------------------------------------------------------------------------------------
// Error mapping for language-model failures
// ---------------------------------------------------------------------------------------------
function languageModelErrorCode(err, isLanguageModelError) {
  if (!err || typeof err !== 'object') return '';
  const injected = typeof isLanguageModelError === 'function' && safeCall(isLanguageModelError, err) === true;
  return injected || err.name === 'LanguageModelError' ? String(err.code ?? '') : '';
}

/** Any failure of a chat request -> ProxyHttpError (status, type, code, message). */
function toHttpError(err, options = {}) {
  const { timedOut = false, timeoutSec = DEFAULT_TIMEOUT_SEC, modelId = '', isLanguageModelError = null } = options;
  if (err instanceof ProxyHttpError) return err;
  if (timedOut) return Errors.timeout(timeoutSec);
  switch (languageModelErrorCode(err, isLanguageModelError)) {
    case 'NoPermissions': return Errors.consent();
    case 'Blocked': return Errors.blocked();
    case 'NotFound': return Errors.modelNotFound(modelId);
    default: break;
  }
  const message = oneLine(err && typeof err === 'object' ? err.message : err);
  // e.g. the access dialog was dismissed: the chat API reports it as a plain error
  if (/consent|not allowed to use|no permission/i.test(message)) return Errors.consent();
  if (/context (?:length|window)|token limit|too many tokens|prompt is too long|maximum context/i.test(message)) {
    return Errors.contextTooLong();
  }
  return Errors.upstream(message);
}

/** Some models only accept their default temperature; such a rejection is retried without it. */
function rejectsTemperature(err, isLanguageModelError) {
  const code = languageModelErrorCode(err, isLanguageModelError);
  if (code === 'NoPermissions' || code === 'Blocked' || code === 'NotFound') return false;
  return /temperature/i.test(String(err && err.message));
}

// ---------------------------------------------------------------------------------------------
// Concurrency limit with a small FIFO queue
// ---------------------------------------------------------------------------------------------
class Limiter {
  constructor(max = 1, queueLimit = DEFAULT_QUEUE_LIMIT) {
    this.max = positiveInt(max, 1);
    this.queueLimit = nonNegativeInt(queueLimit, DEFAULT_QUEUE_LIMIT);
    this.active = 0;
    this.queue = [];
  }

  get waiting() {
    return this.queue.length;
  }

  setMax(max) {
    this.max = positiveInt(max, this.max);
    this._pump();
  }

  /** Resolves with a release() function; rejects with 429 when the queue is full. */
  acquire(signal) {
    return new Promise((resolve, reject) => {
      if (signal && signal.aborted) {
        reject(new AbortedError());
        return;
      }
      if (this.active < this.max && this.queue.length === 0) {
        this.active += 1;
        resolve(this._releaser());
        return;
      }
      if (this.queue.length >= this.queueLimit) {
        reject(Errors.queueFull());
        return;
      }
      const waiter = { resolve, signal, onAbort: null };
      if (signal) {
        waiter.onAbort = () => {
          const index = this.queue.indexOf(waiter);
          if (index >= 0) this.queue.splice(index, 1);
          reject(new AbortedError());
        };
        signal.addEventListener('abort', waiter.onAbort, { once: true });
      }
      this.queue.push(waiter);
    });
  }

  _releaser() {
    let released = false;
    return () => {
      if (released) return;
      released = true;
      this.active -= 1;
      this._pump();
    };
  }

  _pump() {
    while (this.active < this.max && this.queue.length > 0) {
      const waiter = this.queue.shift();
      if (waiter.signal && waiter.onAbort) waiter.signal.removeEventListener('abort', waiter.onAbort);
      this.active += 1;
      waiter.resolve(this._releaser());
    }
  }
}

/** Settles like `promise`, or rejects with AbortedError as soon as `signal` aborts. */
function untilAborted(promise, signal) {
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(new AbortedError());
    if (signal.aborted) {
      onAbort();
    } else {
      signal.addEventListener('abort', onAbort, { once: true });
    }
    Promise.resolve(promise).then(
      (value) => { signal.removeEventListener('abort', onAbort); resolve(value); },
      (error) => { signal.removeEventListener('abort', onAbort); reject(error); },
    );
  });
}

/** Minimal CancellationTokenSource stand-in (used when no factory is injected). */
function createCancellation() {
  const listeners = new Set();
  const token = {
    isCancellationRequested: false,
    onCancellationRequested(listener) {
      listeners.add(listener);
      return { dispose: () => listeners.delete(listener) };
    },
  };
  return {
    token,
    cancel() {
      if (token.isCancellationRequested) return;
      token.isCancellationRequested = true;
      for (const listener of [...listeners]) safeCall(listener);
    },
    dispose() {
      listeners.clear();
    },
  };
}

// ---------------------------------------------------------------------------------------------
// Request bodies
// ---------------------------------------------------------------------------------------------
/** Whole request body as a Buffer; rejects with 413 past `limit` bytes. */
function readBody(req, limit = MAX_BODY_BYTES) {
  return new Promise((resolve, reject) => {
    const declared = Number(req.headers && req.headers['content-length']);
    if (Number.isFinite(declared) && declared > limit) {
      reject(Errors.tooLarge());
      return;
    }
    const chunks = [];
    let size = 0;
    let settled = false;
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      fn(value);
    };
    req.on('data', (chunk) => {
      if (settled) return;
      size += chunk.length;
      if (size > limit) {
        chunks.length = 0;
        finish(reject, Errors.tooLarge());
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => finish(resolve, Buffer.concat(chunks)));
    req.on('error', () => finish(reject, new AbortedError()));
    req.on('close', () => finish(reject, new AbortedError())); // closed before 'end': client went away
  });
}

function parseJsonBody(buffer) {
  const text = buffer.toString('utf8').replace(/^﻿/, '');
  if (!text.trim()) throw Errors.badRequest('invalid_json', 'The request body must be a JSON object.');
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    throw Errors.badRequest('invalid_json', 'The request body is not valid JSON.');
  }
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw Errors.badRequest('invalid_json', 'The request body must be a JSON object.');
  }
  return body;
}

// ---------------------------------------------------------------------------------------------
// Logging (never message content, responses or tokens)
// ---------------------------------------------------------------------------------------------
function routeOf(url) {
  const raw = String(url ?? '/');
  const pathOnly = raw.split(/[?#]/, 1)[0] || '/';
  return pathOnly.length > 1 ? pathOnly.replace(/\/+$/, '') : pathOnly;
}

function formatLogLine(entry, durationMs) {
  const parts = [
    new Date().toISOString(),
    oneLine(entry.method, 10) || '-',
    oneLine(entry.path, 80).replace(/\s/g, '_') || '/',
    String(entry.status || 0),
    `${Math.max(0, Math.round(durationMs))}ms`,
  ];
  if (entry.model) parts.push(`model=${oneLine(entry.model, 80).replace(/\s/g, '_')}`);
  if (entry.code) parts.push(`error=${entry.code}`);
  return parts.join(' ');
}

// ---------------------------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------------------------
class CopilotProxy {
  /**
   * @param {object} options
   * @param {{selectChatModels(selector: object): Promise<any[]>}} options.lm  e.g. vscode.lm
   * @param {{User(text: string): any, Assistant(text: string): any}} options.messageFactory
   *        e.g. vscode.LanguageModelChatMessage
   * @param {() => string} options.getToken  current bearer token (read per request: rotation applies at once)
   * @param {() => {token: any, cancel(): void, dispose(): void}} [options.createCancellation]
   * @param {(err: unknown) => boolean} [options.isLanguageModelError]
   * @param {() => void} [options.onConsentRequired]  called when a request needs language-model consent
   * @param {(line: string) => void} [options.log]
   * @param {string} [options.version]
   * @param {string} [options.modelFamily]  "" = first available model
   * @param {number} [options.maxConcurrency]
   * @param {number} [options.queueLimit]
   * @param {number} [options.requestTimeoutSec]
   * @param {boolean} [options.consentConfirmed]  start at full concurrency
   */
  constructor(options = {}) {
    const { lm, messageFactory, getToken } = options;
    if (!lm || typeof lm.selectChatModels !== 'function') {
      throw new TypeError('options.lm.selectChatModels is required');
    }
    if (!messageFactory || typeof messageFactory.User !== 'function' || typeof messageFactory.Assistant !== 'function') {
      throw new TypeError('options.messageFactory.User/Assistant are required');
    }
    if (typeof getToken !== 'function') throw new TypeError('options.getToken is required');
    this.lm = lm;
    this.messageFactory = messageFactory;
    this.getToken = getToken;
    this.createCancellation = typeof options.createCancellation === 'function' ? options.createCancellation : createCancellation;
    this.isLanguageModelError = typeof options.isLanguageModelError === 'function' ? options.isLanguageModelError : null;
    this.onConsentRequired = typeof options.onConsentRequired === 'function' ? options.onConsentRequired : null;
    this.log = typeof options.log === 'function' ? options.log : () => {};
    this.version = String(options.version || '0.0.0');
    this.modelFamily = String(options.modelFamily || '').trim();
    this.maxConcurrency = positiveInt(options.maxConcurrency, DEFAULT_MAX_CONCURRENCY);
    const timeoutSec = Number(options.requestTimeoutSec);
    this.timeoutSec = Number.isFinite(timeoutSec) && timeoutSec > 0 ? timeoutSec : DEFAULT_TIMEOUT_SEC;
    // Until the first request succeeds (VS Code may show its consent dialog), run one at a time.
    this.consentConfirmed = options.consentConfirmed === true;
    this.limiter = new Limiter(this.consentConfirmed ? this.maxConcurrency : 1,
      nonNegativeInt(options.queueLimit, DEFAULT_QUEUE_LIMIT));
    this.stats = { requests: 0, failures: 0, lastStatus: 0, lastModel: '', lastDurationMs: 0 };
    this.host = null;
    this.port = null;
    this.loopbackOnly = true;
    this.server = http.createServer((req, res) => {
      this._handle(req, res).catch(() => this._send(res, {}, 500, Errors.internal().toJSON()));
    });
    this.server.on('clientError', (_err, socket) => {
      if (socket.writable) socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n');
      else socket.destroy();
    });
  }

  /** Bind host:port (port 0 = any free port) -> {host, port}. Rejects with EADDRINUSE etc. */
  listen(port = DEFAULT_PORT, host = DEFAULT_HOST) {
    return new Promise((resolve, reject) => {
      const onError = (err) => {
        this.server.off('listening', onListening);
        reject(err);
      };
      const onListening = () => {
        this.server.off('error', onError);
        this.host = host;
        this.port = this.server.address().port;
        this.loopbackOnly = isLoopbackHost(host);
        resolve({ host: this.host, port: this.port });
      };
      this.server.once('error', onError);
      this.server.once('listening', onListening);
      this.server.listen(port, host);
    });
  }

  get listening() {
    return this.server.listening;
  }

  get baseUrl() {
    return this.port === null ? '' : baseUrlFor(this.host, this.port);
  }

  /** Stop accepting requests and drop open connections (in-flight requests are cancelled). */
  close() {
    return new Promise((resolve) => {
      if (!this.server.listening) {
        resolve();
        return;
      }
      this.server.close(() => resolve());
      if (typeof this.server.closeAllConnections === 'function') this.server.closeAllConnections();
    });
  }

  /** Called once a chat request succeeded (or access was granted interactively). */
  markConsentGranted() {
    if (this.consentConfirmed) return;
    this.consentConfirmed = true;
    this.limiter.setMax(this.maxConcurrency);
  }

  async _handle(req, res) {
    const started = Date.now();
    const entry = { method: String(req.method || ''), path: routeOf(req.url), status: 0, model: '', code: '' };
    let logged = false;
    const writeLog = () => {
      if (logged) return;
      logged = true;
      if (!res.writableFinished && !entry.status) {
        entry.status = 499;
        entry.code = entry.code || 'client_closed';
      }
      const duration = Date.now() - started;
      this.stats.requests += 1;
      if (entry.status >= 400) this.stats.failures += 1;
      this.stats.lastStatus = entry.status;
      this.stats.lastDurationMs = duration;
      if (entry.model) this.stats.lastModel = entry.model;
      safeCall(this.log, formatLogLine(entry, duration));
    };
    res.on('finish', writeLog);
    res.on('close', writeLog);
    try {
      this._checkAccess(req);
      const handlers = ROUTES[entry.path];
      if (!handlers) throw Errors.unknownEndpoint();
      const kind = handlers[entry.method];
      if (!kind) throw Errors.methodNotAllowed(Object.keys(handlers).join(', '));
      if (kind === 'health') {
        this._send(res, entry, 200, await this._health());
      } else if (kind === 'models') {
        this._send(res, entry, 200, { object: 'list', data: (await listCopilotModels(this.lm)).map(describeModel) });
      } else {
        await this._chat(req, res, entry);
      }
    } catch (err) {
      if (err instanceof AbortedError) { // client disconnected while uploading: nobody to answer
        entry.status = 499;
        entry.code = 'client_closed';
        return;
      }
      const httpError = err instanceof ProxyHttpError ? err : Errors.internal();
      entry.code = httpError.code;
      this._send(res, entry, httpError.status, httpError.toJSON(), httpError.headers);
    }
  }

  _checkAccess(req) {
    // DNS-rebinding guard: a loopback-bound proxy only answers to loopback host names.
    const host = hostnameOf(req.headers.host);
    if (this.loopbackOnly && host && !isLoopbackHost(host)) throw Errors.forbiddenHost();
    // Web pages send an Origin header; the proxy serves local programs only.
    if (req.headers.origin !== undefined) throw Errors.browserRequest();
    const expected = safeCall(this.getToken);
    if (!tokensEqual(bearerToken(req.headers.authorization), typeof expected === 'string' ? expected : '')) {
      throw Errors.unauthorized();
    }
  }

  async _health() {
    let count = 0;
    try {
      count = (await listCopilotModels(this.lm)).length;
    } catch {
      count = 0;
    }
    return { ok: true, models: count, service: SERVICE_NAME, version: this.version };
  }

  async _chat(req, res, entry) {
    const body = parseJsonBody(await readBody(req, MAX_BODY_BYTES));
    if (body.stream === true) {
      throw Errors.badRequest('stream_not_supported', 'Streaming is not supported; omit "stream" or send false.');
    }
    const messages = mapMessages(body.messages, this.messageFactory);
    const temperature = readTemperature(body.temperature);
    const requested = typeof body.model === 'string' ? body.model.trim() : '';

    const abort = new AbortController();
    const cancellation = this.createCancellation();
    let timedOut = false;
    let clientGone = false;
    let release = null;
    const stop = () => {
      abort.abort();
      safeCall(() => cancellation.cancel());
    };
    const timer = setTimeout(() => {
      timedOut = true;
      stop();
    }, Math.max(1, Math.round(this.timeoutSec * 1000)));
    const onClose = () => {
      if (!res.writableFinished) {
        clientGone = true;
        stop();
      }
    };
    res.on('close', onClose);
    try {
      release = await this.limiter.acquire(abort.signal);
      const model = pickModel(await listCopilotModels(this.lm), requested, this.modelFamily);
      entry.model = String(model.id ?? '');
      const content = await untilAborted(this._complete(model, messages, temperature, cancellation.token), abort.signal);
      this.markConsentGranted();
      this._send(res, entry, 200, completionResponse(entry.model, content));
    } catch (err) {
      if (clientGone && !timedOut) {
        entry.status = 499;
        entry.code = 'client_closed';
        return;
      }
      const httpError = toHttpError(err, {
        timedOut, timeoutSec: this.timeoutSec, modelId: entry.model || requested,
        isLanguageModelError: this.isLanguageModelError,
      });
      if (httpError.code === 'consent_required' && this.onConsentRequired) safeCall(this.onConsentRequired);
      throw httpError;
    } finally {
      clearTimeout(timer);
      res.off('close', onClose);
      if (release) release();
      safeCall(() => cancellation.dispose());
    }
  }

  async _complete(model, messages, temperature, token) {
    const base = { justification: JUSTIFICATION };
    const options = temperature === undefined ? base : { ...base, modelOptions: { temperature } };
    let response;
    try {
      response = await model.sendRequest(messages, options, token);
    } catch (err) {
      const retry = options !== base && !(token && token.isCancellationRequested)
        && rejectsTemperature(err, this.isLanguageModelError);
      if (!retry) throw err;
      response = await model.sendRequest(messages, base, token);
    }
    const stream = response && response.text;
    if (!stream || typeof stream[Symbol.asyncIterator] !== 'function') {
      throw new Error('the model returned no text stream');
    }
    let text = '';
    for await (const chunk of stream) text += String(chunk);
    return text;
  }

  _send(res, entry, status, payload, headers = {}) {
    entry.status = status;
    if (res.headersSent || res.writableEnded || res.destroyed) return;
    const body = JSON.stringify(payload);
    res.writeHead(status, { ...JSON_HEADERS, ...headers, 'Content-Length': Buffer.byteLength(body) });
    res.end(body);
  }
}

// ---------------------------------------------------------------------------------------------
// Connection file (~/.screening-copilot-proxy.json): how local apps find the proxy
// ---------------------------------------------------------------------------------------------
function connectionFilePath(homeDir = os.homedir()) {
  return path.join(homeDir, CONNECTION_FILE_NAME);
}

/** Connectable base URL: wildcard binds map to 127.0.0.1, IPv6 literals get brackets. */
function baseUrlFor(host, port) {
  let h = String(host ?? '').trim() || DEFAULT_HOST;
  if (h === '0.0.0.0' || h === '::' || h === '[::]') h = DEFAULT_HOST;
  if (h.includes(':') && !h.startsWith('[')) h = `[${h}]`;
  return `http://${h}:${port}/v1`;
}

function connectionInfo({ host, port, token, version, startedAt, pid = process.pid }) {
  return {
    base_url: baseUrlFor(host, port),
    token,
    pid,
    started_at: startedAt,
    version: String(version || '0.0.0'),
  };
}

/** Atomic write (temp file + rename) with mode 600 where the OS supports it. */
function writeConnectionFile(filePath, info) {
  const data = `${JSON.stringify(info, null, 2)}\n`;
  const tmp = `${filePath}.${process.pid}.${crypto.randomBytes(4).toString('hex')}.tmp`;
  fs.writeFileSync(tmp, data, { encoding: 'utf8', mode: 0o600 });
  try {
    fs.renameSync(tmp, filePath);
  } catch {
    safeCall(() => fs.unlinkSync(tmp));
    fs.writeFileSync(filePath, data, { encoding: 'utf8', mode: 0o600 }); // e.g. target briefly locked
  }
  safeCall(() => fs.chmodSync(filePath, 0o600));
}

function readConnectionFile(filePath) {
  try {
    const data = JSON.parse(fs.readFileSync(filePath, 'utf8').replace(/^﻿/, ''));
    return data && typeof data === 'object' && !Array.isArray(data) ? data : null;
  } catch {
    return null;
  }
}

/**
 * Delete the file only while it still describes this proxy instance ({pid, started_at}): another
 * VS Code window may have written its own since. Returns true when the file was removed.
 */
function removeConnectionFile(filePath, owner) {
  const current = readConnectionFile(filePath);
  if (!current) return false;
  if (owner && (current.pid !== owner.pid || current.started_at !== owner.started_at)) return false;
  try {
    fs.unlinkSync(filePath);
    return true;
  } catch {
    return false;
  }
}

/** GET /health on host:port with the token -> {status, body} (status 0 when unreachable). */
function probeHealth({ host = DEFAULT_HOST, port = DEFAULT_PORT, token = '', timeoutMs = 1500 } = {}) {
  return new Promise((resolve) => {
    let target = String(host ?? '').trim() || DEFAULT_HOST;
    if (target === '0.0.0.0' || target === '::' || target === '[::]') target = DEFAULT_HOST;
    target = target.replace(/^\[|\]$/g, '');
    const req = http.request({
      host: target, port, path: '/health', method: 'GET', timeout: timeoutMs,
      headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
    }, (res) => {
      let raw = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => {
        if (raw.length < 65536) raw += chunk;
      });
      res.on('end', () => {
        let body = null;
        try {
          body = JSON.parse(raw);
        } catch {
          body = null;
        }
        resolve({ status: res.statusCode || 0, body });
      });
    });
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', () => resolve({ status: 0, body: null }));
    req.end();
  });
}

function createProxy(options) {
  return new CopilotProxy(options);
}

module.exports = {
  SERVICE_NAME,
  JUSTIFICATION,
  MAX_BODY_BYTES,
  CONNECTION_FILE_NAME,
  DEFAULT_PORT,
  DEFAULT_HOST,
  DEFAULT_MAX_CONCURRENCY,
  DEFAULT_QUEUE_LIMIT,
  DEFAULT_TIMEOUT_SEC,
  AbortedError,
  CopilotProxy,
  Errors,
  Limiter,
  ProxyHttpError,
  baseUrlFor,
  bearerToken,
  completionResponse,
  connectionFilePath,
  connectionInfo,
  createCancellation,
  createProxy,
  describeModel,
  formatLogLine,
  generateToken,
  hostnameOf,
  isLoopbackHost,
  listCopilotModels,
  mapMessages,
  parseJsonBody,
  pickModel,
  probeHealth,
  readBody,
  readConnectionFile,
  readTemperature,
  removeConnectionFile,
  routeOf,
  toHttpError,
  tokensEqual,
  untilAborted,
  writeConnectionFile,
};
