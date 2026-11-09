'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { JUSTIFICATION } = require('../lib/proxy-core');
const { USER_ONLY, chat, fakeModel, lmError, request, startProxy, textResponse, waitFor } = require('./helpers');

test('POST /v1/chat/completions returns an OpenAI-style chat.completion', async (t) => {
  const model = fakeModel({ behavior: () => textResponse(['Hel', 'lo']) });
  const { port, token } = await startProxy(t, { models: [model] });
  const res = await chat(port, token, {
    model: 'gpt-test',
    messages: [{ role: 'system', content: 'Answer briefly.' }, { role: 'user', content: 'Say hello.' }],
    temperature: 0.2,
    max_tokens: 64,
  });
  assert.equal(res.status, 200);
  assert.match(res.headers['content-type'], /^application\/json/);
  assert.equal(res.headers['cache-control'], 'no-store');
  assert.match(res.body.id, /^chatcmpl-[0-9a-f]{24}$/);
  assert.equal(res.body.object, 'chat.completion');
  assert.ok(Number.isInteger(res.body.created) && Math.abs(res.body.created - Date.now() / 1000) < 60);
  assert.equal(res.body.model, 'gpt-test');
  assert.deepEqual(res.body.choices, [
    { index: 0, message: { role: 'assistant', content: 'Hello' }, finish_reason: 'stop' },
  ]);
  assert.deepEqual(res.body.usage, { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 });

  assert.equal(model.calls.length, 1);
  const call = model.calls[0];
  assert.deepEqual(call.messages, [{ role: 'user', text: 'Instructions:\nAnswer briefly.\n\nSay hello.' }]);
  assert.deepEqual(call.options, { justification: JUSTIFICATION, modelOptions: { temperature: 0.2 } });
  assert.equal(JUSTIFICATION, 'Candidate screening console');
  assert.equal(call.token.isCancellationRequested, false);
  assert.equal(typeof call.token.onCancellationRequested, 'function');
});

test('the /v1-less path works too, and temperature is optional and clamped', async (t) => {
  const model = fakeModel();
  const { port, token } = await startProxy(t, { models: [model] });
  assert.equal((await request(port, { method: 'POST', path: '/chat/completions', token, body: USER_ONLY })).status, 200);
  assert.deepEqual(model.calls[0].options, { justification: JUSTIFICATION });
  await chat(port, token, { ...USER_ONLY, temperature: 7 });
  assert.deepEqual(model.calls[1].options.modelOptions, { temperature: 2 });
  await chat(port, token, { ...USER_ONLY, temperature: -1 });
  assert.deepEqual(model.calls[2].options.modelOptions, { temperature: 0 });
  const bad = await chat(port, token, { ...USER_ONLY, temperature: 'warm' });
  assert.equal(bad.status, 400);
  assert.equal(bad.body.error.code, 'invalid_temperature');
});

test('a model that rejects the temperature is retried once without it', async (t) => {
  const model = fakeModel({
    behavior: (_messages, options) => {
      if (options.modelOptions) throw new Error('Unsupported value: temperature does not support 0.2');
      return textResponse(['done']);
    },
  });
  const { port, token } = await startProxy(t, { models: [model] });
  const res = await chat(port, token, { ...USER_ONLY, temperature: 0.2 });
  assert.equal(res.status, 200);
  assert.equal(res.body.choices[0].message.content, 'done');
  assert.equal(model.calls.length, 2);
  assert.deepEqual(model.calls[1].options, { justification: JUSTIFICATION });
});

test('other failures are not retried', async (t) => {
  const model = fakeModel({ behavior: () => { throw lmError('Blocked'); } });
  const { port, token } = await startProxy(t, { models: [model] });
  assert.equal((await chat(port, token, { ...USER_ONLY, temperature: 0.2 })).status, 429);
  assert.equal(model.calls.length, 1);
});

test('invalid bodies are rejected with 400 before Copilot is called', async (t) => {
  const model = fakeModel();
  const { port, token } = await startProxy(t, { models: [model] });
  const cases = [
    ['{not json', 'invalid_json'],
    ['', 'invalid_json'],
    ['[1, 2]', 'invalid_json'],
    ['"text"', 'invalid_json'],
    [JSON.stringify({ messages: [] }), 'invalid_messages'],
    [JSON.stringify({ messages: [{ role: 'tool', content: 'x' }] }), 'unsupported_role'],
    [JSON.stringify({ ...USER_ONLY, stream: true }), 'stream_not_supported'],
  ];
  for (const [body, code] of cases) {
    const res = await chat(port, token, body);
    assert.equal(res.status, 400, body);
    assert.equal(res.body.error.type, 'invalid_request_error');
    assert.equal(res.body.error.code, code, body);
  }
  assert.equal(model.calls.length, 0);
  assert.equal((await chat(port, token, { ...USER_ONLY, stream: false })).status, 200);
});

test('unknown paths answer 404 unknown_endpoint and wrong methods 405', async (t) => {
  const { port, token } = await startProxy(t);
  const missing = await request(port, { path: '/v1/completions', token });
  assert.equal(missing.status, 404);
  assert.equal(missing.body.error.code, 'unknown_endpoint');
  const wrong = await request(port, { method: 'GET', path: '/v1/chat/completions', token });
  assert.equal(wrong.status, 405);
  assert.equal(wrong.headers.allow, 'POST');
  const wrongHealth = await request(port, { method: 'POST', path: '/health', token, body: {} });
  assert.equal(wrongHealth.status, 405);
  assert.equal((await request(port, { path: '/health?x=1', token })).status, 200);
});

test('the log has method, path, status, duration and model - never content or the token', async (t) => {
  const secret = 'Confidential resume text 4815162342';
  const model = fakeModel({ id: 'gpt-log', reply: `Answer about ${secret}` });
  const { port, token, logs } = await startProxy(t, { models: [model] });
  await chat(port, token, { messages: [{ role: 'system', content: secret }, { role: 'user', content: secret }] });
  await chat(port, 'bad-token', { messages: [{ role: 'user', content: secret }] });
  await request(port, { path: '/v1/models', token });
  await waitFor(() => logs.length === 3);
  assert.match(logs[0], /^\S+Z POST \/v1\/chat\/completions 200 \d+ms model=gpt-log$/);
  assert.match(logs[1], /^\S+Z POST \/v1\/chat\/completions 401 \d+ms error=invalid_token$/);
  assert.match(logs[2], /^\S+Z GET \/v1\/models 200 \d+ms$/);
  const all = logs.join('\n');
  assert.ok(!all.includes('4815162342') && !all.includes('Confidential'));
  assert.ok(!all.includes(token));
});

test('stats count requests and failures', async (t) => {
  const { proxy, port, token } = await startProxy(t);
  await chat(port, token, USER_ONLY);
  await chat(port, 'nope', USER_ONLY);
  await waitFor(() => proxy.stats.requests === 2);
  assert.equal(proxy.stats.failures, 1);
  assert.equal(proxy.stats.lastStatus, 401);
  assert.equal(proxy.stats.lastModel, 'gpt-test');
});
