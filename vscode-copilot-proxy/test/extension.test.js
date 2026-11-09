'use strict';

// Loads extension.js against a fake `vscode` module: activation, auto-start, commands, the status
// bar item and the connection file (written to a temporary home folder).

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const core = require('../lib/proxy-core');
const { USER_ONLY, chat, fakeModel, lmError, request, textResponse, waitFor } = require('./helpers');

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function fakeVscode({ settings, models }) {
  const commands = new Map();
  const shown = [];
  const answers = [];   // [predicate(kind, message), answer]
  let clipboard = '';
  const statusItem = { text: '', tooltip: '', show() {}, dispose() {} };
  const output = { lines: [], appendLine(line) { this.lines.push(line); }, show() {}, dispose() {} };
  const configListeners = [];

  class CancellationTokenSource {
    constructor() {
      const listeners = new Set();
      this.listeners = listeners;
      this.token = {
        isCancellationRequested: false,
        onCancellationRequested: (fn) => {
          listeners.add(fn);
          return { dispose: () => listeners.delete(fn) };
        },
      };
    }

    cancel() {
      if (this.token.isCancellationRequested) return;
      this.token.isCancellationRequested = true;
      for (const fn of this.listeners) fn();
    }

    dispose() {}
  }

  class LanguageModelError extends Error {
    constructor(message, code) {
      super(message);
      this.name = 'LanguageModelError';
      this.code = code;
    }
  }

  const respond = (kind) => async (message, ...rest) => {
    shown.push({ kind, message, options: rest[0] && typeof rest[0] === 'object' ? rest[0] : null });
    const match = answers.find(([predicate]) => predicate(kind, message));
    return match ? match[1] : undefined;
  };

  const vscode = {
    StatusBarAlignment: { Left: 1, Right: 2 },
    ProgressLocation: { Notification: 15 },
    LanguageModelChatMessage: {
      User: (text) => ({ role: 'user', text }),
      Assistant: (text) => ({ role: 'assistant', text }),
    },
    LanguageModelError,
    CancellationTokenSource,
    lm: { selectChatModels: async () => models },
    env: { clipboard: { writeText: async (text) => { clipboard = text; } } },
    window: {
      createOutputChannel: () => output,
      createStatusBarItem: () => statusItem,
      showInformationMessage: respond('info'),
      showWarningMessage: respond('warning'),
      showErrorMessage: respond('error'),
      withProgress: async (_options, task) => task({ report() {} }, new CancellationTokenSource().token),
    },
    workspace: {
      getConfiguration: () => ({ get: (key, fallback) => (key in settings ? settings[key] : fallback) }),
      onDidChangeConfiguration: (fn) => {
        configListeners.push(fn);
        return { dispose() {} };
      },
    },
    commands: {
      registerCommand: (id, fn) => {
        commands.set(id, fn);
        return { dispose() {} };
      },
    },
  };
  return {
    vscode, commands, shown, answers, statusItem, output, configListeners, clipboard: () => clipboard,
  };
}

/** Fresh extension module bound to a fake vscode, with HOME pointed at a temp folder. */
function loadExtension(t, { settings, models }) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'screening-proxy-home-'));
  const saved = { HOME: process.env.HOME, USERPROFILE: process.env.USERPROFILE };
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  const fake = fakeVscode({ settings, models });
  const originalLoad = Module._load;
  Module._load = function load(requestName, ...rest) {
    if (requestName === 'vscode') return fake.vscode;
    return originalLoad.call(this, requestName, ...rest);
  };
  const extensionPath = require.resolve('../extension');
  delete require.cache[extensionPath];
  const extension = require('../extension');
  const store = new Map();
  const context = {
    subscriptions: [],
    extension: { packageJSON: { version: '0.1.0' } },
    globalState: {
      get: (key) => store.get(key),
      update: async (key, value) => { store.set(key, value); },
    },
  };
  t.after(async () => {
    await extension.deactivate();
    Module._load = originalLoad;
    delete require.cache[extensionPath];
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    fs.rmSync(home, { recursive: true, force: true });
  });
  return { extension, context, store, fake, connectionFile: path.join(home, core.CONNECTION_FILE_NAME) };
}

