// Service worker: pairing, starting the helper, privileged fetches, tab capture, and relaying to the helper.
// Holds no job state — content scripts keep job ids and poll, so a SW restart loses nothing (section 12.3).

const HELPER = "http://127.0.0.1:8787";
const NATIVE_HOST = "com.balloonlens.helper";
const ALL_URLS = { origins: ["<all_urls>"] };
const DEFAULTS = {
  src: "ja",
  tgt: "ko",
  model: "claude-haiku-5-5",
  reading: "rtl",
  fontScale: 1,
  bgOpacity: 0.92,
  showSource: false,
  autoSites: [],
  workProfiles: [],
  vision: false,
  reviewMode: null,
};
const MAX_IMAGE_BYTES = 25 * 1024 * 1024;
const CAPTURE_MIN_INTERVAL_MS = 600; // captureVisibleTab is limited to ~2 calls/s
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function settings() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
}

// ---------- helper connection ----------
async function rawFetch(path, { method = "GET", body, json, headers = {}, timeoutMs = 10000, token } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const h = { ...headers };
  if (token) h["X-BalloonLens-Token"] = token;
  if (json !== undefined) {
    body = JSON.stringify(json);
    h["Content-Type"] = "application/json";
  }
  try {
    const res = await fetch(HELPER + path, { method, body, headers: h, signal: ctrl.signal });
    return { res, data: await res.json().catch(() => ({})) };
  } catch (e) {
    throw new Error(e.name === "AbortError" ? "helper_timeout" : "helper_unreachable");
  } finally {
    clearTimeout(timer);
  }
}

const errorOf = (res, data) => (typeof data?.detail === "string" ? data.detail : data?.error || `http_${res.status}`);

let pairing = null;
function pair() {
  // The helper hands its token only to this extension's origin, so there's nothing to paste.
  pairing ??= (async () => {
    const { res, data } = await rawFetch("/v1/pair", { method: "POST", timeoutMs: 3000 });
    if (!res.ok || !data.token) throw new Error(errorOf(res, data));
    await chrome.storage.local.set({ token: data.token });
    return data.token;
  })().finally(() => (pairing = null));
  return pairing;
}

async function helper(path, opts = {}) {
  let { token } = await chrome.storage.local.get("token");
  if (!token) token = await pair();
  let { res, data } = await rawFetch(path, { ...opts, token });
  if (res.status === 401) {
    token = await pair(); // helper data was reset -> new token
    ({ res, data } = await rawFetch(path, { ...opts, token }));
  }
  if (!res.ok) throw new Error(errorOf(res, data));
  return data;
}

async function nativeCall(cmd) {
  try {
    const out = await chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd });
    if (!out?.ok) throw new Error(out?.error || "native_error");
    return out;
  } catch (e) {
    throw new Error(/not found|forbidden|not registered/i.test(e.message) ? "native_missing" : e.message);
  }
}

let starting = null;
async function ensureHelper() {
  try {
    // Right after the helper starts it can be slow (OCR warm-up, Ollama probe), so give it more than 2.5 s.
    return await helper("/v1/status", { timeoutMs: 8000 });
  } catch (e) {
    if (e.message !== "helper_unreachable") throw e;
  }
  starting ??= (async () => {
    await nativeCall("start");
    const until = Date.now() + 45000;
    while (Date.now() < until) {
      await sleep(700);
      try {
        return await helper("/v1/status", { timeoutMs: 1500 });
      } catch (e) {
        if (e.message !== "helper_unreachable") throw e;
      }
    }
    throw new Error("helper_start_timeout");
  })().finally(() => (starting = null));
  return starting;
}

// ---------- images ----------
async function submitJob(blob, opts) {
  const q = new URLSearchParams({
    src: opts.src,
    tgt: opts.tgt,
    model: opts.model,
    reading: opts.reading,
    priority: String(opts.priority ?? 2),
    session: opts.session,
    scope: opts.scope || '',
  });
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = '';
  for (let i=0; i<bytes.length; i+=32768) binary += String.fromCharCode(...bytes.subarray(i,i+32768));
  return helper(`/v1/jobs?${q}`, {
    method: "POST",
    json: {image: btoa(binary), options: opts.options || {}},
    timeoutMs: 30000,
  });
}

