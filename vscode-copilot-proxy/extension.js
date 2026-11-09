'use strict';

/**
 * VS Code wiring for the Screening Copilot proxy. Request handling lives in lib/proxy-core.js;
 * this file supplies the language-model API, settings, the token, commands, the status bar item
 * and the output channel. The output channel never receives prompts, responses or the token.
 */

const path = require('node:path');
const vscode = require('vscode');
const core = require('./lib/proxy-core');

const SECTION = 'screeningProxy';
const TOKEN_KEY = 'screeningProxy.token';
const TOKEN_RE = /^[0-9a-f]{64}$/;
const CONSENT_PROMPT_INTERVAL_MS = 60 * 1000;
const ACCESS_TEST_TIMEOUT_MS = 90 * 1000;
const TAKEOVER_CHECK_MS = 30 * 1000;

let extensionContext = null;
let output = null;
let statusItem = null;
let proxy = null;           // CopilotProxy listening in this window
let running = null;         // { host, port, baseUrl, startedAt } while `proxy` listens
let servedElsewhere = null; // { port } when another VS Code window already serves the port
let takeoverTimer = null;   // checks whether that window is still serving
let activeToken = '';
let lastConsentPrompt = 0;
let chain = Promise.resolve();

/** Run start/stop operations one after another (settings changes can trigger restarts). */
function serialized(task) {
  const run = chain.then(task, task);
  chain = run.catch(() => undefined);
  return run;
}

function now() {
  return new Date().toISOString();
}

function log(line) {
  try {
    if (output) output.appendLine(line);
  } catch {
    // the channel is gone during shutdown
  }
}

function settings() {
  const c = vscode.workspace.getConfiguration(SECTION);
  return {
    port: Number(c.get('port', core.DEFAULT_PORT)),
    host: String(c.get('host', core.DEFAULT_HOST) ?? '').trim() || core.DEFAULT_HOST,
    autoStart: c.get('autoStart', true) !== false,
    modelFamily: String(c.get('modelFamily', '') ?? '').trim(),
    maxConcurrency: Number(c.get('maxConcurrency', core.DEFAULT_MAX_CONCURRENCY)),
    requestTimeoutSec: Number(c.get('requestTimeoutSec', core.DEFAULT_TIMEOUT_SEC)),
  };
}

function version() {
  return String(extensionContext?.extension?.packageJSON?.version || '0.0.0');
}

function storedToken() {
  const token = extensionContext ? extensionContext.globalState.get(TOKEN_KEY) : undefined;
  return typeof token === 'string' && TOKEN_RE.test(token) ? token : '';
}

/** The token kept in globalState; created (random 32 bytes, hex) on first use. */
async function ensureToken() {
  let token = storedToken();
  if (!token) {
    token = core.generateToken();
    await extensionContext.globalState.update(TOKEN_KEY, token);
  }
  return token;
}

function isLanguageModelError(err) {
  return typeof vscode.LanguageModelError === 'function' && err instanceof vscode.LanguageModelError;
}

function updateStatus() {
  if (!statusItem) return;
  try {
    if (proxy && running) {
      statusItem.text = '$(radio-tower) Copilot proxy: on';
      statusItem.tooltip = `Screening Copilot proxy at ${running.baseUrl}\nClick for status`;
    } else if (servedElsewhere) {
      statusItem.text = '$(radio-tower) Copilot proxy: on';
      statusItem.tooltip = `Served by another VS Code window on port ${servedElsewhere.port}\nClick for status`;
    } else {
      statusItem.text = '$(circle-slash) Copilot proxy: off';
      statusItem.tooltip = 'Screening Copilot proxy is stopped\nClick for status';
    }
    statusItem.show();
  } catch {
    // the item is gone during shutdown
  }
}

function stopWatchingOtherWindow() {
  if (takeoverTimer) clearInterval(takeoverTimer);
  takeoverTimer = null;
}

/** While another window serves the port, take over once it stops (for example when it closes). */
function watchOtherWindow() {
  stopWatchingOtherWindow();
  takeoverTimer = setInterval(() => {
    if (!servedElsewhere || proxy) {
      stopWatchingOtherWindow();
      return;
    }
    const { port } = servedElsewhere;
    void core.probeHealth({ host: settings().host, port, token: activeToken }).then((probe) => {
      if (probe.status !== 0 || !servedElsewhere || proxy) return;
      log(`${now()} The other VS Code window stopped serving port ${port}; taking over`);
      void serialized(() => start({ quiet: true }));
    });
  }, TAKEOVER_CHECK_MS);
  if (takeoverTimer && typeof takeoverTimer.unref === 'function') takeoverTimer.unref();
}

