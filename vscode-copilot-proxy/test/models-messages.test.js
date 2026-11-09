'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { mapMessages, pickModel } = require('../lib/proxy-core');
const { USER_ONLY, chat, fakeModel, fakeLm, messageFactory, request, startProxy } = require('./helpers');

const GPT = { id: 'gpt-4.1', family: 'gpt-4.1' };
const LARGE = { id: 'reasoner-large-2', family: 'reasoner-large' };
const MINI = { id: 'o4-mini', family: 'o4-mini' };

test('pickModel: body.model by id, then family, then the configured family, then the first model', () => {
  const models = [GPT, LARGE, MINI];
  assert.equal(pickModel(models, 'reasoner-large-2', '').id, 'reasoner-large-2');
  assert.equal(pickModel(models, 'reasoner-large', '').id, 'reasoner-large-2');
  assert.equal(pickModel(models, ' REASONER-LARGE-2 ', '').id, 'reasoner-large-2');
  assert.equal(pickModel(models, 'unknown-model', 'o4-mini').id, 'o4-mini');
  assert.equal(pickModel(models, '', 'reasoner-large').id, 'reasoner-large-2');
  assert.equal(pickModel(models, '', '').id, 'gpt-4.1');
  assert.equal(pickModel(models, 'unknown', 'also-unknown').id, 'gpt-4.1');
  assert.throws(() => pickModel([], 'gpt-4.1', ''), (err) => err.status === 503 && err.type === 'no_models');
});

test('mapMessages prepends system content to the first user message under "Instructions:"', () => {
  const out = mapMessages([
    { role: 'system', content: 'Be brief.' },
    { role: 'user', content: 'First question' },
    { role: 'assistant', content: 'First answer' },
    { role: 'user', content: 'Second question' },
  ], messageFactory);
  assert.deepEqual(out, [
    { role: 'user', text: 'Instructions:\nBe brief.\n\nFirst question' },
    { role: 'assistant', text: 'First answer' },
    { role: 'user', text: 'Second question' },
  ]);
});

test('mapMessages joins several system messages, accepts text parts and developer messages', () => {
  const out = mapMessages([
    { role: 'system', content: 'Rule one.' },
    { role: 'developer', content: 'Rule two.' },
    { role: 'assistant', content: 'Earlier reply' },
    { role: 'user', content: [{ type: 'text', text: 'Part A ' }, { type: 'text', text: 'Part B' }] },
  ], messageFactory);
  assert.deepEqual(out, [
    { role: 'assistant', text: 'Earlier reply' },
    { role: 'user', text: 'Instructions:\nRule one.\n\nRule two.\n\nPart A Part B' },
  ]);
  assert.deepEqual(mapMessages([{ role: 'system', content: 'Only rules.' }], messageFactory),
    [{ role: 'user', text: 'Instructions:\nOnly rules.' }]);
  assert.deepEqual(mapMessages([{ role: 'user', content: 'No system' }], messageFactory),
    [{ role: 'user', text: 'No system' }]);
});

test('mapMessages rejects malformed input with 400', () => {
  const cases = [
    undefined,
    [],
    'hello',
    [{ role: 'tool', content: 'x' }],
    [{ role: 'user', content: [{ type: 'image_url', image_url: { url: 'data:,' } }] }],
    [{ role: 'user', content: { text: 'object content' } }],
    [{ role: 'assistant', content: 'no user turn' }],
    [null],
  ];
  for (const messages of cases) {
    assert.throws(() => mapMessages(messages, messageFactory),
      (err) => err.status === 400 && err.type === 'invalid_request_error', JSON.stringify(messages));
  }
});

test('GET /v1/models returns an OpenAI-style list of Copilot models', async (t) => {
  const models = [
    fakeModel({ id: 'gpt-4.1', family: 'gpt-4.1', name: 'GPT-4.1', maxInputTokens: 111000 }),
    fakeModel({ id: 'reasoner-large-2', family: 'reasoner-large', name: 'Reasoner Large 2', maxInputTokens: 80000 }),
    fakeModel({ id: 'other-vendor-model', vendor: 'someone-else' }),
  ];
  const lm = fakeLm(models);
  const { port, token } = await startProxy(t, { lm });
  const res = await request(port, { path: '/v1/models', token });
  assert.equal(res.status, 200);
  assert.equal(res.body.object, 'list');
  assert.deepEqual(res.body.data.map((m) => m.id), ['gpt-4.1', 'reasoner-large-2']);
  assert.deepEqual(res.body.data[0], {
    id: 'gpt-4.1', object: 'model', created: 0, owned_by: 'copilot', vendor: 'copilot', family: 'gpt-4.1',
    name: 'GPT-4.1', version: '1', max_input_tokens: 111000,
  });
  assert.deepEqual(lm.selectors, [{ vendor: 'copilot' }]);
});

test('GET /health reports the model count (also with no models)', async (t) => {
  const lm = fakeLm([fakeModel(), fakeModel({ id: 'second' })]);
  const { port, token } = await startProxy(t, { lm });
  const res = await request(port, { path: '/health', token });
  assert.equal(res.status, 200);
  assert.equal(res.body.ok, true);
  assert.equal(res.body.models, 2);
  assert.equal(res.body.service, 'screening-copilot-proxy');
  lm.models = [];
  assert.equal((await request(port, { path: '/v1/health', token })).body.models, 0);
});

test('chat requests select the requested model, else the configured family, else the first', async (t) => {
  const first = fakeModel({ id: 'gpt-4.1', family: 'gpt-4.1' });
  const second = fakeModel({ id: 'reasoner-large-2', family: 'reasoner-large' });
  const lm = fakeLm([first, second]);
  const { port, token } = await startProxy(t, { lm, proxyOptions: { modelFamily: 'reasoner-large' } });
  const named = await chat(port, token, { ...USER_ONLY, model: 'gpt-4.1' });
  assert.equal(named.body.model, 'gpt-4.1');
  const byFamily = await chat(port, token, { ...USER_ONLY, model: 'reasoner-large' });
  assert.equal(byFamily.body.model, 'reasoner-large-2');
  const fallback = await chat(port, token, { ...USER_ONLY, model: 'retired-model' });
  assert.equal(fallback.body.model, 'reasoner-large-2');
  const unnamed = await chat(port, token, { ...USER_ONLY, model: '' });
  assert.equal(unnamed.body.model, 'reasoner-large-2');
  assert.equal(first.calls.length, 1);
  assert.equal(second.calls.length, 3);
  assert.ok(lm.selectors.every((s) => s.vendor === 'copilot'));
});
