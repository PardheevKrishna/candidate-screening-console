'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { ProxyHttpError, toHttpError } = require('../lib/proxy-core');
const { USER_ONLY, chat, fakeLm, fakeModel, lmError, startProxy } = require('./helpers');

function assertError(res, status, type, code = type) {
  assert.equal(res.status, status);
  assert.deepEqual(Object.keys(res.body).sort(), ['error']);
  assert.equal(res.body.error.type, type);
  assert.equal(res.body.error.code, code);
  assert.equal(typeof res.body.error.message, 'string');
  assert.ok(res.body.error.message.length > 10);
}

test('no Copilot models -> 503 no_models telling the user to sign in', async (t) => {
  const { port, token } = await startProxy(t, { lm: fakeLm([]) });
  const res = await chat(port, token, USER_ONLY);
  assertError(res, 503, 'no_models');
  assert.match(res.body.error.message, /Sign in to GitHub Copilot in VS Code/);
});

test('LanguageModelError NoPermissions -> 403 consent_required (and the extension is told)', async (t) => {
  let prompts = 0;
  const model = fakeModel({ behavior: () => { throw lmError('NoPermissions'); } });
  const { port, token } = await startProxy(t, {
    models: [model], proxyOptions: { onConsentRequired: () => { prompts += 1; } },
  });
  const res = await chat(port, token, USER_ONLY);
  assertError(res, 403, 'consent_required');
  assert.match(res.body.error.message, /allow/i);
  assert.equal(prompts, 1);
});

test('LanguageModelError Blocked -> 429 blocked; NotFound -> 404 model_not_found', async (t) => {
  let code = 'Blocked';
  const model = fakeModel({ id: 'gpt-gone', behavior: () => { throw lmError(code); } });
  const { port, token } = await startProxy(t, { models: [model] });
  assertError(await chat(port, token, USER_ONLY), 429, 'blocked');
  code = 'NotFound';
  const res = await chat(port, token, USER_ONLY);
  assertError(res, 404, 'model_not_found');
  assert.match(res.body.error.message, /gpt-gone/);
});

test('an injected isLanguageModelError check is honoured', () => {
  class FakeVsCodeError extends Error {
    constructor(code) {
      super('x');
      this.code = code;
    }
  }
  const isLm = (err) => err instanceof FakeVsCodeError;
  assert.equal(toHttpError(new FakeVsCodeError('NoPermissions'), { isLanguageModelError: isLm }).status, 403);
  assert.equal(toHttpError(new FakeVsCodeError('Blocked'), { isLanguageModelError: isLm }).status, 429);
  assert.equal(toHttpError(new FakeVsCodeError('NotFound'), { isLanguageModelError: isLm }).status, 404);
  // without the check the same object is just an upstream failure
  assert.equal(toHttpError(new FakeVsCodeError('Blocked')).status, 502);
});

test('plain errors: consent wording -> 403, context length -> 400, anything else -> 502', () => {
  assert.equal(toHttpError(new Error('User did not consent to login.')).code, 'consent_required');
  assert.equal(toHttpError(new Error('prompt token count exceeds the context window')).code, 'context_length_exceeded');
  const upstream = toHttpError(new Error('Response contained no choices.\nline two'));
  assert.equal(upstream.status, 502);
  assert.equal(upstream.type, 'upstream_error');
  assert.equal(upstream.message, 'GitHub Copilot request failed: Response contained no choices. line two');
  const known = new ProxyHttpError(418, 'teapot', 'teapot', 'short and stout');
  assert.equal(toHttpError(known), known);
  assert.equal(toHttpError(new Error('anything'), { timedOut: true, timeoutSec: 7 }).status, 504);
});

test('a request past requestTimeoutSec is cancelled and answered 504', async (t) => {
  let cancelled = false;
  const model = fakeModel({
    behavior: (_messages, _options, token) => new Promise((_resolve, reject) => {
      token.onCancellationRequested(() => {
        cancelled = true;
        reject(new Error('Canceled'));
      });
    }),
  });
  const { port, token } = await startProxy(t, { models: [model], proxyOptions: { requestTimeoutSec: 0.2 } });
  const started = Date.now();
  const res = await chat(port, token, USER_ONLY);
  assertError(res, 504, 'timeout');
  assert.ok(cancelled, 'the Copilot request was cancelled');
  assert.ok(Date.now() - started < 3000);
});

test('a model that ignores cancellation still times out', async (t) => {
  const model = fakeModel({ behavior: () => new Promise(() => {}) });
  const { port, token } = await startProxy(t, { models: [model], proxyOptions: { requestTimeoutSec: 0.2 } });
  assertError(await chat(port, token, USER_ONLY), 504, 'timeout');
});

test('upstream failures do not leak into other requests', async (t) => {
  let fail = true;
  const model = fakeModel({
    behavior: () => {
      if (fail) throw new Error('transient upstream hiccup');
      return { text: (async function* gen() { yield 'fine'; }()) };
    },
  });
  const { port, token } = await startProxy(t, { models: [model] });
  assertError(await chat(port, token, USER_ONLY), 502, 'upstream_error', 'copilot_error');
  fail = false;
  const ok = await chat(port, token, USER_ONLY);
  assert.equal(ok.status, 200);
  assert.equal(ok.body.choices[0].message.content, 'fine');
});
