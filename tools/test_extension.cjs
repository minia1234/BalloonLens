const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function worker() {
  const event = { addListener() {} };
  const ctx = vm.createContext({
    chrome: {
      runtime: { onMessage: event, onInstalled: event },
      commands: { onCommand: event },
      tabs: { onUpdated: event, onRemoved: event },
    },
    setTimeout, clearTimeout, AbortController, URLSearchParams,
    performance, fetch,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../extension/sw.js'), 'utf8'), ctx);
  return ctx;
}

test('capture refuses another active tab, including after waiting in the capture queue', async () => {
  const ctx = worker();
  let captures = 0;
  let active = 2;
  ctx.chrome.tabs.query = async () => [{ id: active }];
  ctx.chrome.tabs.captureVisibleTab = async () => { captures++; return 'image'; };
  ctx.target = { id: 1, windowId: 3 };
  await assert.rejects(vm.runInContext('captureVisible(target)', ctx), /tab_not_active/);
  assert.equal(captures, 0);
  active = 1;
  let release;
  ctx.gate = new Promise(resolve => { release = resolve; });
  vm.runInContext('captureChain = gate', ctx);
  const pending = vm.runInContext('captureVisible(target)', ctx);
  active = 2;
  release();
  await assert.rejects(pending, /tab_not_active/);
  assert.equal(captures, 0);
});

test('capture checks the tab again when the browser finishes capturing', async () => {
  const ctx = worker();
  let active = 1;
  ctx.chrome.tabs.query = async () => [{ id: active }];
  ctx.chrome.tabs.captureVisibleTab = async () => { active = 2; return 'image'; };
  ctx.target = { id: 1, windowId: 3 };
  await assert.rejects(vm.runInContext('captureVisible(target)', ctx), /tab_not_active/);
});

test('an unresponsive image fetch times out so capture fallback can run', async () => {
  const ctx = worker();
  ctx.setTimeout = (fn) => setTimeout(fn, 5);
  ctx.fetch = (_url, options) => new Promise((resolve, reject) => {
    options?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    // Ensure the regression fails rather than hanging when there is no abort signal.
    setTimeout(() => resolve({ ok: true, blob: async () => ({ type: 'image/png', size: 1 }) }), 50);
  });
  await assert.rejects(vm.runInContext("fetchImage('https://example.test/image.png')", ctx), /image_timeout/);
});

test('a page goes to the helper as one binary frame: length, options JSON, then the image bytes, with no Base64', async () => {
  const ctx = worker();
  ctx.Blob = Blob;
  ctx.TextEncoder = TextEncoder;
  ctx.chrome.storage = { local: { get: async () => ({ token: 't' }), set: async () => {} } };
  const sent = [];
  ctx.fetch = async (url, init) => {
    sent.push({ url, init });
    return { ok: true, status: 200, json: async () => ({ id: 'j1' }) };
  };
  const image = new Uint8Array([137, 80, 78, 71, 0, 1, 2, 3, 250, 251]);
  ctx.blob = new Blob([image], { type: 'image/png' });
  ctx.opts = { src: 'ja', tgt: 'ko', model: 'm', reading: 'rtl', priority: 2, session: 's', scope: '',
    options: { vision: false, visible: [0.25, 0.75], work: { page_url: 'https://x.test/a', position: 0, memory: false } } };
  const out = await vm.runInContext('submitJob(blob, opts)', ctx);
  assert.deepEqual({ ...out }, { id: 'j1' });
  const [{ url, init }] = sent;
  assert.match(url, /\/v1\/jobs\?src=ja&tgt=ko&model=m&reading=rtl&priority=2&session=s&scope=$/);
  assert.equal(init.method, 'POST');
  assert.equal(init.headers['Content-Type'], 'application/x-balloonlens-page');
  assert.equal(init.headers['X-BalloonLens-Token'], 't');
  const body = new Uint8Array(await init.body.arrayBuffer());
  const size = new DataView(body.buffer).getUint32(0);
  const options = JSON.parse(new TextDecoder().decode(body.slice(4, 4 + size)));
  assert.deepEqual(options.visible, [0.25, 0.75]);
  assert.equal(options.work.page_url, 'https://x.test/a');
  assert.deepEqual([...body.slice(4 + size)], [...image]);   // the image bytes follow the options untouched
  assert.equal(body.length, 4 + size + image.length);
});