test('activation auto-starts the proxy, publishes the connection file and serves Copilot', async (t) => {
  const port = await freePort();
  const model = fakeModel({ id: 'gpt-4.1', reply: 'OK from Copilot' });
  const { extension, context, store, fake, connectionFile } = loadExtension(t, { settings: { port }, models: [model] });
  extension.activate(context);
  assert.deepEqual([...fake.commands.keys()].sort(), [
    'screeningProxy.copyToken', 'screeningProxy.rotateToken', 'screeningProxy.showStatus', 'screeningProxy.start',
    'screeningProxy.stop',
  ]);
  await waitFor(() => fake.statusItem.text.endsWith('Copilot proxy: on'));

  const token = store.get('screeningProxy.token');
  assert.match(token, /^[0-9a-f]{64}$/);
  const info = JSON.parse(fs.readFileSync(connectionFile, 'utf8'));
  assert.deepEqual(Object.keys(info).sort(), ['base_url', 'pid', 'started_at', 'token', 'version']);
  assert.equal(info.base_url, `http://127.0.0.1:${port}/v1`);
  assert.equal(info.token, token);
  assert.equal(info.pid, process.pid);
  assert.equal(info.version, '0.1.0');

  const res = await chat(port, token, { model: 'gpt-4.1', ...USER_ONLY, temperature: 0.1 });
  assert.equal(res.status, 200);
  assert.equal(res.body.choices[0].message.content, 'OK from Copilot');
  assert.deepEqual(model.calls[0].options, { justification: 'Candidate screening console', modelOptions: { temperature: 0.1 } });
  await waitFor(() => fake.output.lines.some((line) => line.includes('POST /v1/chat/completions 200')));
  assert.ok(!fake.output.lines.join('\n').includes(token));
  assert.equal(fake.shown.length, 0, 'auto-start is quiet');

  await fake.commands.get('screeningProxy.copyToken')();
  assert.equal(fake.clipboard(), token);

  await fake.commands.get('screeningProxy.stop')();
  assert.equal(fake.statusItem.text.endsWith('Copilot proxy: off'), true);
  assert.equal(fs.existsSync(connectionFile), false);
  assert.equal((await core.probeHealth({ port, token, timeoutMs: 3000 })).status, 0);

  await fake.commands.get('screeningProxy.start')();
  assert.equal(fake.statusItem.text.endsWith('Copilot proxy: on'), true);
  assert.equal(JSON.parse(fs.readFileSync(connectionFile, 'utf8')).token, token, 'the token survives restarts');
});

test('Rotate Token replaces the token everywhere at once', async (t) => {
  const port = await freePort();
  const { extension, context, store, fake, connectionFile } = loadExtension(t, { settings: { port }, models: [fakeModel()] });
  extension.activate(context);
  await waitFor(() => fake.statusItem.text.endsWith('Copilot proxy: on'));
  const old = store.get('screeningProxy.token');
  fake.answers.push([(kind, message) => kind === 'warning' && message.startsWith('Rotate'), 'Rotate']);
  await fake.commands.get('screeningProxy.rotateToken')();
  const fresh = store.get('screeningProxy.token');
  assert.match(fresh, /^[0-9a-f]{64}$/);
  assert.notEqual(fresh, old);
  assert.equal(JSON.parse(fs.readFileSync(connectionFile, 'utf8')).token, fresh);
  assert.equal((await chat(port, old, USER_ONLY)).status, 401);
  assert.equal((await chat(port, fresh, USER_ONLY)).status, 200);
});

test('Show Status lists the URL and models; consent errors offer a user-initiated access test', async (t) => {
  const port = await freePort();
  let denied = true;
  const model = fakeModel({
    id: 'reasoner-large-2',
    behavior: () => {
      if (denied) {
        denied = false;
        throw lmError('NoPermissions');
      }
      return textResponse(['OK']);
    },
  });
  const { extension, context, store, fake } = loadExtension(t, { settings: { port }, models: [model] });
  extension.activate(context);
  await waitFor(() => fake.statusItem.text.endsWith('Copilot proxy: on'));
  const token = store.get('screeningProxy.token');

  await fake.commands.get('screeningProxy.showStatus')();
  const status = fake.shown.find((s) => s.message === 'Copilot proxy: on');
  assert.ok(status && status.options.modal);
  assert.match(status.options.detail, new RegExp(`URL: http://127\\.0\\.0\\.1:${port}/v1`));
  assert.match(status.options.detail, /reasoner-large-2/);

  fake.answers.push([(kind) => kind === 'warning', 'Allow Access']);
  const res = await chat(port, token, USER_ONLY);
  assert.equal(res.status, 403);
  assert.equal(res.body.error.code, 'consent_required');
  await waitFor(() => fake.shown.some((s) => s.kind === 'info' && s.message.startsWith('GitHub Copilot access works')));
  assert.equal(model.calls.length, 2);
  assert.equal((await chat(port, token, USER_ONLY)).status, 200);
});

