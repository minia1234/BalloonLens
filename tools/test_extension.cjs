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
