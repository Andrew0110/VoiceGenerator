const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const flush = async () => {
  for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve));
};

function setup(jobs = []) {
  const nodes = new Map(), timers = new Map(), requests = [];
  let timerId = 0;
  const server = { jobs, voices: [], installed: true, offline: false, respond: null };
  function node() {
    return {
      value: '', hidden: false, dataset: {}, handlers: {},
      classList: { toggle() {} },
      addEventListener(name, callback) { this.handlers[name] = callback; },
      append() {}, replaceChildren() {}, setAttribute() {},
      querySelectorAll() { return []; },
    };
  }
  function get(selector) {
    if (!nodes.has(selector)) nodes.set(selector, node());
    return nodes.get(selector);
  }
  get('#speed').value = '1';
  get('#output-format').value = 'wav';
  get('#mp3-bitrate').value = '64';
  get('#quality').value = '32';
  get('#language').value = 'uk';
  const document = {
    hidden: false, handlers: {}, querySelector: get,
    querySelectorAll() { return []; }, createElement: node,
    addEventListener(name, callback) { this.handlers[name] = callback; },
  };
  const context = vm.createContext({
    document,
    window: { addEventListener() {}, dispatchEvent() {} },
    location: { hash: '' },
    localStorage: { getItem() { return null; }, setItem() {} },
    VoiceRecorder: class {},
    Option: class {},
    CustomEvent: class {},
    Intl, URL,
    setTimeout(callback, delay) {
      const id = ++timerId;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options = {}) {
      requests.push({ url, method: options.method || 'GET' });
      if (server.offline) throw new Error('Connection refused');
      if (server.respond) return server.respond(url, options);
      const body = url === '/api/status' ? { installed: server.installed }
        : url === '/api/voices' ? server.voices
        : options.method === 'POST' ? server.jobs[0] : server.jobs;
      return { ok: true, json: async () => JSON.parse(JSON.stringify(body)) };
    },
  });
  vm.runInContext(code + `
    renderVoices = () => {};
    renderJobs = () => {};
    updateButton = () => {};
    globalThis.studio = { state, refresh };
  `, context);
  return {
    server, requests, timers, document, get, studio: context.studio,
    async tick(delay) {
      const entry = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(entry, 'Expected a scheduled refresh');
      timers.delete(entry[0]);
      await entry[1].callback();
      await flush();
    },
    async visibility(hidden) {
      document.hidden = hidden;
      document.handlers.visibilitychange();
      await flush();
    },
  };
}

test('initial load fetches all resources once and stays idle without a timer', async () => {
  const s = setup();
  await flush();
  assert.deepEqual(s.requests.map(r => r.url), ['/api/status', '/api/voices', '/api/generations']);
  assert.equal(s.timers.size, 0);
});

test('submitting a generation polls only jobs until completion', async () => {
  const s = setup();
  await flush();
  s.requests.length = 0;
  s.server.jobs = [{ id: 'new', status: 'queued' }];
  await s.get('#generate').handlers.click();
  s.server.jobs[0].status = 'running';
  await s.tick(2000);
  assert.equal(s.studio.state.jobs[0].status, 'running');
  s.server.jobs[0].status = 'completed';
  await s.tick(2000);
  assert.equal(s.studio.state.jobs[0].status, 'completed');
  assert.deepEqual(s.requests, [
    { url: '/api/generations', method: 'POST' },
    { url: '/api/generations', method: 'GET' },
    { url: '/api/generations', method: 'GET' },
    { url: '/api/generations', method: 'GET' },
  ]);
  assert.equal([...s.timers.values()].filter(t => t.delay === 2000).length, 0);
});

test('existing active jobs are tracked until every job completes or fails', async () => {
  const s = setup([{ id: 'one', status: 'running' }, { id: 'two', status: 'queued' }]);
  await flush();
  s.server.jobs[0].status = 'completed';
  await s.tick(2000);
  assert.equal(s.timers.size, 1);
  s.server.jobs[1].status = 'failed';
  await s.tick(2000);
  assert.equal(s.timers.size, 0);
});

test('voice changes refresh only voices and editor saves refresh only jobs', async () => {
  const s = setup();
  await flush();
  s.requests.length = 0;
  s.server.voices = [{ id: 'voice', name: 'New name' }];
  await s.studio.refresh({ voices: true });
  assert.equal(s.studio.state.voices[0].name, 'New name');
  s.server.jobs = [{ id: 'mix', status: 'completed' }];
  await s.studio.refresh();
  assert.equal(s.studio.state.jobs[0].id, 'mix');
  assert.deepEqual(s.requests.map(r => r.url), ['/api/voices', '/api/generations']);
  assert.equal(s.timers.size, 0);
});

test('hidden tabs stop polling and resynchronize when shown', async () => {
  const s = setup([{ id: 'active', status: 'running' }]);
  await flush();
  assert.equal(s.timers.size, 1);
  await s.visibility(true);
  assert.equal(s.timers.size, 0);
  s.requests.length = 0;
  s.server.jobs[0].status = 'completed';
  s.server.voices = [{ id: 'added-in-another-tab' }];
  await s.visibility(false);
  assert.deepEqual(s.requests.map(r => r.url), ['/api/status', '/api/voices', '/api/generations']);
  assert.equal(s.studio.state.voices[0].id, 'added-in-another-tab');
  assert.equal(s.timers.size, 0);
});

test('connection errors retry and restore the interface after recovery', async () => {
  const s = setup();
  await flush();
  s.server.offline = true;
  await s.studio.refresh();
  assert.equal(s.get('#connection-notice').hidden, false);
  assert.equal(s.studio.state.installed, false);
  s.requests.length = 0;
  s.server.offline = false;
  await s.tick(5000);
  assert.deepEqual(s.requests.map(r => r.url), ['/api/status', '/api/voices', '/api/generations']);
  assert.equal(s.get('#connection-notice').hidden, true);
  assert.equal(s.studio.state.installed, true);
  assert.equal(s.timers.size, 0);
});

test('a mutation refresh waits for an earlier request and then reads fresh data', async () => {
  const s = setup();
  await flush();
  let release;
  s.server.respond = () => new Promise(resolve => { release = resolve; });
  const first = s.studio.refresh();
  const afterMutation = s.studio.refresh();
  s.server.respond = null;
  s.server.jobs = [{ id: 'saved-after-first-request', status: 'completed' }];
  release({ ok: true, json: async () => [] });
  await Promise.all([first, afterMutation]);
  assert.equal(s.studio.state.jobs[0].id, 'saved-after-first-request');
  assert.equal(s.timers.size, 0);
});