function publishConnectionFile() {
  if (!running) return;
  try {
    core.writeConnectionFile(core.connectionFilePath(), core.connectionInfo({
      host: running.host, port: running.port, token: activeToken, version: version(), startedAt: running.startedAt,
    }));
  } catch (err) {
    log(`${now()} Could not write the connection file (${err && err.code ? err.code : 'error'}); `
      + 'set COPILOT_PROXY_TOKEN for the screening console instead.');
  }
}

async function start({ quiet = false } = {}) {
  if (proxy && running) {
    if (!quiet) void vscode.window.showInformationMessage(`Copilot proxy is already running at ${running.baseUrl}.`);
    return true;
  }
  const s = settings();
  if (!Number.isInteger(s.port) || s.port < 1 || s.port > 65535) {
    void vscode.window.showErrorMessage('Screening Proxy: "screeningProxy.port" must be a whole number from 1 to 65535.');
    updateStatus();
    return false;
  }
  activeToken = await ensureToken();
  if (!core.isLoopbackHost(s.host)) {
    void vscode.window.showWarningMessage(
      `Screening Proxy listens on ${s.host}: other machines on the network can reach it, and anyone with the `
      + 'token can use your GitHub Copilot access. Set "screeningProxy.host" back to 127.0.0.1 unless you need this.');
  }
  const instance = core.createProxy({
    lm: vscode.lm,
    messageFactory: {
      User: (text) => vscode.LanguageModelChatMessage.User(text),
      Assistant: (text) => vscode.LanguageModelChatMessage.Assistant(text),
    },
    createCancellation: () => {
      const source = new vscode.CancellationTokenSource();
      return { token: source.token, cancel: () => source.cancel(), dispose: () => source.dispose() };
    },
    isLanguageModelError,
    onConsentRequired: promptForConsent,
    getToken: () => activeToken,
    log,
    version: version(),
    modelFamily: s.modelFamily,
    maxConcurrency: s.maxConcurrency,
    requestTimeoutSec: s.requestTimeoutSec,
  });
  let address;
  try {
    address = await instance.listen(s.port, s.host);
  } catch (err) {
    await instance.close();
    return handleListenError(err, s, quiet);
  }
  proxy = instance;
  servedElsewhere = null;
  stopWatchingOtherWindow();
  running = {
    host: s.host, port: address.port, baseUrl: core.baseUrlFor(s.host, address.port), startedAt: now(),
  };
  publishConnectionFile();
  log(`${now()} Listening on ${running.baseUrl}`);
  updateStatus();
  if (!quiet) void vscode.window.showInformationMessage(`Copilot proxy listening on ${running.baseUrl}.`);
  return true;
}

async function handleListenError(err, s, quiet) {
  const code = err && err.code ? String(err.code) : 'unknown error';
  if (code === 'EADDRINUSE') {
    const probe = await core.probeHealth({ host: s.host, port: s.port, token: activeToken });
    if (probe.status === 200 && probe.body && probe.body.service === core.SERVICE_NAME) {
      servedElsewhere = { port: s.port };
      watchOtherWindow();
      log(`${now()} Port ${s.port} is already served by another VS Code window`);
      updateStatus();
      if (!quiet) {
        void vscode.window.showInformationMessage(`Another VS Code window already serves the Copilot proxy on port ${s.port}.`);
      }
      return true;
    }
    const otherProxy = probe.status === 401 && probe.body && probe.body.error && probe.body.error.code === 'invalid_token';
    void vscode.window.showErrorMessage(otherProxy
      ? `Screening Proxy: port ${s.port} is used by another Screening Copilot proxy with a different token `
        + '(for example in another VS Code profile). Stop it there or choose another "screeningProxy.port".'
      : `Screening Proxy: port ${s.port} is already used by another program. Choose another `
        + '"screeningProxy.port"; the screening console finds the new port automatically.');
  } else if (code === 'EACCES') {
    void vscode.window.showErrorMessage(`Screening Proxy is not allowed to listen on port ${s.port}. Choose another port.`);
  } else if (code === 'EADDRNOTAVAIL' || code === 'ENOTFOUND') {
    void vscode.window.showErrorMessage(`Screening Proxy: "${s.host}" is not an address of this computer. Use 127.0.0.1.`);
  } else {
    void vscode.window.showErrorMessage(`Screening Proxy could not start (${code}).`);
  }
  log(`${now()} Could not listen on ${s.host}:${s.port} (${code})`);
  updateStatus();
  return false;
}

