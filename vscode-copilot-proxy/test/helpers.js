'use strict';

// Test doubles for the VS Code language-model API and a tiny HTTP client (no dependencies).

const http = require('node:http');
const { CopilotProxy, generateToken } = require('../lib/proxy-core');

/** Stand-in for vscode.LanguageModelChatMessage. */
const messageFactory = Object.freeze({
  User: (text) => ({ role: 'user', text }),
  Assistant: (text) => ({ role: 'assistant', text }),
});

/** A LanguageModelChatResponse-like object whose text arrives in chunks. */
function textResponse(chunks) {
  return {
    text: (async function* stream() {
      for (const chunk of chunks) yield chunk;
    }()),
  };
}

/** Error shaped like vscode.LanguageModelError (name + code). */
function lmError(code, message = `${code} error`) {
  const err = new Error(message);
  err.name = 'LanguageModelError';
  err.code = code;
  return err;
}

/**
 * Fake LanguageModelChat. `behavior(messages, options, token, model)` may return a response or
 * throw; by default it streams `reply`.
 */
function fakeModel(overrides = {}) {
  const model = {
    id: 'gpt-test',
    family: 'gpt-test',
    name: 'GPT Test',
    vendor: 'copilot',
    version: '1',
    maxInputTokens: 64000,
    reply: 'Hello from Copilot',
    behavior: null,
    calls: [],
    ...overrides,
  };
  model.sendRequest = async (messages, options, token) => {
    model.calls.push({ messages, options, token });
    if (typeof model.behavior === 'function') return model.behavior(messages, options, token, model);
    return textResponse([model.reply]);
  };
  return model;
}

/** Fake vscode.lm: selectChatModels filters by vendor and records selectors. */
function fakeLm(models) {
  const lm = {
    models,
    selectors: [],
    async selectChatModels(selector) {
      lm.selectors.push(selector);
      return lm.models.filter((m) => !selector || !selector.vendor || m.vendor === selector.vendor);
    },
  };
  return lm;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function waitFor(predicate, timeoutMs = 2000) {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > timeoutMs) throw new Error('waitFor: condition not met in time');
    await new Promise((r) => setTimeout(r, 5));
  }
}

/** Start a proxy on 127.0.0.1:<free port>; closed automatically after the test. */
async function startProxy(t, options = {}) {
  const token = options.token || generateToken();
  const logs = [];
  const lm = options.lm || fakeLm(options.models || [fakeModel()]);
  const proxy = new CopilotProxy({
    lm,
    messageFactory,
    getToken: options.getToken || (() => token),
    log: (line) => logs.push(line),
    version: '9.9.9',
    consentConfirmed: true,
    ...options.proxyOptions,
  });
  const { port } = await proxy.listen(0, '127.0.0.1');
  t.after(() => proxy.close());
  return { proxy, port, token, logs, lm };
}

/** One HTTP request on a fresh connection -> {status, headers, body (parsed JSON or null), text}. */
function request(port, { method = 'GET', path = '/health', token, body, headers = {} } = {}) {
  return new Promise((resolve, reject) => {
    let payload = null;
    if (body !== undefined) payload = typeof body === 'string' || Buffer.isBuffer(body) ? body : JSON.stringify(body);
    const allHeaders = { ...headers };
    if (token !== undefined) allHeaders.Authorization = `Bearer ${token}`;
    if (payload !== null) {
      if (!allHeaders['Content-Type']) allHeaders['Content-Type'] = 'application/json';
      allHeaders['Content-Length'] = Buffer.byteLength(payload);
    }
    const req = http.request({ host: '127.0.0.1', port, method, path, headers: allHeaders, agent: false }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        let json = null;
        try {
          json = JSON.parse(text);
        } catch {
          json = null;
        }
        resolve({ status: res.statusCode, headers: res.headers, body: json, text });
      });
    });
    req.on('error', reject);
    if (payload !== null) req.write(payload);
    req.end();
  });
}

function chat(port, token, body, extra = {}) {
  return request(port, { method: 'POST', path: '/v1/chat/completions', token, body, ...extra });
}

const USER_ONLY = Object.freeze({ messages: [{ role: 'user', content: 'Reply with OK.' }] });

module.exports = {
  USER_ONLY,
  chat,
  deferred,
  fakeLm,
  fakeModel,
  lmError,
  messageFactory,
  request,
  startProxy,
  textResponse,
  waitFor,
};
