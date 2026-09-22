'use strict';
// Container-side pinned Codex 0.154.0/0.155.1 driver. Auth RPC payloads never enter output.
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const crypto = require('node:crypto');

const root = path.dirname(path.resolve(process.argv[2] || ''));
const requestPath = path.join(root, 'request.json');
let child, activeThread, activeTurn, adopted, pollTimer, exiting = false;
let nextId = 1, currentAccount, updateBusy = false;
const pending = new Map(), completed = new Map(), waiters = new Map();
let buffer = Buffer.alloc(0);
let lastHeartbeat, lastHeartbeatAt = Date.now();
let nativeClosed = false, lastState = "failed", lifecycle = [];
function hostAlive() {
  const heartbeat = readPrivate(path.join(root, 'heartbeat.json'));
  if (typeof heartbeat.sequence !== 'string' || !/^[a-f0-9]{32}$/.test(heartbeat.sequence)) throw new Error('heartbeat_invalid');
  if (heartbeat.sequence !== lastHeartbeat) { lastHeartbeat = heartbeat.sequence; lastHeartbeatAt = Date.now(); }
  if (Date.now() - lastHeartbeatAt > 15000) throw new Error('host_unavailable');
  if (fs.existsSync(path.join(root, 'stop.json'))) throw new Error('host_stopped');
}
async function awaitStart() {
  const deadline = Date.now() + 60000;
  while (!exiting && Date.now() < deadline) {
    hostAlive();
    if (fs.existsSync(path.join(root, 'start.json'))) {
      const permit = readPrivate(path.join(root, 'start.json'));
      if (permit.schema !== 'dradar.managed_start.v1' || permit.generation !== adopted) throw new Error('start_invalid');
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('start_unavailable');
}

function readPrivate(file, limit = 262144) {
  const before = fs.lstatSync(file);
  if (!before.isFile() || before.isSymbolicLink() || (before.mode & 0o077)) throw new Error('private_file_invalid');
  const fd = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  try {
    const st = fs.fstatSync(fd);
    if (st.ino !== before.ino || st.dev !== before.dev || st.size > limit) throw new Error('private_file_changed');
    return JSON.parse(fs.readFileSync(fd, 'utf8'));
  } finally { fs.closeSync(fd); }
}
function writeMetadata(name, value) {
  const temporary = path.join(root, `.meta-${crypto.randomUUID()}`);
  const fd = fs.openSync(temporary, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
  try { fs.writeFileSync(fd, JSON.stringify(value)); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  fs.renameSync(temporary, path.join(root, name));
}
function status(state) {
  lastState = state;
  if (state === 'running' && !lifecycle.some(e => e.state === 'running'))
    lifecycle.push({ state: 'running', at: new Date().toISOString(), generation: adopted, native_closed: false });
  if (nativeClosed && !lifecycle.some(e => e.native_closed))
    lifecycle.push({ state, at: new Date().toISOString(), generation: adopted, native_closed: true });
  writeMetadata('status.json', { schema: 'dradar.managed_consumer.v1', state,
    generation: adopted || null, native_closed: nativeClosed, lifecycle, native_acceptance: adopted ? 'confirmed' : 'unknown', request_used: 'unknown' });
  process.stdout.write(JSON.stringify({ type: 'managed_runtime', state }) + '\n');
}
function generation() {
  const pointer = readPrivate(path.join(root, 'current.json'));
  if (!pointer || !/^[a-f0-9]{32}$/.test(pointer.generation)) throw new Error('generation_invalid');
  const data = readPrivate(path.join(root, `at-${pointer.generation}.json`));
  const allowed = ['access_token', 'account_id', 'expires_at', 'generation'];
  if (!data || Object.keys(data).some(k => !allowed.includes(k)) || Object.keys(data).length !== allowed.length ||
      data.generation !== pointer.generation || typeof data.account_id !== 'string' || !data.account_id ||
      typeof data.access_token !== 'string' || !/^[A-Za-z0-9_.-]+$/.test(data.access_token) ||
      typeof data.expires_at !== 'number' || !Number.isFinite(data.expires_at) || data.expires_at <= Date.now() / 1000 ||
      (currentAccount && currentAccount !== data.account_id)) throw new Error('generation_unavailable');
  return data;
}
function send(value) { if (!exiting) child.stdin.write(JSON.stringify(value) + '\n'); }
function rpc(method, params, timeout = 10000) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('rpc_timeout')); }, timeout);
    pending.set(id, { resolve, reject, timer });
    send({ id, method, params });
  });
}
async function applyGeneration(data) {
  const result = await rpc('account/login/start', { type: 'chatgptAuthTokens',
    accessToken: data.access_token, chatgptAccountId: data.account_id });
  if (result.type !== 'chatgptAuthTokens') throw new Error('adoption_failed');
  currentAccount = data.account_id; adopted = data.generation;
  status(activeTurn ? 'running' : 'ready');
}
async function refreshRequest(message) {
  const prior = adopted;
  if (message.params?.previousAccountId && message.params.previousAccountId !== currentAccount) {
    send({ id: message.id, error: { code: -32000, message: 'managed identity mismatch' } }); return;
  }
  writeMetadata('refresh-request.json', { schema: 'dradar.managed_refresh_request.v1', generation: prior, reason: 'unauthorized' });
  const deadline = Date.now() + 8000;
  while (!exiting && Date.now() < deadline) {
    try {
      const data = generation();
      if (data.generation !== prior) {
        send({ id: message.id, result: { accessToken: data.access_token, chatgptAccountId: data.account_id } });
        // The callback response is delivery, not an account/login/start ack.
        // A later explicit update will establish the adoption receipt.
        return;
      }
    } catch (_) { /* Wait only for a bounded, valid next generation. */ }
    await new Promise(r => setTimeout(r, 100));
  }
  send({ id: message.id, error: { code: -32000, message: 'managed credential unavailable' } });
}
function onMessage(message) {
  if (!message || typeof message !== 'object') throw new Error('rpc_invalid');
  if (message.method === 'account/chatgptAuthTokens/refresh' && message.id !== undefined) {
    refreshRequest(message).catch(() => shutdown(1)); return;
  }
  if (message.id !== undefined && !message.method) {
    const entry = pending.get(message.id);
    if (!entry) return;
    clearTimeout(entry.timer); pending.delete(message.id);
    if (message.error || !message.result) entry.reject(new Error('rpc_failed'));
    else entry.resolve(message.result);
    return;
  }
  if (message.method === 'turn/completed') {
    const turn = message.params?.turn;
    if (!turn || typeof turn.id !== 'string') throw new Error('turn_invalid');
    completed.set(turn.id, turn.status);
    if (waiters.has(turn.id)) { waiters.get(turn.id)(turn.status); waiters.delete(turn.id); }
  }
  // All other native events are deliberately omitted from this control log.
  // Native CODEX_HOME/sessions remains the authoritative trajectory source.
}
async function interrupt() {
  if (activeThread && activeTurn) {
    try { await rpc('turn/interrupt', { threadId: activeThread, turnId: activeTurn }, 2000); } catch (_) {}
  }
}
function shutdown(code) {
  if (exiting) return;
  exiting = true;
  clearInterval(pollTimer);
  for (const resolve of waiters.values()) resolve('failed');
  waiters.clear();
  for (const entry of pending.values()) { clearTimeout(entry.timer); entry.reject(new Error('runtime_closed')); }
  pending.clear();
  if (child) { try { child.kill('SIGTERM'); } catch (_) {} }
  process.exitCode = code;
  setTimeout(() => { if (child && child.exitCode === null) child.kill('SIGKILL'); }, 500).unref();
}
async function main() {
  const request = readPrivate(requestPath, 1024 * 1024);
  if (request.schema !== 'dradar.managed_run.v1' || typeof request.instruction !== 'string' ||
      typeof request.model !== 'string' || !request.model ||
      typeof request.effort !== 'string' || !/^[a-z][a-z0-9_]{0,31}$/.test(request.effort)) throw new Error('request_invalid');
  hostAlive();
  const initial = generation(); currentAccount = initial.account_id;
  const env = { ...process.env, CODEX_HOME: path.join(root, 'codex-home') };
  for (const key of ['OPENAI_API_KEY', 'CODEX_ACCESS_TOKEN', 'CODEX_AUTH_JSON_PATH', 'CODEX_FORCE_AUTH_JSON', 'RUST_LOG']) delete env[key];
  child = spawn('codex', ['--enable', 'unified_exec', 'app-server'], { cwd: process.cwd(), env, stdio: ['pipe', 'pipe', 'pipe'] });
  child.stdout.on('data', chunk => {
    buffer = Buffer.concat([buffer, chunk]);
    if (buffer.length > 2 * 1024 * 1024) { shutdown(1); return; }
    let index;
    while ((index = buffer.indexOf(10)) >= 0) {
      const line = buffer.subarray(0, index); buffer = buffer.subarray(index + 1);
      try { onMessage(JSON.parse(line.toString('utf8'))); } catch (_) { shutdown(1); return; }
    }
  });
  child.stdin.on('error', () => shutdown(1));
  child.stderr.on('data', () => {}); // Never copy auth/provider error bodies to logs.
  child.on('error', () => shutdown(1));
  child.on('exit', () => { if (!exiting) shutdown(1); });
  child.on('close', () => { nativeClosed = true; try { status(lastState); } catch (_) {} });
  await rpc('initialize', { clientInfo: { name: 'dradar_managed', version: '1' }, capabilities: { experimentalApi: true } });
  send({ method: 'initialized', params: {} });
  await applyGeneration(initial);
  await awaitStart();
  const thread = await rpc('thread/start', { model: request.model, modelProvider: 'openai', cwd: process.cwd(),
    approvalPolicy: 'never', sandbox: 'danger-full-access', ephemeral: false,
    config: { model_reasoning_effort: request.effort, model_reasoning_summary: request.summary || 'auto' } });
  activeThread = thread.thread?.id;
  if (typeof activeThread !== 'string') throw new Error('thread_invalid');
  const turn = await rpc('turn/start', { threadId: activeThread, model: request.model, effort: request.effort,
    approvalPolicy: 'never', input: [{ type: 'text', text: request.instruction, text_elements: [] }] });
  activeTurn = turn.turn?.id;
  if (typeof activeTurn !== 'string') throw new Error('turn_invalid');
  status('running');
  pollTimer = setInterval(async () => {
    if (exiting || updateBusy) return;
    updateBusy = true;
    try {
      try { hostAlive(); } catch (_) { await interrupt(); shutdown(1); return; }
      const next = generation();
      if (next.generation !== adopted) await applyGeneration(next);
    } catch (_) { /* Current native state remains distinct from failed delivery. */ }
    finally { updateBusy = false; }
  }, 200);
  const outcome = completed.has(activeTurn) ? completed.get(activeTurn) : await new Promise(resolve => waiters.set(activeTurn, resolve));
  clearInterval(pollTimer);
  status(outcome === 'completed' ? 'completed' : 'failed');
  shutdown(outcome === 'completed' ? 0 : 1);
}
process.on('SIGTERM', () => { interrupt().finally(() => shutdown(1)); });
process.on('SIGINT', () => { interrupt().finally(() => shutdown(1)); });
main().catch(() => { try { status('failed'); } catch (_) {} shutdown(1); });
