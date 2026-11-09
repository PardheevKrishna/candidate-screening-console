'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { Readable } = require('node:stream');

const { Limiter, MAX_BODY_BYTES, readBody } = require('../lib/proxy-core');
const { USER_ONLY, chat, deferred, fakeModel, startProxy, textResponse, waitFor } = require('./helpers');

function bodyStream(chunks, headers = {}) {
  const stream = Readable.from(chunks);
  stream.headers = headers;
  return stream;
}

test('the body limit is 2 MB', () => {
  assert.equal(MAX_BODY_BYTES, 2 * 1024 * 1024);
});

test('a declared body over 2 MB is answered 413 before it is read', async (t) => {
  const model = fakeModel();
  const { port, token } = await startProxy(t, { models: [model] });
  const res = await new Promise((resolve, reject) => {
    const req = http.request({
      host: '127.0.0.1', port, method: 'POST', path: '/v1/chat/completions', agent: false,
      headers: {
        Authorization: `Bearer ${token}`, 'Content-Type': 'application/json',
        'Content-Length': String(MAX_BODY_BYTES + 1),
      },
    }, (response) => {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => {
        req.destroy();
        resolve({ status: response.statusCode, body: JSON.parse(Buffer.concat(chunks).toString('utf8')) });
      });
    });
    req.on('error', (err) => {
      if (err.code !== 'ECONNRESET' && err.code !== 'EPIPE') reject(err);
    });
    req.flushHeaders(); // headers only; the body is never sent
  });
  assert.equal(res.status, 413);
  assert.equal(res.body.error.code, 'body_too_large');
  assert.equal(model.calls.length, 0);
});

test('readBody stops at the limit for streamed bodies', async () => {
  await assert.rejects(readBody(bodyStream([Buffer.alloc(600), Buffer.alloc(600)]), 1000),
    (err) => err.status === 413 && err.code === 'body_too_large');
  const ok = await readBody(bodyStream([Buffer.from('{"a":'), Buffer.from('1}')]), 1000);
  assert.equal(ok.toString('utf8'), '{"a":1}');
  await assert.rejects(readBody(bodyStream([], { 'content-length': '1001' }), 1000), (err) => err.status === 413);
});

test('a large body under the limit is accepted', async (t) => {
  const model = fakeModel();
  const { port, token } = await startProxy(t, { models: [model] });
  const big = 'x'.repeat(MAX_BODY_BYTES - 1024);
  const res = await chat(port, token, { messages: [{ role: 'user', content: big }] });
  assert.equal(res.status, 200);
  assert.equal(model.calls[0].messages[0].text.length, big.length);
});

test('Limiter caps concurrency, queues FIFO and rejects when the queue is full', async () => {
  const limiter = new Limiter(2, 1);
  const releaseA = await limiter.acquire();
  const releaseB = await limiter.acquire();
  let third = null;
  const pending = limiter.acquire().then((release) => {
    third = release;
    return release;
  });
  await assert.rejects(limiter.acquire(), (err) => err.status === 429 && err.code === 'queue_full'
    && err.headers['Retry-After'] === '2');
  assert.equal(limiter.active, 2);
  assert.equal(limiter.waiting, 1);
  releaseA();
  releaseA(); // a second call is a no-op
  await pending;
  assert.equal(typeof third, 'function');
  assert.equal(limiter.active, 2);
  releaseB();
  third();
  assert.equal(limiter.active, 0);
});

test('Limiter drops waiters whose request was aborted', async () => {
  const limiter = new Limiter(1, 4);
  const release = await limiter.acquire();
  const controller = new AbortController();
  const waiting = limiter.acquire(controller.signal);
  assert.equal(limiter.waiting, 1);
  controller.abort();
  await assert.rejects(waiting, (err) => err.name === 'AbortError');
  assert.equal(limiter.waiting, 0);
  release();
  assert.equal(limiter.active, 0);
  limiter.setMax(3);
  await Promise.all([limiter.acquire(), limiter.acquire(), limiter.acquire()]);
  assert.equal(limiter.active, 3);
});

test('the server runs at most maxConcurrency requests and answers 429 when the queue is full', async (t) => {
  const gate = deferred();
  let inFlight = 0;
  let peak = 0;
  const model = fakeModel({
    behavior: async () => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await gate.promise;
      inFlight -= 1;
      return textResponse(['ok']);
    },
  });
  const { proxy, port, token } = await startProxy(t, {
    models: [model], proxyOptions: { maxConcurrency: 2, queueLimit: 1, consentConfirmed: true },
  });
  const pending = [chat(port, token, USER_ONLY), chat(port, token, USER_ONLY), chat(port, token, USER_ONLY)];
  await waitFor(() => model.calls.length === 2 && proxy.limiter.waiting === 1);
  const rejected = await chat(port, token, USER_ONLY);
  assert.equal(rejected.status, 429);
  assert.equal(rejected.body.error.code, 'queue_full');
  assert.equal(rejected.headers['retry-after'], '2');
  gate.resolve();
  const results = await Promise.all(pending);
  assert.deepEqual(results.map((r) => r.status), [200, 200, 200]);
  assert.equal(peak, 2);
  assert.equal(model.calls.length, 3);
});

test('requests run one at a time until the first one succeeds (consent dialog)', async (t) => {
  const gates = [deferred(), deferred(), deferred(), deferred(), deferred()];
  let inFlight = 0;
  let peak = 0;
  const model = fakeModel({
    behavior: async (_messages, _options, _token, self) => {
      const gate = gates[self.calls.length - 1];
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await gate.promise;
      inFlight -= 1;
      return textResponse(['ok']);
    },
  });
  const { proxy, port, token } = await startProxy(t, {
    models: [model], proxyOptions: { maxConcurrency: 3, consentConfirmed: false },
  });
  const first = [chat(port, token, USER_ONLY), chat(port, token, USER_ONLY)];
  await waitFor(() => model.calls.length === 1 && proxy.limiter.waiting === 1);
  assert.equal(proxy.consentConfirmed, false);
  gates[0].resolve();
  await waitFor(() => model.calls.length === 2);
  assert.equal(proxy.consentConfirmed, true);
  const more = [chat(port, token, USER_ONLY), chat(port, token, USER_ONLY)];
  await waitFor(() => model.calls.length === 4);
  assert.equal(inFlight, 3);
  for (const gate of gates) gate.resolve();
  const statuses = (await Promise.all([...first, ...more])).map((r) => r.status);
  assert.deepEqual(statuses, [200, 200, 200, 200]);
  assert.equal(peak, 3);
});

test('a client that disconnects cancels its Copilot request and frees the slot', async (t) => {
  let cancelled = false;
  const model = fakeModel({
    behavior: (_messages, _options, token) => new Promise((_resolve, reject) => {
      token.onCancellationRequested(() => {
        cancelled = true;
        reject(new Error('Canceled'));
      });
    }),
  });
  const { proxy, port, token } = await startProxy(t, { models: [model], proxyOptions: { maxConcurrency: 1 } });
  const req = http.request({
    host: '127.0.0.1', port, method: 'POST', path: '/v1/chat/completions', agent: false,
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
  });
  req.on('error', () => {});
  req.end(JSON.stringify(USER_ONLY));
  await waitFor(() => model.calls.length === 1);
  req.destroy();
  await waitFor(() => cancelled && proxy.limiter.active === 0);
});