async function stop({ quiet = false } = {}) {
  if (!proxy && servedElsewhere && !quiet) {
    void vscode.window.showInformationMessage(
      'Another VS Code window serves the Copilot proxy; run "Screening Proxy: Stop" in that window.');
    return;
  }
  const instance = proxy;
  const info = running;
  proxy = null;
  running = null;
  servedElsewhere = null;
  stopWatchingOtherWindow();
  if (instance) await instance.close();
  if (info) {
    core.removeConnectionFile(core.connectionFilePath(), { pid: process.pid, started_at: info.startedAt });
    log(`${now()} Stopped`);
  }
  updateStatus();
  if (!quiet) {
    void vscode.window.showInformationMessage(instance ? 'Copilot proxy stopped.' : 'Copilot proxy is not running in this window.');
  }
}

/** Shown when a request needs language-model consent; the button runs a user-initiated request. */
function promptForConsent() {
  const t = Date.now();
  if (t - lastConsentPrompt < CONSENT_PROMPT_INTERVAL_MS) return;
  lastConsentPrompt = t;
  void vscode.window.showWarningMessage(
    'The screening console asked to use GitHub Copilot, but VS Code has not yet allowed "Screening Copilot Proxy" '
    + 'to use language models.', 'Allow Access',
  ).then((pick) => {
    if (pick === 'Allow Access') void testCopilotAccess();
  });
}

/** Sends one tiny request from a user action, so VS Code can show its model-access dialog. */
async function testCopilotAccess() {
  let models = [];
  try {
    models = await core.listCopilotModels(vscode.lm);
  } catch {
    models = [];
  }
  if (models.length === 0) {
    void vscode.window.showErrorMessage(core.Errors.noModels().message);
    return;
  }
  const model = core.pickModel(models, '', settings().modelFamily);
  const source = new vscode.CancellationTokenSource();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    source.cancel();
  }, ACCESS_TEST_TIMEOUT_MS);
  try {
    await vscode.window.withProgress({
      location: vscode.ProgressLocation.Notification,
      title: `Testing GitHub Copilot access (${model.id})...`,
      cancellable: true,
    }, async (_progress, token) => {
      token.onCancellationRequested(() => source.cancel());
      const response = await model.sendRequest(
        [vscode.LanguageModelChatMessage.User('Reply with the single word OK.')],
        { justification: core.JUSTIFICATION }, source.token);
      // eslint-disable-next-line no-unused-vars
      for await (const _chunk of response.text) { /* drain the reply */ }
    });
    if (proxy) proxy.markConsentGranted();
    void vscode.window.showInformationMessage(`GitHub Copilot access works (model ${model.id}). The screening console can use the proxy.`);
  } catch (err) {
    const cancelled = source.token.isCancellationRequested && !timedOut;
    const mapped = core.toHttpError(err, { timedOut, timeoutSec: ACCESS_TEST_TIMEOUT_MS / 1000, modelId: model.id, isLanguageModelError });
    void vscode.window.showErrorMessage(cancelled ? 'GitHub Copilot access test cancelled.' : `GitHub Copilot access failed: ${mapped.message}`);
  } finally {
    clearTimeout(timer);
    source.dispose();
  }
}

