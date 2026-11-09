'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { bearerToken, generateToken, hostnameOf, isLoopbackHost, tokensEqual } = require('../lib/proxy-core');
const { USER_ONLY, chat, fakeModel, fakeLm, request, startProxy } = require('./helpers');

test('generateToken returns 32 random bytes as 64 hex characters', () => {
  const a = generateToken();
  const b = generateToken();
  assert.match(a, /^[0-9a-f]{64}$/);
  assert.match(b, /^[0-9a-f]{64}$/);
  assert.notEqual(a, b);
});

test('tokensEqual is exact and never accepts empty values', () => {
  const token = generateToken();
  assert.equal(tokensEqual(token, token), true);
  assert.equal(tokensEqual(token, `${token}x`), false);
  assert.equal(tokensEqual(token.toUpperCase(), token), false);
  assert.equal(tokensEqual('', ''), false);
  assert.equal(tokensEqual(undefined, token), false);
  assert.equal(tokensEqual(token, undefined), false);
});

test('bearerToken reads "Authorization: Bearer <token>"', () => {
  assert.equal(bearerToken('Bearer abc123'), 'abc123');
  assert.equal(bearerToken('  bearer   abc123  '), 'abc123');
  assert.equal(bearerToken('Basic abc123'), '');
  assert.equal(bearerToken('Bearer'), '');
  assert.equal(bearerToken('Bearer a b'), '');
  assert.equal(bearerToken(undefined), '');
});

test('every endpoint answers 401 without the right token and never reaches Copilot', async (t) => {
  const model = fakeModel();
  const lm = fakeLm([model]);
  const { port, token } = await startProxy(t, { lm });
  const routes = [['GET', '/health'], ['GET', '/v1/models'], ['POST', '/v1/chat/completions'], ['GET', '/unknown']];
  for (const [method, path] of routes) {
    for (const bad of [undefined, 'wrong-token', token.slice(0, -1), token.toUpperCase(), `${token}0`]) {
      const res = await request(port, { method, path, token: bad, body: method === 'POST' ? USER_ONLY : undefined });
      assert.equal(res.status, 401, `${method} ${path}`);
      assert.deepEqual(Object.keys(res.body.error).sort(), ['code', 'message', 'type']);
      assert.equal(res.body.error.type, 'unauthorized');
      assert.ok(!res.text.includes(token), 'the token is never echoed');
    }
  }
  assert.equal(lm.selectors.length, 0);
  assert.equal(model.calls.length, 0);
  assert.equal((await request(port, { path: '/health', token })).status, 200);
});

test('a rotated token applies to the next request', async (t) => {
  let current = generateToken();
  const { port } = await startProxy(t, { getToken: () => current });
  const old = current;
  assert.equal((await chat(port, old, USER_ONLY)).status, 200);
  current = generateToken();
  assert.equal((await chat(port, old, USER_ONLY)).status, 401);
  assert.equal((await chat(port, current, USER_ONLY)).status, 200);
});

test('no configured token means every request is refused', async (t) => {
  const { port } = await startProxy(t, { getToken: () => '' });
  assert.equal((await request(port, { path: '/health', token: '' })).status, 401);
  assert.equal((await request(port, { path: '/health' })).status, 401);
});

test('requests naming a non-loopback host are refused (DNS rebinding guard)', async (t) => {
  const { port, token } = await startProxy(t);
  const res = await request(port, { path: '/health', token, headers: { Host: `attacker.example:${port}` } });
  assert.equal(res.status, 403);
  assert.equal(res.body.error.code, 'forbidden_host');
  for (const host of [`localhost:${port}`, `127.0.0.1:${port}`, `[::1]:${port}`]) {
    assert.equal((await request(port, { path: '/health', token, headers: { Host: host } })).status, 200, host);
  }
});

test('browser requests (Origin header) are refused even with the token', async (t) => {
  const { port, token } = await startProxy(t);
  const res = await chat(port, token, USER_ONLY, { headers: { Origin: 'https://site.example' } });
  assert.equal(res.status, 403);
  assert.equal(res.body.error.code, 'browser_request');
});

test('isLoopbackHost and hostnameOf', () => {
  for (const host of ['127.0.0.1', '127.4.5.6', 'localhost', 'LOCALHOST', '::1', '[::1]', '0:0:0:0:0:0:0:1', '::ffff:127.0.0.1']) {
    assert.equal(isLoopbackHost(host), true, host);
  }
  for (const host of ['0.0.0.0', '::', '192.168.1.20', '10.0.0.1', 'example.com', '', undefined, '128.0.0.1']) {
    assert.equal(isLoopbackHost(host), false, String(host));
  }
  assert.equal(hostnameOf('127.0.0.1:5757'), '127.0.0.1');
  assert.equal(hostnameOf('[::1]:5757'), '::1');
  assert.equal(hostnameOf('Example.COM'), 'example.com');
  assert.equal(hostnameOf(''), '');
});
