const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../static/voice-recorder.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

function setup({ getUserMedia, supported = true, maxBytes, minSeconds, maxSeconds } = {}) {
  const ready = [], errors = [], states = [], tracks = [{ stopped: false, stop() { this.stopped = true; } }];
  const stream = { getTracks: () => tracks };
  let time = 100;
  let instance;
  let tick;
  class MediaRecorder {
    static isTypeSupported(type) { return supported && type.startsWith('audio/webm'); }
    constructor(stream, options) { this.stream = stream; this.mimeType = options?.mimeType || 'audio/mp4'; instance = this; }
    start() { this.state = 'recording'; }
    stop() {
      this.state = 'inactive';
      queueMicrotask(() => {
        this.ondataavailable?.({ data: new Blob(['encoded audio'], { type: this.mimeType }) });
        this.onstop?.();
      });
    }
  }
  const context = vm.createContext({
    navigator: { mediaDevices: { getUserMedia: getUserMedia || (async constraints => {
      assert.equal(constraints.audio, true); assert.equal(constraints.video, false); return stream;
    }) } },
    MediaRecorder, Blob, performance: { now: () => time }, setInterval: callback => { tick = callback; return 1; }, clearInterval: () => {},
  });
  vm.runInContext(code, context);
  const recorder = new context.VoiceRecorder({
    onState: value => states.push(value), onReady: value => ready.push(value), onError: value => errors.push(value), maxBytes, minSeconds, maxSeconds,
  });
  return { recorder, ready, errors, states, tracks, stream, context, tick: () => tick(), setTime: value => { time = value; }, instance: () => instance };
}

test('a recording longer than 15 seconds stops, previews, and releases the microphone', async () => {
  const s = setup();
  await s.recorder.start();
  assert.equal(s.recorder.phase, 'recording');
  s.setTime(60100);
  s.recorder.stop();
  assert.ok(s.tracks[0].stopped);
  await flush();
  assert.equal(s.ready.length, 1);
  assert.equal(s.ready[0].type, 'audio/webm;codecs=opus');
  assert.equal(s.recorder.phase, 'ready');
  assert.equal(s.states.at(-1).seconds, 60);
  assert.deepEqual(s.errors, []);
});

test('closing during a pending permission request releases any later stream', async () => {
  let allow;
  const s = setup({ getUserMedia: () => new Promise(resolve => { allow = resolve; }) });
  const starting = s.recorder.start();
  s.recorder.cancel();
  allow(s.stream);
  await starting;
  assert.ok(s.tracks[0].stopped);
  assert.equal(s.instance(), undefined);
  assert.equal(s.recorder.phase, 'idle');
});

test('permission denial is readable and leaves no active recorder', async () => {
  const s = setup({ getUserMedia: async () => { throw { name: 'NotAllowedError' }; } });
  await s.recorder.start();
  assert.match(s.errors[0], /Доступ до мікрофона/);
  assert.equal(s.recorder.phase, 'idle');
});

test('cancel while recording discards delayed encoded chunks', async () => {
  const s = setup();
  await s.recorder.start();
  s.setTime(30100);
  s.recorder.cancel();
  await flush();
  assert.ok(s.tracks[0].stopped);
  assert.deepEqual(s.ready, []);
});

test('a browser default format works when preferred codecs are unavailable', async () => {
  const s = setup({ supported: false });
  await s.recorder.start();
  s.setTime(5100);
  s.recorder.stop();
  await flush();
  assert.equal(s.ready[0].type, 'audio/mp4');
});

test('too short recordings can be retried without leaking microphone tracks', async () => {
  const s = setup();
  await s.recorder.start();
  s.setTime(1100);
  s.recorder.stop();
  await flush();
  assert.equal(s.ready.length, 0);
  assert.match(s.errors[0], /3 секунди/);
  assert.ok(s.tracks[0].stopped);
  await s.recorder.start();
  assert.equal(s.recorder.phase, 'recording');
  s.recorder.cancel();
});

test('recorder errors and oversized data are discarded and release the microphone', async () => {
  for (const oversized of [false, true]) {
    const s = setup({ maxBytes: 10 });
    await s.recorder.start();
    if (oversized) s.instance().ondataavailable({ data: new Blob(['x'.repeat(11)]) });
    else s.instance().onerror();
    await flush();
    assert.equal(s.errors.length, 1);
    assert.equal(s.ready.length, 0);
    assert.ok(s.tracks[0].stopped);
    assert.equal(s.recorder.phase, 'idle');
  }
});

test('unsupported browsers offer file upload instead of throwing', async () => {
  const s = setup();
  s.context.MediaRecorder = undefined;
  await s.recorder.start();
  assert.match(s.errors[0], /завантаж аудіофайл/);
  assert.equal(s.recorder.phase, 'idle');
});

test('audio editor accepts subsecond clips without changing the voice sample minimum', async () => {
  const s = setup({ minSeconds: .1 });
  await s.recorder.start();
  s.setTime(600);
  s.recorder.stop();
  await flush();
  assert.equal(s.ready.length, 1);
  assert.deepEqual(s.errors, []);
  assert.ok(s.tracks[0].stopped);
});

test('audio editor duration limit stops capture and delivers the recording', async () => {
  const s = setup({ minSeconds: .1, maxSeconds: 599 });
  await s.recorder.start();
  s.setTime(599100);
  s.tick();
  assert.equal(s.recorder.phase, 'stopping');
  assert.ok(s.tracks[0].stopped);
  await flush();
  assert.equal(s.ready.length, 1);
  assert.equal(s.recorder.phase, 'ready');
});
