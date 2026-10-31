// Vue 生命周期与浏览器协议桩验证；不打开麦克风，不调用 ASR 或模型。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as vue from 'vue';

async function setup({ permission, media } = {}) {
  let now = 1000, sequence = 0, api, sent = 0, mediaCalls = 0;
  const timers = new Map(), frames = new Map(), listeners = new Set(), spoken = [], recognizers = [];
  const setTimer = (callback, delay, interval = false) => {
    const id = ++sequence; timers.set(id, { callback, at: now + delay, delay, interval }); return id;
  };
  const synthesis = { speaking: false, pending: false, paused: false,
    speak: item => spoken.push(item), cancel() {}, resume() {}, getVoices: () => [],
    addEventListener: (_, callback) => listeners.add(callback),
    removeEventListener: (_, callback) => listeners.delete(callback) };
  class Recognition {
    constructor() { recognizers.push(this); }
    start() { this.started = true; }
    stop() { this.stopped = true; }
    abort() { this.aborted = true; }
  }
  const context = vm.createContext({ console, Date: class extends Date { static now() { return now; } },
    setTimeout: (fn, delay) => setTimer(fn, delay), clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => setTimer(fn, delay, true), clearInterval: id => timers.delete(id),
    requestAnimationFrame: callback => { const id = ++sequence; frames.set(id, callback); return id; },
    cancelAnimationFrame: id => frames.delete(id),
    SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } },
    window: { speechSynthesis: synthesis, SpeechRecognition: Recognition, isSecureContext: true },
    navigator: { permissions: { query: permission || (async () => ({ state: 'granted' })) },
      mediaDevices: { getUserMedia: () => { mediaCalls++; return media(); } } },
    document: { createElement: () => ({ content: { textContent: 'synthetic reply' } }) } });
  const synthetic = values => new vm.SyntheticModule(Object.keys(values), function () {
    for (const [key, value] of Object.entries(values)) this.setExport(key, value);
  }, { context });
  const imports = {
    vue: synthetic(vue),
    '../../api/ai/aliasr.js': synthetic({ getAliAsrToken: async () => ({ code: 200, data: {} }) }),
    '../../utils/aliAsrAdapter.js': synthetic({ AliAsrAdapter: Recognition }),
    '../../utils/safeHtml.js': synthetic({ sanitizeRichHtml: value => value }),
    '../../app/ui/feedback': synthetic({ message: { warning() {}, error() {} }, warning() {} }),
  };
  const source = fs.readFileSync(new URL('../src/shared/ui/useChatSpeech.js', import.meta.url), 'utf8');
  const module = new vm.SourceTextModule(source, { context });
  await module.link(name => {
    assert.ok(imports[name], `Unexpected dependency: ${name}`); return imports[name];
  });
  await module.evaluate();
  const state = { messages: vue.ref([]), isSending: vue.ref(false), activeConversation: vue.ref({ id: 'one' }),
    newMessage: vue.ref(''), lastSentMessage: vue.ref(''), scrollToBottom() {},
    sendMessage() { sent++; }, stopMessage() {} };
  const renderer = vue.createRenderer({ createElement: () => ({}), createText: () => ({}),
    createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {},
    parentNode: () => null, nextSibling: () => null, patchProp() {} });
  const app = renderer.createApp({ setup() { api = module.namespace.useChatSpeech(state); return () => null; } });
  app.mount({});
  const settle = async () => { for (let n = 0; n < 12; n++) { await Promise.resolve(); await vue.nextTick(); } };
  const advance = async duration => {
    const end = now + duration;
    for (let n = 0; n < 2000; n++) {
      const entry = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!entry) break;
      const [id, timer] = entry;
      now = timer.at;
      if (timer.interval) timer.at += timer.delay; else timers.delete(id);
      timer.callback(); await settle();
      assert.ok(n < 1999, 'Unbounded timer retry');
    }
    now = end; await settle();
  };
  return { api, state, app, settle, advance, timers, frames, listeners, spoken, recognizers,
    sent: () => sent, mediaCalls: () => mediaCalls };
}

let passed = 0;
async function check(name, callback) { await callback(); passed++; console.log(`PASS ${name}`); }
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

await check('unmount cancels pending send, speech, animation and recognition', async () => {
  const f = await setup();
  f.api.isVoiceMode.value = true; await f.settle();
  f.api.startRecording();
  const rec = f.recognizers.at(-1);
  rec.onresult({ results: [{ 0: { transcript: 'synthetic' }, isFinal: true }] });
  await f.advance(400); f.api.stopRecording();
  assert.ok(f.timers.size > 0 && f.frames.size > 0);
  f.app.unmount(); await f.advance(5000);
  assert.equal(f.sent(), 0); assert.ok(rec.aborted);
  assert.equal(f.timers.size, 0); assert.equal(f.frames.size, 0); assert.equal(f.listeners.size, 0);
});
await check('conversation switch invalidates old callbacks and pending send', async () => {
  const f = await setup(); f.api.isVoiceMode.value = true; await f.settle();
  f.api.startRecording(); const rec = f.recognizers.at(-1), late = rec.onresult;
  rec.onresult({ results: [{ 0: { transcript: 'old conversation' }, isFinal: true }] });
  await f.advance(400); f.api.stopRecording();
  f.state.activeConversation.value = { id: 'two' }; await f.settle();
  late({ results: [{ 0: { transcript: 'late callback' }, isFinal: true }] });
  await f.advance(3000);
  assert.equal(f.sent(), 0); assert.equal(f.api.recognizedPreviewText.value, '');
  assert.equal(f.api.isVoiceMode.value, false); assert.equal(f.timers.size, 0);
  f.app.unmount();
});
await check('late permission response cannot resume after unmount', async () => {
  const pending = deferred(), f = await setup({ permission: () => pending.promise });
  f.api.isVoiceMode.value = true; await f.settle(); f.app.unmount();
  pending.resolve({ state: 'prompt' }); await f.settle();
  assert.equal(f.mediaCalls(), 0); assert.equal(f.recognizers.length, 0); assert.equal(f.timers.size, 0);
});
await check('late microphone stream is released after conversation switch', async () => {
  const pending = deferred(); let stopped = 0;
  const f = await setup({ permission: async () => ({ state: 'prompt' }), media: () => pending.promise });
  f.api.isVoiceMode.value = true; await f.settle(); assert.equal(f.mediaCalls(), 1);
  f.state.activeConversation.value = { id: 'two' };
  pending.resolve({ getTracks: () => [{ stop() { stopped++; } }] }); await f.settle();
  f.api.startRecording(); await f.settle();
  assert.equal(stopped, 1); assert.equal(f.recognizers.length, 0); f.app.unmount();
});
await check('superseded streaming utterance cannot drain new queue', async () => {
  const f = await setup(); f.api.isVoiceMode.value = true; await f.settle();
  f.api.startStreamingTTS(); f.api.streamTTS('first sentence.'); const old = f.spoken.at(-1);
  f.api.startStreamingTTS(); f.api.streamTTS('second sentence.'); f.api.streamTTS('second sentence. queued sentence.');
  const before = f.spoken.length;
  old.onend(); old.onstart(); assert.equal(f.spoken.length, before);
  f.app.unmount(); old.onend(); assert.equal(f.spoken.length, before); assert.equal(f.timers.size, 0);
});
console.log(JSON.stringify({ scope: 'local_vue_lifecycle_browser_stubs', passed, realNetwork: false, microphoneOpened: false }));