// Images downloaded ahead while earlier pages translate. Lost on worker restart, which only costs a refetch.
const prefetched = new Map();
const PREFETCH_MAX = 4;
const PREFETCH_TTL_MS = 60000;

function prefetchImage(url) {
  if (prefetched.has(url)) return;
  while (prefetched.size >= PREFETCH_MAX) prefetched.delete(prefetched.keys().next().value);
  const promise = fetchImage(url);
  promise.catch(() => {});
  prefetched.set(url, { at: Date.now(), promise });
}

async function takeImage(url) {
  const hit = prefetched.get(url);
  prefetched.delete(url);
  if (hit && Date.now() - hit.at < PREFETCH_TTL_MS) {
    try {
      return await hit.promise;
    } catch {
      // fall through to a fresh attempt
    }
  }
  return fetchImage(url);
}

async function fetchImage(url) {
  if (!/^(https?|data|blob|chrome-extension):/.test(url)) throw new Error("unsupported_url");
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 15000);
  try {
    const res = await fetch(url, { credentials: "include", signal: ctrl.signal });
    if (!res.ok) throw new Error(`image_http_${res.status}`);
    const blob = await res.blob();
    if (!blob.type.startsWith("image/")) throw new Error("not_image");
    if (blob.size > MAX_IMAGE_BYTES) throw new Error("image_too_large");
    return blob;
  } catch (e) {
    if (e.name === "AbortError") throw new Error("image_timeout");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

let captureChain = Promise.resolve();
let lastCapture = 0;
function captureVisible(tab) {
  const run = async () => {
    const wait = lastCapture + CAPTURE_MIN_INTERVAL_MS - Date.now();
    if (wait > 0) await sleep(wait);
    const isActive = async () => (await chrome.tabs.query({ active: true, windowId: tab.windowId }))[0]?.id === tab.id;
    if (!(await isActive())) throw new Error("tab_not_active");
    lastCapture = Date.now();
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    if (!(await isActive())) throw new Error("tab_not_active");
    return dataUrl;
  };
  const p = captureChain.then(run, run);
  captureChain = p.catch(() => {});
  return p;
}

// rect is in CSS px of the viewport; viewport is {w, h} CSS px at capture time.
async function captureRegion(tab, rect, viewport) {
  const dataUrl = await captureVisible(tab);
  const bitmap = await createImageBitmap(await (await fetch(dataUrl)).blob());
  const scale = bitmap.width / viewport.w; // covers devicePixelRatio and browser zoom together
  const sx = Math.max(0, Math.round(rect.x * scale));
  const sy = Math.max(0, Math.round(rect.y * scale));
  const sw = Math.min(bitmap.width - sx, Math.round(rect.w * scale));
  const sh = Math.min(bitmap.height - sy, Math.round(rect.h * scale));
  if (sw < 8 || sh < 8) throw new Error("region_too_small");
  const canvas = new OffscreenCanvas(sw, sh);
  canvas.getContext("2d").drawImage(bitmap, sx, sy, sw, sh, 0, 0, sw, sh);
  bitmap.close();
  return { blob: await canvas.convertToBlob({ type: "image/png" }), scale };
}

async function sendToTab(tabId, type) {
  // content.js guards against double injection, so injecting every time is safe.
  await chrome.scripting.executeScript({ target: { tabId }, files: ["reading.js", "editor.js", "content.js"] });
  return chrome.tabs.sendMessage(tabId, { type, tabId, settings: await settings() });
}

const BADGE = {
  busy: { text: "…", color: "#b7791f" },
  done: { text: "ON", color: "#16a34a" },
  error: { text: "!", color: "#dc2626" },
  off: { text: "" },
};

const handlers = {
  // ---- popup / options ----
  async setup({ checkNative } = {}) {
    const out = { native: false, helper: null, error: null, permission: await chrome.permissions.contains(ALL_URLS) };
    try {
      out.helper = await helper("/v1/status", { timeoutMs: 2500 });
    } catch (e) {
      out.error = e.message;
    }
    if (!out.helper || checkNative) {
      try {
        out.native = !!(await nativeCall("ping"));
      } catch (e) {
        out.nativeError = e.message;
      }
    }
    return out;
  },
  async status() {
    return helper("/v1/status", { timeoutMs: 3000 });
  },
  async startHelper() {
    return ensureHelper();
  },
  async repair() {
    await chrome.storage.local.remove("token");
    await pair();
    return helper("/v1/status", { timeoutMs: 3000 });
  },
  async pullModel({ name }) {
    return helper(`/v1/models/pull?name=${encodeURIComponent(name)}`, { method: "POST" });
  },
  async pullStatus({ name }) {
    return helper(`/v1/models/pull?name=${encodeURIComponent(name)}`);
  },
  async start({ tabId }) {
    await ensureHelper();
    return sendToTab(tabId, "bl:start");
  },
  async stop({ tabId }) {
    return chrome.tabs.sendMessage(tabId, { type: "bl:stop" }).catch(() => null);
  },
  async region({ tabId }) {
    await ensureHelper();
    return sendToTab(tabId, "bl:region");
  },
  async peek({ tabId, on }) {
    return chrome.tabs.sendMessage(tabId, { type: "bl:peek", on }).catch(() => null);
  },
  async tabState({ tabId }) {
    return chrome.tabs.sendMessage(tabId, { type: "bl:ping" }).catch(() => null);
  },
  async retryFailed({tabId}) {
    await ensureHelper();
    return chrome.tabs.sendMessage(tabId, {type:'bl:retry-failed'});
  },
  async nextIssue({tabId}) {
    return chrome.tabs.sendMessage(tabId, {type:'bl:next-issue'});
  },
  async openOptions() {
    return chrome.runtime.openOptionsPage();
  },

  // ---- from content scripts ----
  async translateImage({ url, opts }) {
    const t0 = performance.now();
    // "fetch:" prefix tells the content script to fall back to screen capture (section 7.1).
    const blob = await takeImage(url).catch((e) => {
      throw new Error(`fetch:${e.message}`);
    });
    const fetchMs = Math.round(performance.now() - t0);
    const { id } = await submitJob(blob, opts);
    return { jobId: id, fetchMs };
  },
  async translateRegion({ rect, viewport, opts }, sender) {
    const { blob, scale } = await captureRegion(sender.tab, rect, viewport);
    const { id } = await submitJob(blob, opts);
    return { jobId: id, scale };
  },
  async getJob({ jobId }) {
    return helper(`/v1/jobs/${encodeURIComponent(jobId)}`, { timeoutMs: 5000 });
  },
  async cancelJob({ jobId }) {
    return helper(`/v1/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
  },
  async setPriority({ jobId, value }) {
    return helper(`/v1/jobs/${encodeURIComponent(jobId)}/priority?value=${value}`, { method: "POST" });
  },
  async cancelSession({ session }) {
    return helper(`/v1/sessions/${encodeURIComponent(session)}/cancel`, { method: "POST" });
  },
  async translateLine({ text, src, tgt, model, scope, fresh, options, page }) {
    return helper("/v1/translate-line", { method: "POST", json: { text, src, tgt, model, scope, fresh, options: options || {}, page: page || [] }, timeoutMs: 270000 });
  },
  async saveEdit({ image_hash, src, tgt, bbox, src_text, tgt_text, clear, scope }) {
    return helper("/v1/edits", { method: "PUT", json: { image_hash, src, tgt, bbox, src_text, tgt_text, clear: !!clear, scope: scope || "" } });
  },
  // Open the dialogue list (turning translation on first if needed).
  async script({ tabId, open }) {
    const state = await handlers.tabState({ tabId });
    if (!state?.mounted) await handlers.start({ tabId });
    return chrome.tabs.sendMessage(tabId, { type: "bl:script", open: state?.mounted ? open : true });
  },
  async editRegions({ tabId }) {
    return chrome.tabs.sendMessage(tabId, { type: "bl:edit" }).catch(() => null);
  },
  async prefetchImage({ url }) {
    prefetchImage(url);
    return true;
  },
  async saveLayout({ image_hash, src, layout }) {
    const path = `/v1/layouts/${encodeURIComponent(image_hash)}?src=${encodeURIComponent(src)}`;
    return layout ? helper(path, { method: "PUT", json: layout }) : helper(path, { method: "DELETE" });
  },
  async openWorkSettings({ url }) {
    return chrome.tabs.create({ url: `${chrome.runtime.getURL("options.html")}?workUrl=${encodeURIComponent(url || "")}` });
  },

  // ---- work memory (options page) ----
  async getWork({ scope }) {
    return helper(`/v1/works?scope=${encodeURIComponent(scope)}`);
  },
  async saveWork({ scope, work }) {
    return helper(`/v1/works?scope=${encodeURIComponent(scope)}`, { method: "PUT", json: work });
  },
  async deleteWork({ scope }) {
    return helper(`/v1/works?scope=${encodeURIComponent(scope)}`, { method: "DELETE" });
  },
  async clearWorkPages({ scope }) {
    return helper(`/v1/works/pages?scope=${encodeURIComponent(scope)}`, { method: "DELETE" });
  },
  async suggestWork({ scope }) {
    const { model } = await settings();
    const usable = /^(qwen3|claude-|gpt-)/.test(model); // anything else is refused by the helper
    const q = new URLSearchParams({ scope, model: usable ? model : "qwen3.5:9b" });
    return helper(`/v1/works/suggest?${q}`, { method: "POST", timeoutMs: 180000 });
  },
  // ---- subscription backends (v0.5.0): the helper keeps limits and CLI state ----
  async backends() {
    return helper("/v1/backends", { timeoutMs: 3000 });
  },
  async checkBackend({ name }) {
    return helper(`/v1/backends/${encodeURIComponent(name)}/check`, { method: "POST", timeoutMs: 90000 });
  },
  async resumeBackend({ name }) {
    return helper(`/v1/backends/${encodeURIComponent(name)}/resume`, { method: "POST" });
  },
  async setCaps({ name, per_5h, per_7d }) {
    return helper(`/v1/backends/${encodeURIComponent(name)}/caps`, { method: "PUT", json: { per_5h, per_7d } });
  },
  async calibrate({ backend, percent }) {
    return helper("/v1/usage/calibration", { method: "PUT", json: { backend, percent } });
  },
  async progress({ state }, sender) {
    const tabId = sender.tab?.id;
    if (tabId == null) return;
    const b = BADGE[state] || BADGE.off;
    await chrome.action.setBadgeText({ tabId, text: b.text });
    if (b.color) await chrome.action.setBadgeBackgroundColor({ tabId, color: b.color });
  },
};

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const fn = handlers[msg?.type];
  if (!fn) return false;
  fn(msg, sender).then(
    (data) => sendResponse({ ok: true, data }),
    (err) => sendResponse({ ok: false, error: String(err?.message || err) })
  );
  return true;
});

chrome.runtime.onInstalled.addListener(async ({ reason }) => {
  if (reason === "install") {
    await chrome.storage.local.set({ model: DEFAULTS.model, schemaVersion: 2 });
    chrome.tabs.create({ url: "options.html#welcome" });
  } else if (reason === "update") {
    // Readers who already use the helper keep their model; the old yes/no review flag changes shape.
    const { model, reviewAll } = await chrome.storage.local.get(["model", "reviewAll"]);
    const next = { schemaVersion: 2 };
    if (!model) next.model = "qwen3.5:9b";
    if (reviewAll === true) next.reviewMode = "all";
    await chrome.storage.local.set(next);
    await chrome.storage.local.remove("reviewAll");
  }
});

chrome.commands.onCommand.addListener(async (command, tab) => {
  if (!tab?.id) return;
  if (!(await chrome.permissions.contains(ALL_URLS))) return chrome.runtime.openOptionsPage(); // prompt needs extension UI
  const state = await handlers.tabState({ tabId: tab.id });
  if (command === "region-translate") return handlers.region({ tabId: tab.id }).catch(() => {});
  if (command === "peek-original") return handlers.peek({ tabId: tab.id, on: !state?.peek });
  if (command === "toggle-script") return handlers.script({ tabId: tab.id }).catch(() => {});
  return state?.running ? handlers.stop({ tabId: tab.id }) : handlers.start({ tabId: tab.id }).catch(() => {});
});

// Sites the reader opted into: turn translation on when a page finishes loading (FR-18).
chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  if (info.status !== "complete" || !/^https?:/.test(tab.url || "")) return;
  const { autoSites = [] } = await chrome.storage.local.get("autoSites");
  if (!autoSites.includes(new URL(tab.url).hostname)) return;
  if (!(await chrome.permissions.contains(ALL_URLS))) return;
  handlers.start({ tabId }).catch(() => {});
});

// Tab closed -> cancel that tab's remaining work.
chrome.tabs.onRemoved.addListener((tabId) => {
  helper(`/v1/sessions/tab-${tabId}/cancel`, { method: "POST" }).catch(() => {});
});