test('a port already served by another window is reported as on, not as an error', async (t) => {
  const port = await freePort();
  const token = core.generateToken();
  const other = core.createProxy({
    lm: { selectChatModels: async () => [] },
    messageFactory: { User: (x) => x, Assistant: (x) => x },
    getToken: () => token,
  });
  await other.listen(port, '127.0.0.1');
  t.after(() => other.close());
  t.mock.timers.enable({ apis: ['setInterval'] });
  const { extension, context, store, fake, connectionFile } = loadExtension(t, { settings: { port }, models: [] });
  store.set('screeningProxy.token', token);
  extension.activate(context);
  await waitFor(() => fake.statusItem.text.endsWith('Copilot proxy: on'));
  assert.match(fake.statusItem.tooltip, /another VS Code window/);
  assert.equal(fake.shown.filter((s) => s.kind === 'error').length, 0);
  assert.equal(fs.existsSync(connectionFile), false, 'only the owning window writes the file');
  assert.equal((await request(port, { path: '/health', token })).status, 200);

  await fake.commands.get('screeningProxy.stop')();            // not this window's server
  assert.match(fake.shown.at(-1).message, /run "Screening Proxy: Stop" in that window/);
  assert.equal(fake.statusItem.text.endsWith('Copilot proxy: on'), true);

  t.mock.timers.tick(30 * 1000);                                // the other window is still serving
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal(fs.existsSync(connectionFile), false);

  await other.close();                                          // e.g. that window was closed
  t.mock.timers.tick(30 * 1000);
  await waitFor(() => fs.existsSync(connectionFile), 5000);
  assert.equal(JSON.parse(fs.readFileSync(connectionFile, 'utf8')).base_url, `http://127.0.0.1:${port}/v1`);
  assert.match(fake.statusItem.tooltip, new RegExp(`127\\.0\\.0\\.1:${port}`));
  assert.ok(fake.output.lines.some((line) => line.includes('taking over')));
});

test('a port taken by another program is reported as an error', async (t) => {
  const sockets = new Set();
  const blocker = net.createServer((socket) => {
    sockets.add(socket);
    socket.on('error', () => {});
    socket.resume(); // consume the request so the connection can close
    socket.end('HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n');
  });
  await new Promise((resolve) => blocker.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => {
    for (const socket of sockets) socket.destroy();
    blocker.close(resolve);
  }));
  const { port } = blocker.address();
  const { extension, context, fake } = loadExtension(t, { settings: { port }, models: [] });
  extension.activate(context);
  await waitFor(() => fake.shown.some((s) => s.kind === 'error'));
  assert.match(fake.shown.find((s) => s.kind === 'error').message, /already used by another program/);
  assert.equal(fake.statusItem.text.endsWith('Copilot proxy: off'), true);
});

test('settings changes restart the proxy on the new port', async (t) => {
  const port = await freePort();
  const settings = { port };
  const { extension, context, store, fake, connectionFile } = loadExtension(t, { settings, models: [fakeModel()] });
  extension.activate(context);
  await waitFor(() => fake.statusItem.text.endsWith('Copilot proxy: on'));
  const token = store.get('screeningProxy.token');
  const next = await freePort();
  settings.port = next;
  for (const listener of fake.configListeners) listener({ affectsConfiguration: (section) => section === 'screeningProxy' });
  await waitFor(() => fs.existsSync(connectionFile)
    && JSON.parse(fs.readFileSync(connectionFile, 'utf8')).base_url === `http://127.0.0.1:${next}/v1`);
  assert.equal((await chat(next, token, USER_ONLY)).status, 200);
  assert.equal((await core.probeHealth({ port, token, timeoutMs: 3000 })).status, 0);
});
