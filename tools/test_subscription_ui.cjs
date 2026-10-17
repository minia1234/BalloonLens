const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const reading = require('../extension/reading.js');

function sandbox(storage = {}) {
  const listeners = {};
  const calls = [];
  const event = (name) => ({ addListener: (fn) => { listeners[name] = fn; } });
  const ctx = vm.createContext({
    chrome: {
      runtime: { onMessage: event('message'), onInstalled: event('installed') },
      commands: { onCommand: event('command') },
      tabs: { onUpdated: event('updated'), onRemoved: event('removed'), create: async (opts) => { calls.push({ tabs: opts }); } },
      storage: {
        local: {
          get: async (keys) => {
            const list = Array.isArray(keys) ? keys : [keys];
            return Object.fromEntries(list.filter((k) => k in storage).map((k) => [k, storage[k]]));
          },
          set: async (values) => { Object.assign(storage, values); },
          remove: async (key) => { delete storage[key]; },
        },
      },
    },
    fetch: async (url, opts = {}) => {
      calls.push({ url, method: opts.method, body: opts.body });
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    },
    AbortController, URLSearchParams, setTimeout, clearTimeout, performance, console,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../extension/sw.js'), 'utf8'), ctx);
  return { ctx, listeners, calls, storage };
}

test('subscription models are recognised and local ones are not', () => {
  assert.equal(reading.isSubscription('claude-haiku-5-5'), true);
  assert.equal(reading.isSubscription('gpt-6-luna'), false); // Codex is gone: its old name is not a subscription model
  assert.equal(reading.isSubscription('qwen3.5:9b'), false);
  assert.equal(reading.isSubscription(undefined), false);
});

test('backup import keeps subscription models and review modes, and maps the old review flag', () => {
  const r = reading.validateBackup({ app: 'BalloonLens', settings: {
    model: 'claude-haiku-5-5', reviewMode: 'selective', token: 'secret' } });
  assert.deepEqual(r.settings, { model: 'claude-haiku-5-5', reviewMode: 'selective' });
  assert.equal(reading.validateBackup({ app: 'BalloonLens', settings: { model: 'evil:1b' } }).settings.model, undefined);
  assert.equal(reading.validateBackup({ app: 'BalloonLens', settings: { reviewMode: 'bogus' } }).settings.reviewMode, undefined);
  assert.equal(reading.validateBackup({ app: 'BalloonLens', settings: { reviewAll: true } }).settings.reviewMode, 'all');
  assert.equal(reading.validateBackup({ app: 'BalloonLens', settings: { reviewAll: false } }).settings.reviewMode, null);
  assert.deepEqual(reading.validateBackup({ app: 'BalloonLens', settings: { reviewAll: true } }).skipped, []);
});

test('new error codes have Korean wording and unknown codes pass through', () => {
  assert.match(reading.explainCode('quota_paused'), /멈췄/);
  assert.equal(reading.explainCode('something_new'), 'something_new');
  assert.deepEqual(reading.stateLabel('ready'), ['준비됨', 'ok']);
  assert.deepEqual(reading.stateLabel('no_such_state'), ['no_such_state', 'warn']);
});

test('a new install starts on Claude Haiku and a returning reader keeps their model', async () => {
  const fresh = sandbox();
  await fresh.listeners.installed({ reason: 'install' });
  assert.equal(fresh.storage.model, 'claude-haiku-5-5');
  assert.equal(fresh.storage.schemaVersion, 2);
  assert.equal(fresh.calls[0].tabs.url, 'options.html#welcome');

  const existing = sandbox({ model: 'translategemma:4b', reviewAll: true });
  await existing.listeners.installed({ reason: 'update' });
  assert.equal(existing.storage.model, 'translategemma:4b');
  assert.equal(existing.storage.reviewMode, 'all');
  assert.equal('reviewAll' in existing.storage, false);

  const unset = sandbox({ reviewAll: false });
  await unset.listeners.installed({ reason: 'update' });
  assert.equal(unset.storage.model, 'qwen3.5:9b');
  assert.equal('reviewMode' in unset.storage, false);
});

test('helper start failures tell the reader which program to close or which setup window to open', () => {
  assert.match(reading.explainCode('helper_start_timeout'), /BalloonLens-Setup\.exe/);
  assert.match(reading.explainCode('helper_start_timeout'), /8787/);
  assert.match(reading.explainCode('native_missing'), /BalloonLens-Setup\.exe/);
});

test('account usage shows remaining percentages, zero and reset times without local call counts', () => {
  const usage = {state: 'ready', checked_at: 100, stale: false, windows: [
    {id: 'five_hour', remaining_percent: 89, resets_at: 2000},
    {id: 'seven_day', remaining_percent: 0, resets_at: 3000},
  ]};
  const text = reading.subscriptionUsageText(usage, () => '초기화 시각');
  assert.match(text, /5시간 남음 89%/);
  assert.match(text, /7일 남음 0%/);
  assert.match(text, /초기화 시각/);
  assert.doesNotMatch(text, /오늘|회|토큰/);
  assert.match(reading.subscriptionUsageText({...usage, stale: true}), /마지막 확인값/);
  assert.match(reading.subscriptionUsageText({state: 'loading', windows: []}), /확인 중/);
  assert.match(reading.subscriptionUsageText(null), /확인 불가/);
  assert.doesNotMatch(reading.subscriptionUsageText({state: 'unavailable', windows: []}), /100%/);
});
