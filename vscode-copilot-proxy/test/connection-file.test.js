'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  CONNECTION_FILE_NAME, SERVICE_NAME, baseUrlFor, connectionFilePath, connectionInfo, generateToken, probeHealth,
  readConnectionFile, removeConnectionFile, writeConnectionFile,
} = require('../lib/proxy-core');
const { startProxy } = require('./helpers');

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'screening-proxy-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

test('the connection file lives in the home folder as .screening-copilot-proxy.json', () => {
  assert.equal(CONNECTION_FILE_NAME, '.screening-copilot-proxy.json');
  assert.equal(connectionFilePath(), path.join(os.homedir(), CONNECTION_FILE_NAME));
  assert.equal(connectionFilePath('/some/home'), path.join('/some/home', CONNECTION_FILE_NAME));
});

test('connectionInfo has exactly base_url, token, pid, started_at and version', () => {
  const token = generateToken();
  const info = connectionInfo({ host: '127.0.0.1', port: 5757, token, version: '0.1.0', startedAt: '2026-01-02T03:04:05.000Z' });
  assert.deepEqual(info, {
    base_url: 'http://127.0.0.1:5757/v1', token, pid: process.pid, started_at: '2026-01-02T03:04:05.000Z', version: '0.1.0',
  });
});

test('baseUrlFor maps wildcard binds to 127.0.0.1 and brackets IPv6 hosts', () => {
  assert.equal(baseUrlFor('127.0.0.1', 5757), 'http://127.0.0.1:5757/v1');
  assert.equal(baseUrlFor('0.0.0.0', 6000), 'http://127.0.0.1:6000/v1');
  assert.equal(baseUrlFor('::', 6000), 'http://127.0.0.1:6000/v1');
  assert.equal(baseUrlFor('::1', 6000), 'http://[::1]:6000/v1');
  assert.equal(baseUrlFor('localhost', 6000), 'http://localhost:6000/v1');
  assert.equal(baseUrlFor('', 6000), 'http://127.0.0.1:6000/v1');
});

test('write/read round trip is atomic, private and removal checks ownership', (t) => {
  const dir = tempDir(t);
  const file = connectionFilePath(dir);
  const info = connectionInfo({
    host: '127.0.0.1', port: 5757, token: generateToken(), version: '0.1.0', startedAt: '2026-01-02T03:04:05.000Z', pid: 4242,
  });
  writeConnectionFile(file, info);
  assert.deepEqual(readConnectionFile(file), info);
  assert.deepEqual(fs.readdirSync(dir), [CONNECTION_FILE_NAME], 'no temp files left behind');
  if (process.platform !== 'win32') assert.equal(fs.statSync(file).mode & 0o777, 0o600);

  const rotated = { ...info, token: generateToken() };
  writeConnectionFile(file, rotated);
  assert.equal(readConnectionFile(file).token, rotated.token);

  assert.equal(removeConnectionFile(file, { pid: 1, started_at: info.started_at }), false, 'another window owns it');
  assert.equal(removeConnectionFile(file, { pid: 4242, started_at: 'earlier start' }), false);
  assert.ok(fs.existsSync(file));
  assert.equal(removeConnectionFile(file, { pid: 4242, started_at: info.started_at }), true);
  assert.ok(!fs.existsSync(file));
  assert.equal(removeConnectionFile(file, { pid: 4242, started_at: info.started_at }), false);
});

test('readConnectionFile returns null for missing or malformed files', (t) => {
  const dir = tempDir(t);
  const file = path.join(dir, 'conn.json');
  assert.equal(readConnectionFile(file), null);
  fs.writeFileSync(file, '{broken');
  assert.equal(readConnectionFile(file), null);
  fs.writeFileSync(file, '[1, 2]');
  assert.equal(readConnectionFile(file), null);
  assert.equal(removeConnectionFile(file, { pid: 1, started_at: 'x' }), false);
});

test('probeHealth recognises a running proxy, a wrong token and a closed port', async (t) => {
  const { port, token } = await startProxy(t);
  const ours = await probeHealth({ host: '127.0.0.1', port, token });
  assert.equal(ours.status, 200);
  assert.equal(ours.body.service, SERVICE_NAME);
  const wrong = await probeHealth({ host: '0.0.0.0', port, token: 'nope' });
  assert.equal(wrong.status, 401);
  assert.equal(wrong.body.error.code, 'invalid_token');

  const { proxy: other, port: closedPort } = await startProxy(t);
  await other.close();
  const closed = await probeHealth({ host: '127.0.0.1', port: closedPort, token, timeoutMs: 3000 });
  assert.equal(closed.status, 0);
});

test('listen reports EADDRINUSE when the port is taken', async (t) => {
  const { port } = await startProxy(t);
  const { CopilotProxy } = require('../lib/proxy-core');
  const second = new CopilotProxy({
    lm: { selectChatModels: async () => [] },
    messageFactory: { User: (x) => x, Assistant: (x) => x },
    getToken: () => 'x',
  });
  await assert.rejects(second.listen(port, '127.0.0.1'), (err) => err.code === 'EADDRINUSE');
  await second.close();
});