async function showStatus() {
  const s = settings();
  let models = [];
  let listed = true;
  try {
    models = await core.listCopilotModels(vscode.lm);
  } catch {
    listed = false;
  }
  const lines = [];
  if (proxy && running) {
    const st = proxy.stats;
    lines.push(`URL: ${running.baseUrl}`);
    lines.push(st.requests
      ? `Requests: ${st.requests} (${st.failures} failed; last HTTP ${st.lastStatus} in `
        + `${(st.lastDurationMs / 1000).toFixed(1)} s${st.lastModel ? `, model ${st.lastModel}` : ''})`
      : 'Requests: none yet');
    lines.push(proxy.consentConfirmed
      ? `Concurrency: up to ${proxy.maxConcurrency} requests at a time`
      : 'Concurrency: one request at a time until the first request succeeds');
  } else if (servedElsewhere) {
    lines.push(`Served by another VS Code window on port ${servedElsewhere.port}.`);
  } else {
    lines.push('The proxy is not running in this window.');
  }
  if (models.length) {
    lines.push(`GitHub Copilot chat models: ${models.length} (default: ${core.pickModel(models, '', s.modelFamily).id})`);
    lines.push(`Available: ${models.map((m) => m.id).join(', ')}`);
  } else {
    lines.push(listed ? 'GitHub Copilot chat models: none. Sign in to GitHub Copilot in VS Code.'
      : 'GitHub Copilot chat models: could not be listed.');
  }
  lines.push(`Connection file: ${path.join('~', core.CONNECTION_FILE_NAME)}`);
  const title = proxy ? 'Copilot proxy: on' : servedElsewhere ? 'Copilot proxy: on (another window)' : 'Copilot proxy: off';
  const actions = [proxy ? 'Stop' : 'Start', 'Test Copilot Access', 'Copy Token', 'Open Log'];
  const pick = await vscode.window.showInformationMessage(title, { modal: true, detail: lines.join('\n') }, ...actions);
  if (pick === 'Start') await serialized(() => start());
  else if (pick === 'Stop') await serialized(() => stop());
  else if (pick === 'Test Copilot Access') await testCopilotAccess();
  else if (pick === 'Copy Token') await copyToken();
  else if (pick === 'Open Log' && output) output.show(true);
}

async function copyToken() {
  let token = proxy ? activeToken : '';
  if (!token && servedElsewhere) {
    const file = core.readConnectionFile(core.connectionFilePath());
    token = file && typeof file.token === 'string' ? file.token : '';
  }
  if (!token) token = await ensureToken();
  await vscode.env.clipboard.writeText(token);
  void vscode.window.showInformationMessage('Proxy token copied. The screening console normally reads it automatically; '
    + 'set COPILOT_PROXY_TOKEN only if it cannot.');
}

async function rotateToken() {
  if (!proxy && servedElsewhere) {
    void vscode.window.showWarningMessage('Another VS Code window serves the proxy; run "Screening Proxy: Rotate Token" there.');
    return;
  }
  const pick = await vscode.window.showWarningMessage('Rotate the Copilot proxy token?', {
    modal: true,
    detail: 'The old token stops working immediately. The screening console picks up the new token from the '
      + 'connection file; update COPILOT_PROXY_TOKEN wherever you set it by hand.',
  }, 'Rotate');
  if (pick !== 'Rotate') return;
  const token = core.generateToken();
  await extensionContext.globalState.update(TOKEN_KEY, token);
  activeToken = token;
  publishConnectionFile();
  log(`${now()} Token rotated`);
  void vscode.window.showInformationMessage('Proxy token rotated.');
}

function onConfigurationChanged(event) {
  if (!event.affectsConfiguration(SECTION)) return;
  if (proxy || servedElsewhere) {
    void serialized(async () => {
      await stop({ quiet: true });
      await start({ quiet: true });
    });
  }
}

function activate(context) {
  extensionContext = context;
  output = vscode.window.createOutputChannel('Screening Copilot Proxy');
  statusItem = vscode.window.createStatusBarItem('screeningProxy.status', vscode.StatusBarAlignment.Right, 100);
  statusItem.name = 'Screening Copilot Proxy';
  statusItem.command = 'screeningProxy.showStatus';
  context.subscriptions.push(
    output,
    statusItem,
    vscode.commands.registerCommand('screeningProxy.start', () => serialized(() => start())),
    vscode.commands.registerCommand('screeningProxy.stop', () => serialized(() => stop())),
    vscode.commands.registerCommand('screeningProxy.showStatus', () => showStatus()),
    vscode.commands.registerCommand('screeningProxy.copyToken', () => copyToken()),
    vscode.commands.registerCommand('screeningProxy.rotateToken', () => rotateToken()),
    vscode.workspace.onDidChangeConfiguration(onConfigurationChanged),
  );
  updateStatus();
  if (settings().autoStart) void serialized(() => start({ quiet: true }));
}

async function deactivate() {
  await serialized(() => stop({ quiet: true }));
}

module.exports = { activate, deactivate };
