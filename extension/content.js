// BalloonLens content script: finds comic images, submits them, draws translations on top,
// and lets the reader check or fix a line in place. Injected on demand (popup / shortcut / auto site).
// Positions are fractions of an anchor box, so scroll and zoom only re-place layers (section 10.2).
(() => {
  if (window.__balloonlensLoaded) return;
  window.__balloonlensLoaded = true;

  const MIN_NATURAL = 250;
  const MIN_DISPLAY = 150;
  const POLL_MS = 500;
  const MAX_SUBMITS = 3;
  const FINISHED = new Set(["done", "partial", "failed", "cancelled"]);
  const MODEL_NAMES = { "translategemma:4b": "TranslateGemma", "qwen3:8b": "Qwen3", "qwen3.5:9b": "Qwen3.5", "claude-haiku-5-5": "Claude", "gpt-6-luna": "Codex" };
  const explain = (code) => BalloonLensReading.explainCode(code);
  const svg = (d) =>
    `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
  const ICON = {
    eye: svg('<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>'),
    region: svg('<path d="M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5a1 1 0 0 1-1-1v-4"/>'),
    list: svg('<path d="M8 6h12M8 12h12M8 18h8"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/>'),
    edit: svg('<rect x="4" y="6" width="9" height="12" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1" stroke-dasharray="2 2"/><path d="M16 15l4 4"/>'),
    close: svg('<path d="M6 6l12 12M18 6 6 18"/>'),
  };

  let settings = null;
  let session = "";
  let mounted = false; // UI present (auto mode or region results)
  let auto = false; // scanning page images
  let mountedAt = 0;
  let peekOn = false;
  let gen = 0; // bumps on stop / navigation / language change -> late results are ignored
  let loopId = 0;
  let helperDown = 0;
  let downToastShown = false;
  let lastError = "";
  let lastScroll = 0;
  let submitting = 0;
  let captureBusy = false;
  let idleSince = 0;
  let lastProgress = "";
  let popover = null; // { e, sid, pop }
  let editor = null; // BalloonLensEditor instance while the region editor is open
  let retryGeneration = null;
  let lastIssueElement = null;
  let href = location.href;
  const waiting = [];
  const images = new Map(); // img -> { key, main, captures, needsCapture, covered }
  const entries = new Set(); // render entries (one per job)

  // ---------- UI shell (closed shadow root so page CSS can't touch it) ----------
  const host = document.createElement("balloonlens-root");
  host.style.cssText = "all:initial;position:fixed;inset:0;pointer-events:none;z-index:2147483646;";
  const shadow = host.attachShadow({ mode: "closed" });
  shadow.innerHTML = `<style>
    :host { --blue: #2563eb; }
    [hidden] { display: none !important; } /* "all: unset" on buttons would otherwise undo the hidden attribute */
    .wrap { position: fixed; inset: 0; pointer-events: none; font: 13px/1.45 system-ui, "Segoe UI", "Malgun Gothic", sans-serif; }
    .layers { position: fixed; inset: 0; overflow: hidden; }
    .wrap.peek .layers { visibility: hidden; }
    .layer { position: absolute; left: 0; top: 0; }
    .cleanup { position: absolute; pointer-events: none; z-index: 0; }
    .seg { position: absolute; box-sizing: border-box; display: flex; flex-direction: column; align-items: center;
      justify-content: center; text-align: center; padding: 2px 5px; border-radius: 10px; color: #111;
      font-family: "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans KR", "Noto Sans CJK JP", sans-serif;
      font-weight: 500; line-height: 1.3; word-break: keep-all; overflow-wrap: anywhere; overflow: hidden; z-index: 1;
      transition: box-shadow .12s; }
    .seg.framed { padding: 0; } /* the helper's frame is already inset from the bubble outline */
    .seg.pending { outline: 2px dashed rgba(37, 99, 235, .4); animation: breathe 1.4s ease-in-out infinite; }
    @keyframes breathe { 50% { outline-color: rgba(37, 99, 235, .12); } }
    .seg.review { box-shadow: 0 0 0 2px #e0a100; }
    .seg.failed { outline: 2px dashed rgba(220, 38, 38, .65); }
    .seg.clickable { pointer-events: auto; cursor: pointer; }
    .seg.clickable:hover, .seg.active { box-shadow: 0 0 0 2px var(--blue), 0 2px 10px rgba(37, 99, 235, .25); }
    .seg.edited::after { content: ""; position: absolute; top: 4px; right: 4px; width: 6px; height: 6px; border-radius: 50%; background: var(--blue); }
    .seg.user-hidden { background: transparent !important; outline: 1.5px dotted rgba(37, 99, 235, .75); }
    .seg.user-hidden > * { visibility: hidden; }
    .seg.overflow::before { content: '…'; position:absolute; right:3px; bottom:0; background:#fff; font-size:12px; }
    .seg .src { font-size: .6em; color: #666; font-weight: 400; margin-top: 2px; }

    .pill { position: fixed; right: 14px; bottom: 14px; pointer-events: auto; display: flex; gap: 8px; align-items: center;
      background: rgba(22, 23, 28, .9); color: #f1f2f6; padding: 5px 5px 5px 12px; border-radius: 999px;
      box-shadow: 0 6px 20px rgba(0, 0, 0, .28); backdrop-filter: blur(6px); user-select: none; }
    .pill .dot { width: 8px; height: 8px; border-radius: 50%; background: #3ecf74; flex: none; }
    .pill .dot.busy { background: #f5b83d; animation: breathe2 1s ease-in-out infinite; }
    .pill .dot.err { background: #f26b6b; }
    @keyframes breathe2 { 50% { opacity: .35; } }
    .pill .text { display: flex; flex-direction: column; min-width: 92px; }
    .pill .label { font-size: 12px; font-weight: 600; white-space: nowrap; }
    .pill .prog { height: 3px; margin-top: 3px; background: rgba(255, 255, 255, .18); border-radius: 2px; overflow: hidden; }
    .pill .prog i { display: block; height: 100%; width: 0; background: #7aa7ff; transition: width .35s; }
    .pill .btns { display: flex; gap: 2px; }
    .icon-btn { all: unset; width: 30px; height: 30px; display: grid; place-items: center; border-radius: 50%; cursor: pointer; color: #dfe2ea; }
    .icon-btn:hover { background: rgba(255, 255, 255, .14); color: #fff; }
    .icon-btn[aria-pressed="true"] { background: var(--blue); color: #fff; }
    .icon-btn:focus-visible { outline: 2px solid #7aa7ff; }
    .pill.compact { padding: 5px 12px; }
    .pill.compact .btns, .pill.compact .prog { display: none; }
    .pill.compact:hover { padding: 5px 5px 5px 12px; }
    .pill.compact:hover .btns { display: flex; }

    .toast { position: fixed; right: 14px; bottom: 62px; max-width: 340px; pointer-events: auto; display: flex; gap: 12px;
      align-items: center; background: #16171c; color: #eef0f5; border-radius: 12px; padding: 10px 14px;
      box-shadow: 0 10px 28px rgba(0, 0, 0, .3); animation: rise .18s ease-out; }
    .toast button { all: unset; cursor: pointer; color: #8fb4ff; font-weight: 600; white-space: nowrap; }
    @keyframes rise { from { transform: translateY(6px); opacity: 0; } }

    .popover { position: fixed; width: 320px; box-sizing: border-box; pointer-events: auto; background: #fff; color: #17181c;
      border-radius: 14px; padding: 12px 14px 12px; box-shadow: 0 16px 40px rgba(0, 0, 0, .28), 0 0 0 1px rgba(0, 0, 0, .06);
      animation: rise .15s ease-out; }
    .popover .head { display: flex; justify-content: space-between; align-items: center; }
    .popover .head b { font-size: 13px; }
    .popover .x { all: unset; width: 26px; height: 26px; display: grid; place-items: center; border-radius: 7px; color: #6b6f7a; cursor: pointer; }
    .popover .x:hover { background: #f0f2f5; }
    .popover label { display: flex; justify-content: space-between; font-size: 11px; font-weight: 600; color: #6b6f7a; margin: 8px 0 4px; }
    .popover .tag { font-weight: 500; color: var(--blue); }
    .popover .tag.warn { color: #b7791f; }
    .popover textarea { width: 100%; box-sizing: border-box; resize: vertical; min-height: 46px; font: inherit; font-size: 14px;
      color: inherit; background: #f5f6f8; border: 1px solid #e1e4ea; border-radius: 9px; padding: 7px 9px; }
    .popover textarea:focus { outline: 2px solid var(--blue); outline-offset: -1px; background: #fff; }
    .popover .actions { display: flex; gap: 6px; margin-top: 10px; }
    .popover .btn { all: unset; cursor: pointer; padding: 6px 10px; border-radius: 8px; border: 1px solid #e1e4ea; font-size: 12px; font-weight: 500; white-space: nowrap; }
    .popover .btn:hover { background: #f0f2f5; }
    .popover .btn.primary { margin-left: auto; background: var(--blue); border-color: var(--blue); color: #fff; font-weight: 600; }
    .popover .btn.primary:hover { filter: brightness(1.07); }
    .popover.busy .btn { opacity: .5; pointer-events: none; }
    .popover .links { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 9px; font-size: 12px; }
    .popover .links button { all: unset; cursor: pointer; color: #6b6f7a; white-space: nowrap; }
    .popover .links button:hover { color: var(--blue); }
    .popover .foot { margin-top: 8px; font-size: 11px; color: #8a8f99; }
    @media (prefers-color-scheme: dark) {
      .popover { background: #1c1d22; color: #eceef2; box-shadow: 0 16px 40px rgba(0, 0, 0, .5), 0 0 0 1px rgba(255, 255, 255, .08); }
      .popover textarea { background: #25272e; border-color: #33363f; }
      .popover textarea:focus { background: #1c1d22; }
      .popover .btn { border-color: #33363f; }
      .popover .btn:hover, .popover .x:hover { background: #2a2c34; }
    }

    .selector { position: fixed; inset: 0; pointer-events: auto; cursor: crosshair; background: rgba(10, 12, 20, .28); }
    .selector .box { position: absolute; border: 2px solid #4f8cff; border-radius: 4px; background: rgba(79, 140, 255, .12);
      box-shadow: 0 0 0 9999px rgba(10, 12, 20, .25); }
    .selector .hint { position: fixed; top: 16px; left: 50%; transform: translateX(-50%); background: #16171c; color: #fff;
      padding: 8px 14px; border-radius: 10px; font-size: 13px; box-shadow: 0 6px 20px rgba(0, 0, 0, .3); }
    .selector .hint kbd { font: 11px ui-monospace, Consolas, monospace; background: #2d2f37; padding: 1px 5px; border-radius: 4px; }

    .popover .term { display: flex; gap: 6px; margin-top: 8px; }
    .popover .term input { flex: 1; min-width: 0; font: inherit; font-size: 12px; color: inherit; background: #f5f6f8;
      border: 1px solid #e1e4ea; border-radius: 7px; padding: 5px 7px; }
    @media (prefers-color-scheme: dark) { .popover .term input { background: #25272e; border-color: #33363f; } }

    /* region editor (editor.js) */
    .layer.editing > .seg, .layer.editing > .cleanup { visibility: hidden; }
    .ed-board { position: absolute; inset: 0; pointer-events: auto; touch-action: none; background: rgba(15, 23, 42, .14); }
    .ed-board.adding { cursor: crosshair; }
    .ed-box { position: absolute; box-sizing: border-box; border-radius: 4px; cursor: move; }
    .ed-text { border: 2px solid #f97316; background: rgba(249, 115, 22, .12); color: #f97316; }
    .ed-frame { border: 2px solid #2563eb; background: rgba(37, 99, 235, .12); color: #2563eb; }
    .ed-frame.auto { border-style: dashed; }
    .ed-box.faint { opacity: .35; pointer-events: none; }
    .ed-box.faint .tag { display: none; } /* only the boxes being edited are labelled */

    /* dialogue list panel */
    .script { position: fixed; top: 12px; right: 12px; bottom: 68px; width: min(360px, calc(100vw - 24px)); z-index: 2;
      box-sizing: border-box; pointer-events: auto; display: flex; flex-direction: column; gap: 8px; padding: 12px;
      background: #fff; color: #17181c; border-radius: 14px; animation: rise .15s ease-out;
      box-shadow: 0 16px 40px rgba(0, 0, 0, .28), 0 0 0 1px rgba(0, 0, 0, .06); }
    .script-head { display: flex; align-items: center; gap: 8px; }
    .script-head b { font-size: 14px; }
    .script-head .muted { margin-right: auto; font-size: 12px; color: #6b6f7a; }
    .script .x { all: unset; width: 26px; height: 26px; display: grid; place-items: center; border-radius: 7px; color: #6b6f7a; cursor: pointer; }
    .script .x:hover { background: #f0f2f5; }
    .script input[type="search"] { font: inherit; font-size: 13px; color: inherit; background: #f5f6f8;
      border: 1px solid #e1e4ea; border-radius: 8px; padding: 6px 9px; }
    .script input[type="search"]:focus { outline: 2px solid var(--blue); outline-offset: -1px; background: #fff; }
    .chips { display: flex; gap: 4px; }
    .chips button { all: unset; cursor: pointer; font-size: 12px; padding: 4px 10px; border-radius: 999px; background: #eef0f4; color: #4b5060; }
    .chips button[aria-checked="true"] { background: var(--blue); color: #fff; }
    .script-list { flex: 1; min-height: 0; overflow: auto; display: flex; flex-direction: column; gap: 2px; margin: 0 -4px; padding: 0 4px; }
    .script-group { margin: 8px 2px 2px; font-size: 11px; font-weight: 700; color: #8a8f99; }
    .script-item { all: unset; cursor: pointer; display: flex; flex-direction: column; gap: 2px; padding: 7px 9px; border-radius: 9px; }
    .script-item:hover, .script-item:focus-visible { background: #f0f3fa; }
    .script-item.active { background: #e7eeff; }
    .script-item .tgt { font-size: 14px; font-weight: 500; white-space: pre-wrap; }
    .script-item .src { font-size: 12px; color: #6b6f7a; white-space: pre-wrap; }
    .script-item .state { align-self: flex-start; margin-top: 2px; padding: 1px 7px; border-radius: 999px; font-size: 11px; font-weight: 600;
      background: #fdf1d8; color: #9a6400; }
    .script-item .state.edited { background: #e7eeff; color: var(--blue); }
    .script-empty { margin: 16px 4px; font-size: 13px; color: #8a8f99; }
    .script-foot { display: flex; gap: 6px; }
    .script-foot .btn { all: unset; cursor: pointer; padding: 6px 10px; border-radius: 8px; border: 1px solid #e1e4ea; font-size: 12px; font-weight: 500; }
    .script-foot .btn:hover { background: #f0f2f5; }
    @media (prefers-color-scheme: dark) {
      .script { background: #1c1d22; color: #eceef2; box-shadow: 0 16px 40px rgba(0, 0, 0, .5), 0 0 0 1px rgba(255, 255, 255, .08); }
      .script input[type="search"] { background: #25272e; border-color: #33363f; }
      .script input[type="search"]:focus { background: #1c1d22; }
      .chips button { background: #2a2c34; color: #b9bdc8; }
      .script-item:hover, .script-item:focus-visible, .script .x:hover, .script-foot .btn:hover { background: #2a2c34; }
      .script-item.active { background: #1f2a44; }
      .script-foot .btn { border-color: #33363f; }
      .script-item .state { background: #3a2e17; color: #f0b95a; }
      .script-item .state.edited { background: #1f2a44; color: #8fb4ff; }
    }
    .ed-box.sel { box-shadow: 0 0 0 2px #fff, 0 0 0 4px currentColor; }
    .ed-box .tag { position: absolute; left: -2px; bottom: 100%; margin-bottom: 2px; max-width: 180px; overflow: hidden;
      text-overflow: ellipsis; white-space: nowrap; padding: 0 6px; border-radius: 4px; color: #fff; pointer-events: none;
      font: 600 11px/18px system-ui, "Malgun Gothic", sans-serif; }
    .ed-text .tag { background: #ea580c; } .ed-frame .tag { background: #1d4ed8; }
    .ed-h { position: absolute; width: 12px; height: 12px; box-sizing: border-box; background: #fff; border: 2px solid currentColor; border-radius: 3px; }
    .ed-ghost { position: absolute; box-sizing: border-box; border: 2px dashed #f97316; background: rgba(249, 115, 22, .16); }
    .ed-bar { position: fixed; top: 12px; left: 50%; transform: translateX(-50%); z-index: 3; pointer-events: auto;
      width: max-content; max-width: calc(100vw - 24px); display: flex; flex-direction: column; gap: 8px;
      background: #16171c; color: #eef0f5; border-radius: 12px; padding: 10px 12px; box-shadow: 0 12px 32px rgba(0, 0, 0, .35); }
    .ed-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
    .ed-row.ed-end { justify-content: flex-end; }
    .ed-hint { color: #a3a8b5; font-size: 12px; margin-right: auto; }
    .ed-row b + .ed-hint { margin-left: 8px; }
    .ed-bar button { all: unset; cursor: pointer; padding: 5px 10px; border-radius: 7px; background: rgba(255, 255, 255, .1); font-size: 12px; white-space: nowrap; }
    .ed-bar button:hover { background: rgba(255, 255, 255, .2); }
    .ed-bar button:focus-visible { outline: 2px solid #7aa7ff; }
    .ed-bar button[disabled] { opacity: .35; pointer-events: none; }
    .ed-bar button.primary { background: #2563eb; color: #fff; font-weight: 600; }
    .ed-bar button[aria-pressed="true"], .ed-seg button[aria-checked="true"] { background: #2563eb; color: #fff; }
    .ed-seg { display: inline-flex; gap: 2px; padding: 2px; border-radius: 9px; background: rgba(255, 255, 255, .06); }
  </style>
  <div class="wrap"><div class="layers"></div>
    <div class="pill">
      <span class="dot"></span>
      <div class="text"><span class="label">말풍선 렌즈</span><div class="prog"><i></i></div></div>
      <div class="btns">
        <button class="icon-btn" data-act="peek" aria-pressed="false" title="원문 보기 (Alt+Shift+O)">${ICON.eye}</button>
        <button class="icon-btn" data-act="region" title="영역 번역 (Alt+Shift+R)">${ICON.region}</button>
        <button class="icon-btn" data-act="script" title="대사 목록 (Alt+Shift+L)">${ICON.list}</button>
        <button class="icon-btn" data-act="edit" title="화면의 이미지 영역 편집 (글자·번역 칸 위치와 크기)">${ICON.edit}</button>
        <button class="icon-btn" data-act="stop" title="번역 끄기 (Alt+Shift+T)">${ICON.close}</button>
      </div>
    </div>
  </div>`;
  const wrap = shadow.querySelector(".wrap");
  const layersEl = shadow.querySelector(".layers");
  const pill = shadow.querySelector(".pill");
  const pillDot = shadow.querySelector(".dot");
  const pillLabel = shadow.querySelector(".label");
  const pillBar = shadow.querySelector(".prog i");
  const peekBtn = shadow.querySelector('[data-act="peek"]');

  // Our UI must not trigger the page's own handlers (e.g. a viewer's click-to-next-page).
  for (const type of ["pointerdown", "pointerup", "mousedown", "mouseup", "click", "dblclick", "keydown", "keyup", "keypress"]) {
    wrap.addEventListener(type, (ev) => ev.stopPropagation());
  }
  pill.addEventListener("click", (ev) => {
    const act = ev.target.closest("[data-act]")?.dataset.act;
    if (act === "peek") setPeek(!peekOn);
    if (act === "region") startRegionSelect();
    if (act === "edit") editVisibleImage();
    if (act === "script") toggleScript();
    if (act === "stop") stop();
  });

  // ---------- messaging ----------
  async function send(type, payload = {}) {
    let res;
    try {
      res = await chrome.runtime.sendMessage({ type, ...payload });
    } catch {
      throw new Error("extension_reloaded");
    }
    if (!res?.ok) throw new Error(res?.error || "no_response");
    return res.data;
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const nextFrames = (n) =>
    new Promise((r) => {
      const step = () => (--n <= 0 ? r() : requestAnimationFrame(step));
      requestAnimationFrame(step);
    });

  function jobOpts(priority, entry) {
    const { src, tgt, model, reading } = settings;
    const work = BalloonLensReading.workSettings(location.href, settings.workProfiles);
    const context = [];
    const target = entry?.anchor?.el;
    if (target) {
      for (const img of document.querySelectorAll('img')) {
        if (img === target) break;
        const snap = images.get(img)?.main?.lastSnap;
        if (!snap || !FINISHED.has(snap.status)) continue;
        for (const base of snap.segments) {
          const s = segView(images.get(img).main, base);
          if (s.status === 'done') context.push({source:s.src.slice(0,500), translation:(s.tgt || '').slice(0,500)});
        }
      }
    }
    const page = location.href.split('#')[0].slice(0, 1000);
    const position = target ? Math.max(0, [...document.querySelectorAll('img')].indexOf(target)) : 0;
    return { src, tgt, model, reading, priority, session, scope: work.scope,
      options: {instructions:work.instructions || '', glossary:work.glossary || '', context:context.slice(-12), vision:!!settings.vision,
        // Cross-page memory only for a work the reader registered, never guessed from the address.
        work: {page_url: page, position: Math.min(position, 10000), memory: !!work.matched},
        review: settings.reviewMode ?? null} };
  }

  // ---------- lifecycle ----------
  function mount(msg) {
    settings = msg.settings;
    session = `tab-${msg.tabId}`;
    if (mounted) return;
    mounted = true;
    mountedAt = Date.now();
    document.documentElement.appendChild(host);
    window.addEventListener("scroll", onScroll, { capture: true, passive: true });
    window.addEventListener("resize", onScroll, { passive: true });
    window.addEventListener("pointerdown", onOutsidePointer, true);
    document.addEventListener("load", onLoadCapture, true);
    pollLoop(++loopId);
    tick();
  }

  function startAuto() {
    auto = true;
    mutationObs.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["src", "srcset"] });
    document.querySelectorAll("img").forEach((img) => nearObs.observe(img));
  }

  function clearAll() {
    editor?.close();
    gen++;
    waiting.length = 0;
    closePopover();
    send("cancelSession", { session }).catch(() => {});
    for (const e of entries) e.layer.remove();
    entries.clear();
    images.clear();
    nearObs.disconnect();
    visObs.disconnect();
  }

  function stop() {
    clearAll();
    auto = false;
    mounted = false;
    loopId++;
    mutationObs.disconnect();
    window.removeEventListener("scroll", onScroll, { capture: true });
    window.removeEventListener("resize", onScroll);
    window.removeEventListener("pointerdown", onOutsidePointer, true);
    document.removeEventListener("load", onLoadCapture, true);
    shadow.querySelector(".selector")?.remove();
    shadow.querySelector(".toast")?.remove();
    setPeek(false);
    toggleScript(false);
    host.remove();
    lastProgress = "";
    send("progress", { state: "off" }).catch(() => {});
  }

  function restart() {
    const wasAuto = auto;
    clearAll();
    if (wasAuto) startAuto();
  }

  function setPeek(on) {
    peekOn = on;
    wrap.classList.toggle("peek", on);
    peekBtn.setAttribute("aria-pressed", String(on));
    if (on) closePopover();
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    switch (msg?.type) {
      case "bl:start":
        mount(msg);
        if (!auto) startAuto();
        break;
      case "bl:region":
        mount(msg);
        startRegionSelect();
        break;
      case "bl:stop":
        if (mounted) stop();
        break;
      case "bl:peek":
        if (mounted) setPeek(msg.on === undefined ? !peekOn : !!msg.on);
        break;
      case 'bl:retry-failed':
        if (mounted) retryFailures();
        sendResponse({message:mounted ? '실패한 부분만 다시 시도합니다. 직접 고친 대사는 유지돼요.' : '먼저 번역을 켜 주세요.'});
        return;
      case 'bl:next-issue':
        sendResponse(nextIssue());
        return;
      case 'bl:script':
        if (mounted) toggleScript(msg.open);
        break;
      case 'bl:edit':
        if (mounted) editVisibleImage();
        break;
    }
    sendResponse(status());
  });

  chrome.storage.onChanged.addListener((changes) => {
    if (!settings) return;
    for (const [k, { newValue }] of Object.entries(changes)) settings[k] = newValue;
    if (["src", "tgt", "model", "reading", "workProfiles", "vision"].some((k) => k in changes)) return mounted && restart();
    for (const e of entries) {
      e.segs.forEach((el) => (el.dataset.sig = ""));
      if (e.lastSnap) apply(e, e.lastSnap);
    }
  });

  window.addEventListener("pagehide", () => mounted && send("cancelSession", { session }).catch(() => {}));

  function status() {
    let done = 0, pending = 0, failed = 0, edited = 0, jobsActive = 0, review = 0, overflow = 0, failedJobs = 0;
    for (const e of entries) {
      if (e.status==='failed' && !e.segs.size) failedJobs++;
      if (e.jobId && !FINISHED.has(e.status)) jobsActive++;
      e.segs.forEach((el) => {
        const st = el.dataset.status;
        if (st === "done" || (st === "review" && el.dataset.hasText === '1')) done++;
        else if (st === 'review') failed++;
        else if (st === "failed") failed++;
        else pending++;
        if (el.dataset.edited) edited++;
        if (st === 'review') review++;
        if (el.classList.contains('overflow')) overflow++;
      });
    }
    return { running: auto, mounted, peek: peekOn, done, pending, failed, failedJobs, edited, review, overflow, retrying:retryGeneration===gen, jobsActive, helperDown: helperDown > 6, lastError };
  }

  function nextIssue() {
    const issues=[];
    for (const e of entries) for (const [sid,el] of e.segs) {
      if (['review','failed'].includes(el.dataset.status) || el.classList.contains('overflow')) issues.push({e,sid,el});
    }
    if (!issues.length) return {found:false,message:'현재 확인할 대사가 없어요.'};
    const index=(issues.findIndex(item=>item.el===lastIssueElement)+1)%issues.length;
    const item=issues[index];
    lastIssueElement=item.el;
    focusLine(item.e, item.sid);
    return {found:true,message:`확인할 대사 ${index+1} / ${issues.length}`};
  }

  // Bring one balloon into view and open its popover.
  function focusLine(e, sid) {
    const el = e.segs.get(sid);
    if (!el) return;
    setPeek(false);
    if (anchorBox(e.anchor)) {
      if (e.anchor.type === 'element') e.anchor.el.scrollIntoView({block: 'center'});
      entries.forEach(place);
      const rect = el.getBoundingClientRect();
      window.scrollBy({top: rect.top - innerHeight * .35, behavior: 'instant'});
      entries.forEach(place);
    }
    openPopover(e, sid);
  }

  async function retryFailures() {
    if (retryGeneration===gen) return;
    const generation=gen;
    retryGeneration=generation;
    let restored=0;
    try {
      for (const e of [...entries]) {
        if (!alive(e) || gen!==generation) return;
        if (!FINISHED.has(e.status)) continue;
        if (!e.segs.size && e.status==='failed' && e.resubmit) {
          e.retries=0;
          await e.resubmit();
          continue;
        }
        for (const base of e.lastSnap?.segments || []) {
          if (!alive(e) || gen!==generation) return;
          const current=segView(e,base);
          if (!BalloonLensReading.needsRetry(current)) continue;
          const opts=jobOpts(1,e);
          const page=e.lastSnap.segments.filter(s=>s.id!==base.id).slice(-16).map(s=>{
            const item=segView(e,s);
            return {source:item.src.slice(0,500),translation:(item.tgt || '').slice(0,500)};
          });
          const result=await send('translateLine',{text:current.src,src:settings.src,tgt:settings.tgt,
            model:settings.model,scope:opts.scope,options:opts.options,page,fresh:true});
          if (!alive(e) || gen!==generation) return;
          // An edit made while inference was running always wins.
          if (segView(e,base).edited || !result.text) continue;
          setOverride(e,base.id,{tgt:result.text,status:result.status,edited:false});
          restored++;
        }
      }
      toast(restored ? `${restored}개 대사를 다시 번역했어요.` : '재시도를 마쳤어요. 남은 오류는 대사를 눌러 확인해 주세요.');
    } catch(err) {
      if (gen===generation) toast(`재시도 중 오류: ${explain(err.message)}`);
    } finally {
      if (retryGeneration===generation) retryGeneration=null;
      if (gen===generation) updatePill();
    }
  }

  // ---------- image discovery ----------
  const nearObs = new IntersectionObserver(
    (items) => {
      if (subscriptionMode()) return scheduleReadAhead();
      for (const it of items) {
        if (!it.isIntersecting) continue;
        const r = it.boundingClientRect;
        ensure(it.target, r.bottom > 0 && r.top < innerHeight ? 2 : 3);
      }
    },
    { rootMargin: "150% 0px" }
  );

  const visObs = new IntersectionObserver((items) => {
    for (const it of items) {
      const e = images.get(it.target)?.main;
      if (!e?.jobId || e.status !== "queued") continue;
      const want = it.isIntersecting ? 2 : 3;
      if (e.priority !== want) {
        e.priority = want;
        send("setPriority", { jobId: e.jobId, value: want }).catch(() => {});
      }
    }
  });

  // Subscription models pay per image, so the queue reads ahead by one image only (see reading.js planReadAhead).
  function subscriptionMode() {
    return !!settings && BalloonLensReading.isSubscription(settings.model);
  }

  let readAheadQueued = false;
  function scheduleReadAhead() {
    if (readAheadQueued || !auto || !subscriptionMode()) return;
    readAheadQueued = true;
    requestAnimationFrame(() => {
      readAheadQueued = false;
      readAhead();
    });
  }

  function readAhead() {
    if (!auto) return;
    const items = [];
    for (const img of document.querySelectorAll("img")) {
      const key = img.currentSrc || img.src;
      if (!key || !eligible(img)) continue;
      const r = img.getBoundingClientRect();
      const rec = images.get(img);
      items.push({ img, top: r.top, bottom: r.bottom, queued: !!rec && rec.key === key });
    }
    const busy = [...images.values()].some(rec =>
      rec.main && rec.main.priority === 3 && alive(rec.main) && !FINISHED.has(rec.main.status));
    for (const step of BalloonLensReading.planReadAhead(items, innerHeight, busy)) ensure(step.item.img, step.priority);
  }

  const mutationObs = new MutationObserver((records) => {
    for (const rec of records) {
      if (rec.type === "attributes" && rec.target instanceof HTMLImageElement) retrigger(rec.target);
      for (const n of rec.addedNodes || []) {
        if (n instanceof HTMLImageElement) nearObs.observe(n);
        else if (n.querySelectorAll) n.querySelectorAll("img").forEach((img) => nearObs.observe(img));
      }
    }
  });

  function onLoadCapture(ev) {
    if (ev.target instanceof HTMLImageElement && auto) retrigger(ev.target);
  }

  function retrigger(img) {
    nearObs.unobserve(img);
    nearObs.observe(img); // observe() fires an initial callback with the current intersection
  }

  function eligible(img) {
    if (!img.complete || img.naturalWidth < MIN_NATURAL || img.naturalHeight < MIN_NATURAL) return false;
    const r = img.getBoundingClientRect();
    return r.width >= MIN_DISPLAY && r.height >= MIN_DISPLAY;
  }

  function ensure(img, priority) {
    if (!auto) return;
    const key = img.currentSrc || img.src;
    let rec = images.get(img);
    if (rec && rec.key === key) return;
    if (rec) dropRecord(img, rec); // same element now shows a different picture
    if (!key || !eligible(img)) return;
    rec = { key, main: null, captures: [], needsCapture: false, covered: [] };
    images.set(img, rec);
    visObs.observe(img);
    const entry = makeEntry({ type: "element", el: img, fx: 0, fy: 0, fw: 1, fh: 1 });
    rec.main = entry;
    entry.resubmit = () => submitFetch(rec, entry, entry.priority);
    const task = () => submitFetch(rec, entry, priority);
    task.url = key;
    task.entry = entry;
    priority === 2 ? waiting.unshift(task) : waiting.push(task);
    pump();
  }

  function pump() {
    scheduleReadAhead();
    while (submitting < MAX_SUBMITS && waiting.length) {
      // Wait for discovered earlier images to finish so their dialogue is available
      // as context. Never wait inside a submit slot (that can deadlock a long page).
      const index = waiting.findIndex(task => {
        if (!alive(task.entry)) return true;
        // Subscription pages go out side by side (the helper runs CLI_PARALLEL calls at once), so they do not queue
        // behind earlier pages. Local pages keep reading order, since each one takes the earlier pages as context.
        if (subscriptionMode()) return true;
        const target = task.entry.anchor.el;
        for (const img of document.querySelectorAll('img')) {
          if (img === target) break;
          const earlier = images.get(img)?.main;
          if (earlier && alive(earlier) && !FINISHED.has(earlier.status)) return false;
        }
        return true;
      });
      if (index < 0) {
        // While earlier images translate, download the next ones so they start without a network wait.
        for (const task of waiting.slice(0, 2)) {
          if (task.prefetched || !task.url) continue;
          task.prefetched = true;
          send('prefetchImage', { url: task.url }).catch(() => {});
        }
        return;
      }
      const [task] = waiting.splice(index, 1);
      submitting++;
      task().finally(() => {
        submitting--;
        pump();
      });
    }
  }

  function dropRecord(img, rec) {
    [rec.main, ...rec.captures].forEach((e) => e && removeEntry(e));
    images.delete(img);
    visObs.unobserve(img);
  }

  async function submitFetch(rec, entry, priority) {
    if (!alive(entry)) return;
    entry.status = "submitting";
    try {
      const { jobId, fetchMs } = await send("translateImage", { url: rec.key, opts: jobOpts(priority, entry) });
      if (!alive(entry)) return void send("cancelJob", { jobId }).catch(() => {});
      Object.assign(entry, { jobId, fetchMs, priority, status: "queued" });
    } catch (err) {
      if (!alive(entry)) return;
      if (err.message.startsWith("fetch:")) {
        // Original image not reachable (hotlink protection, auth...) -> capture what's on screen.
        removeEntry(entry);
        rec.main = null;
        rec.needsCapture = true;
      } else fail(entry, err.message);
    }
  }

  // ---------- screen-capture fallback ----------
  function uncoveredRatio(covered, a, b) {
    const parts = covered.map(([x, y]) => [Math.max(a, x), Math.min(b, y)]).filter(([x, y]) => y > x).sort((p, q) => p[0] - q[0]);
    let len = 0, end = a;
    for (const [x, y] of parts) {
      if (y <= end) continue;
      len += y - Math.max(x, end);
      end = y;
    }
    return 1 - len / (b - a);
  }

  async function captureTick() {
    if (!auto || captureBusy || popover || Date.now() - lastScroll < 350 || document.visibilityState !== "visible") return;
    for (const [img, rec] of images) {
      if (!rec.needsCapture) continue;
      const r = img.getBoundingClientRect();
      const top = Math.max(0, r.top), bottom = Math.min(innerHeight, r.bottom);
      const left = Math.max(0, r.left), right = Math.min(innerWidth, r.right);
      if (bottom - top < 80 || right - left < 80) continue;
      const a = (top - r.top) / r.height, b = (bottom - r.top) / r.height;
      if (uncoveredRatio(rec.covered, a, b) < 0.3) continue;
      captureBusy = true;
      try {
        const anchor = { type: "element", el: img, fx: (left - r.left) / r.width, fy: a, fw: (right - left) / r.width, fh: b - a };
        const e = await captureInto(anchor, { x: left, y: top, w: right - left, h: bottom - top }, 2);
        if (e) {
          e.parent = rec;
          rec.captures.push(e);
          rec.covered.push([a, b]);
        }
      } finally {
        captureBusy = false;
      }
      return; // one capture per tick
    }
  }

  async function captureInto(anchor, rect, priority) {
    const myGen = gen;
    const scrollAt = lastScroll;
    host.style.visibility = "hidden"; // never OCR our own translations (section 7.2)
    await nextFrames(2);
    let res;
    try {
      res = await send("translateRegion", { rect, viewport: { w: innerWidth, h: innerHeight }, opts: jobOpts(priority) });
    } catch (err) {
      lastError = err.message;
      if (priority === 1) toast(`영역 번역 실패: ${explain(err.message)}`);
      return null;
    } finally {
      host.style.visibility = "";
    }
    if (gen !== myGen || lastScroll !== scrollAt) {
      // Page moved while capturing: the screenshot may not match the anchor any more.
      send("cancelJob", { jobId: res.jobId }).catch(() => {});
      if (priority === 1) toast("캡처하는 동안 화면이 움직였어요. 다시 선택해 주세요.");
      return null;
    }
    const e = makeEntry(anchor);
    Object.assign(e, { jobId: res.jobId, status: "queued", priority });
    return e;
  }

  // ---------- region selection ----------
  function startRegionSelect() {
    if (!mounted || shadow.querySelector(".selector")) return;
    setPeek(false);
    closePopover();
    const sel = document.createElement("div");
    sel.className = "selector";
    sel.innerHTML = '<div class="hint">번역할 부분을 드래그하세요 · <kbd>Esc</kbd> 취소</div><div class="box" hidden></div>';
    const box = sel.querySelector(".box");
    wrap.appendChild(sel);
    let start = null;
    const onKey = (ev) => ev.key === "Escape" && done();
    const done = () => {
      sel.remove();
      window.removeEventListener("keydown", onKey, true);
    };
    window.addEventListener("keydown", onKey, true);
    sel.addEventListener("pointerdown", (ev) => {
      start = { x: ev.clientX, y: ev.clientY };
      sel.setPointerCapture(ev.pointerId);
    });
    sel.addEventListener("pointermove", (ev) => {
      if (!start) return;
      const x = Math.min(start.x, ev.clientX), y = Math.min(start.y, ev.clientY);
      Object.assign(box.style, { left: `${x}px`, top: `${y}px`, width: `${Math.abs(ev.clientX - start.x)}px`, height: `${Math.abs(ev.clientY - start.y)}px` });
      box.hidden = false;
    });
    sel.addEventListener("pointerup", async (ev) => {
      if (!start) return;
      const rect = { x: Math.min(start.x, ev.clientX), y: Math.min(start.y, ev.clientY), w: Math.abs(ev.clientX - start.x), h: Math.abs(ev.clientY - start.y) };
      done();
      if (rect.w < 16 || rect.h < 16) return;
      const img = document.elementsFromPoint(rect.x + rect.w / 2, rect.y + rect.h / 2).find((n) => n instanceof HTMLImageElement);
      let anchor = { type: "page", x: rect.x + scrollX, y: rect.y + scrollY, w: rect.w, h: rect.h };
      if (img) {
        const r = img.getBoundingClientRect();
        anchor = { type: "element", el: img, fx: (rect.x - r.left) / r.width, fy: (rect.y - r.top) / r.height, fw: rect.w / r.width, fh: rect.h / r.height };
      }
      await captureInto(anchor, rect, 1);
    });
  }

  // ---------- entries & rendering ----------
  function makeEntry(anchor) {
    const layer = document.createElement("div");
    layer.className = "layer";
    layersEl.appendChild(layer);
    const e = { anchor, gen, layer, jobId: null, status: "new", priority: 2, segs: new Map(), overrides: new Map(), fitW: 0, retries: 0, fetchMs: 0, lastSnap: null };
    entries.add(e);
    place(e);
    return e;
  }

  const alive = (e) => e.gen === gen && entries.has(e);

  function removeEntry(e) {
    if (popover?.e === e) closePopover();
    e.layer.remove();
    entries.delete(e);
    if (e.jobId && !FINISHED.has(e.status)) send("cancelJob", { jobId: e.jobId }).catch(() => {});
    queueMicrotask(pump);
  }

  function fail(e, error) {
    e.status = "failed";
    noteError(error);
    queueMicrotask(pump);
  }

  let lastToastError = { code: "", at: 0 };
  function noteError(code) {
    lastError = code;
    if (code === "helper_unreachable" || (lastToastError.code === code && Date.now() - lastToastError.at < 10000)) return;
    lastToastError = { code, at: Date.now() };
    toast(`번역하지 못한 부분이 있어요: ${explain(code)}`);
  }

  function anchorBox(a) {
    if (a.type === "page") return { left: a.x - scrollX, top: a.y - scrollY, width: a.w, height: a.h };
    if (!a.el.isConnected) return null;
    const r = a.el.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    return { left: r.left + a.fx * r.width, top: r.top + a.fy * r.height, width: a.fw * r.width, height: a.fh * r.height };
  }

  function place(e) {
    const b = anchorBox(e.anchor);
    if (!b) return void (e.layer.style.display = "none");
    Object.assign(e.layer.style, { display: "", transform: `translate(${b.left}px, ${b.top}px)`, width: `${b.width}px`, height: `${b.height}px` });
    if (Math.abs(b.width - e.fitW) > 2 || Math.abs(b.height - (e.fitH || 0)) > 2) {
      e.fitW = b.width;
      e.fitH = b.height;
      e.segs.forEach(fit);
    }
  }

  let placeQueued = false;
  function onScroll() {
    lastScroll = Date.now();
    scheduleReadAhead();
    if (placeQueued) return;
    placeQueued = true;
    requestAnimationFrame(() => {
      placeQueued = false;
      entries.forEach(place);
      positionPopover();
    });
  }

  function segBox(s, W, H) {
    return BalloonLensReading.sourceFrame(s,W,H).map((v,i)=>v/(i%2 ? H : W));
  }

  function toImageFrac(e, [fx, fy, fw, fh]) {
    const a = e.anchor;
    return [a.fx + fx * a.fw, a.fy + fy * a.fh, fw * a.fw, fh * a.fh];
  }

  function overlap(p, q) {
    const iw = Math.min(p[0] + p[2], q[0] + q[2]) - Math.max(p[0], q[0]);
    const ih = Math.min(p[1] + p[3], q[1] + q[3]) - Math.max(p[1], q[1]);
    return iw > 0 && ih > 0 ? (iw * ih) / Math.min(p[2] * p[3], q[2] * q[3]) : 0;
  }

  // Overlapping captures of one image: keep one copy of each balloon. A copy sliced by a capture edge reads
  // less text, so prefer the uncut copy, or the one whose OCR box is clearly bigger.
  function duplicateInSibling(e, box, cut, area) {
    const ib = toImageFrac(e, box);
    for (const sib of e.parent.captures) {
      if (sib === e) continue;
      for (const [id, el] of sib.segs) {
        if (overlap(ib, JSON.parse(el.dataset.ib)) < 0.5) continue;
        if ((el.dataset.cut === "1" && !cut) || area > Number(el.dataset.area) * 1.15) {
          if (popover?.e === sib && popover.sid === id) closePopover();
          el.remove();
          el._cleanup?.remove();
          sib.segs.delete(id);
          continue;
        }
        return true;
      }
    }
    return false;
  }

  // Snapshot segment + the reader's unsaved/saved local changes.
  function segView(e, base) {
    const ov = e.overrides.get(base.id);
    if (!ov) return base;
    return {
      ...base,
      src: ov.src ?? base.src,
      tgt: ov.tgt ?? base.tgt,
      status: ov.status ?? (ov.tgt != null ? "done" : base.status),
      edited: ov.edited ?? base.edited,
      hidden: !!ov.hidden,
    };
  }

  function apply(e, snap) {
    if (!alive(e) || snap.tgt !== settings.tgt) return;
    e.status = snap.status;
    e.lastSnap = snap;
    if (snap.error && snap.status !== "done") noteError(snap.error);
    if (!snap.width || !snap.height) return;
    for (const base of snap.segments) {
      const v = segView(e, base);
      let el = e.segs.get(base.id);
      const sig = [v.status, v.tgt, v.edited, v.hidden, settings.showSource && v.src].join("|");
      if (el && el.dataset.sig === sig) continue;
      if (!el) {
        const box = segBox(base, snap.width, snap.height);
        // OCR boxes stop a little short of a capture edge that slices through text, so allow some slack.
        const [, y, w, h] = base.bbox;
        const margin = Math.max(12, Math.min(w, h) * 0.5);
        const cut = y <= margin || y + h >= snap.height - margin;
        const area = (w / snap.width) * e.anchor.fw * (h / snap.height) * e.anchor.fh; // OCR box, as image fraction
        if (e.parent && duplicateInSibling(e, box, cut, area)) continue;
        el = document.createElement("div");
        el._sourceFontRatio=BalloonLensReading.sourceFontSize(base)/snap.width;
        el._imageLayer=e.layer;
        el._frac=box; // the OCR box as fractions of the layer, to return to after a grown box
        const pct = (n) => `${n * 100}%`;
        Object.assign(el.style, { left: pct(box[0]), top: pct(box[1]), width: pct(box[2]), height: pct(box[3]), minHeight: pct(box[3]) });
        el.dataset.ib = JSON.stringify(toImageFrac(e, box));
        el.dataset.cut = cut ? "1" : "0";
        el.dataset.area = String(area);
        const sid = base.id;
        el.addEventListener("click", (ev) => {
          ev.preventDefault();
          if (el.classList.contains("clickable")) openPopover(e, sid);
        });
        e.layer.appendChild(el);
        e.segs.set(base.id, el);
        if (base.presentation?.cleanup) {
          const p = base.presentation, patch = document.createElement('img');
          patch.className = 'cleanup';
          patch.src = p.cleanup;
          const [px,py,pw,ph] = p.cleanup_bbox;
          Object.assign(patch.style, {left:pct(px/snap.width), top:pct(py/snap.height), width:pct(pw/snap.width), height:pct(ph/snap.height)});
          e.layer.prepend(patch);
          el._cleanup = patch;
        }
      }
      el.dataset.sig = sig;
      renderSeg(el, v, popover?.e === e && popover.sid === base.id);
    }
  }

  function renderSeg(el, v, active) {
    const shown = (v.status === "done" || v.status === "review") && !!v.tgt;
    el.className = ["seg", v.status, shown || v.status === "failed" || v.status === 'review' ? "clickable" : "", v.edited ? "edited" : "", v.hidden ? "user-hidden" : "", active ? "active" : "", v.presentation?.frame ? "framed" : ""].join(" ");
    el.dataset.status = v.status;
    el.dataset.hasText = v.tgt ? '1' : '';
    el.dataset.edited = v.edited ? "1" : "";
    el.title = v.status === "failed" ? "번역 실패 · 눌러서 다시 시도" : v.hidden ? "눌러서 번역 다시 보기" : "";
    el.textContent = ""; // text only, never HTML (section 14.1)
    el.style.background = "transparent";
    if (el._cleanup) el._cleanup.hidden = !shown || !!v.hidden;
    if (!shown) {
      resetBox(el); // a balloon that lost its text goes back to its OCR box
      return;
    }
    const t = document.createElement("span");
    t.textContent = v.tgt;
    t.dataset.fullText = v.tgt;
    el.append(t);
    if (settings.showSource) {
      const o = document.createElement("span");
      o.className = "src";
      o.textContent = v.src;
      el.append(o);
    }
    el.style.background = el._cleanup ? 'transparent' : `rgba(255,255,255,${settings.bgOpacity})`;
    fit(el);
  }

  // A balloon sits in its layer as fractions of the image (its OCR box). A grown box is set in pixels instead.
  const pctOf = (n) => `${n * 100}%`;
  function setBox(el, x, y, w, h) {
    Object.assign(el.style, { left: `${x}px`, top: `${y}px`, width: `${w}px`, height: `${h}px`, minHeight: "0px" });
  }
  function resetBox(el) {
    const [x, y, w, h] = el._frac;
    Object.assign(el.style, { left: pctOf(x), top: pctOf(y), width: pctOf(w), height: pctOf(h), minHeight: pctOf(h) });
  }

  // A line that does not fit its box grows around it (fitGrowing), so the whole translation shows.
  function fit(el) {
    if (!el.firstChild) return;
    const layer = el._imageLayer, LW = layer.clientWidth, LH = layer.clientHeight;
    if (!LW || !LH) return; // the layer is not laid out (hidden tab); place() measures again when it is
    const [fx, fy, fw, fh] = el._frac;
    const x0 = fx * LW, y0 = fy * LH, w0 = fw * LW, h0 = fh * LH;
    setBox(el, x0, y0, w0, h0);
    const css = getComputedStyle(el), text = el.firstChild;
    const padX = parseFloat(css.paddingLeft) + parseFloat(css.paddingRight);
    const padY = parseFloat(css.paddingTop) + parseFloat(css.paddingBottom);
    const ctx = document.createElement('canvas').getContext('2d');
    const shrink = settings.showSource ? .7 : 1; // the source line takes the rest of the height
    const baseW = Math.max(1, w0 - padX), baseH = Math.max(1, h0 - padY) * shrink;
    const fitted = BalloonLensReading.fitGrowing(text.dataset.fullText || text.textContent, baseW, baseH,
      Math.max(1, LW - padX), Math.max(1, LH - padY) * shrink, (s, size) => {
        ctx.font = `${css.fontWeight} ${size}px ${css.fontFamily}`;
        return ctx.measureText(s).width;
      }, settings.fontScale, el._sourceFontRatio * LW);
    const grown = fitted.width > baseW + .5 || fitted.height > baseH + .5;
    if (grown) {
      const boxW = fitted.width + padX, boxH = fitted.height / shrink + padY;
      const x = Math.max(0, Math.min(LW - boxW, x0 + w0 / 2 - boxW / 2));
      const y = Math.max(0, Math.min(LH - boxH, y0 + h0 / 2 - boxH / 2));
      setBox(el, x, y, boxW, boxH);
    } else resetBox(el);
    text.textContent = fitted.lines.join('\n');
    text.style.whiteSpace = 'pre';
    el.style.fontSize = `${fitted.size}px`;
    // A grown box leaves the cleaned area of the original text, so it gets its own light background.
    el.style.background = el._cleanup && !grown ? 'transparent' : `rgba(255,255,255,${settings.bgOpacity})`;
    el.classList.toggle('overflow', fitted.overflow);
    el.title = fitted.overflow ? '대사가 길어요 · 눌러서 전체 보기' : '눌러서 원문 확인·수정';
  }

  function setOverride(e, sid, patch) {
    e.overrides.set(sid, { ...(e.overrides.get(sid) || {}), ...patch });
    const el = e.segs.get(sid);
    if (el) el.dataset.sig = "";
    if (e.lastSnap) apply(e, e.lastSnap);
  }

  // ---------- line popover: check original, fix OCR, re-translate, edit (section 3.4) ----------
  function onOutsidePointer(ev) {
    // Events from inside our closed shadow root are retargeted to the host element.
    if (popover && ev.target !== host) closePopover();
  }

  function closePopover() {
    if (!popover) return;
    const { e, sid, pop } = popover;
    popover = null;
    pop.remove();
    e.segs.get(sid)?.classList.remove("active");
  }

  function positionPopover() {
    if (!popover) return;
    const r = popover.e.segs.get(popover.sid)?.getBoundingClientRect();
    if (!r || (!r.width && !r.height)) return closePopover();
    const pw = 320, ph = popover.pop.offsetHeight || 280, m = 10;
    const left = Math.min(Math.max(m, r.left + r.width / 2 - pw / 2), innerWidth - pw - m);
    let top = r.bottom + m;
    if (top + ph > innerHeight - m) top = Math.max(m, r.top - ph - m);
    Object.assign(popover.pop.style, { left: `${left}px`, top: `${top}px` });
  }

  function openPopover(e, sid) {
    closePopover();
    const base = e.lastSnap?.segments.find((x) => x.id === sid);
    if (!base) return;
    const v = segView(e, base);
    const snap = e.lastSnap;
    const otherModel = ["qwen3.5:9b", "claude-haiku-5-5", "gpt-6-luna"].find((m) => m !== settings.model) || "qwen3.5:9b";
    const ocrSrc = base.ocr_src ?? base.src;

    const pop = document.createElement("div");
    pop.className = "popover";
    pop.innerHTML = `
      <div class="head"><b>대사 확인</b><button class="x" data-act="close" title="닫기 (Esc)">${ICON.close}</button></div>
      <label>원문 <span class="tag" data-ref="srcTag"></span></label>
      <textarea data-ref="src" rows="2" spellcheck="false"></textarea>
      <label>번역 <span class="tag" data-ref="tgtTag"></span></label>
      <textarea data-ref="tgt" rows="2"></textarea>
      <div class="actions">
        <button class="btn" data-act="retry" title="원문 칸의 글로 다시 번역">다시 번역</button>
        <button class="btn" data-act="other"></button>
        <button class="btn primary" data-act="save">저장</button>
      </div>
      <div class="links">
        <button data-act="hide"></button>
        <button data-act="copy">번역 복사</button>
        <button data-act="reset" hidden>자동 번역으로 되돌리기</button>
        <button data-act="term" title="원문 칸이나 번역 칸에서 단어를 선택한 뒤 누르면 채워져요">용어 추가</button>
        <button data-act="regions" title="글자 영역과 번역 칸의 위치·크기를 고쳐요">영역 편집</button>
      </div>
      <div class="term" data-ref="termForm" hidden>
        <input data-ref="termSrc" placeholder="원문 표기 (예: 田中)" maxlength="60" />
        <input data-ref="termTgt" placeholder="한국어 (예: 다나카)" maxlength="60" />
        <button class="btn" data-act="termSave">추가</button>
      </div>
      <div class="foot" data-ref="foot">Ctrl+Enter 저장 · Esc 닫기</div>`;
    const ref = (n) => pop.querySelector(`[data-ref="${n}"]`);
    const act = (n) => pop.querySelector(`[data-act="${n}"]`);
    const srcBox = ref("src"), tgtBox = ref("tgt"), foot = ref("foot");
    srcBox.value = v.src;
    tgtBox.value = v.tgt || "";
    ref("srcTag").textContent = v.src !== ocrSrc ? "고친 원문" : "";
    const tgtTag = ref("tgtTag");
    const setTag = (text, warn) => {
      tgtTag.textContent = text;
      tgtTag.className = `tag${warn ? " warn" : ""}`;
    };
    if (v.edited) setTag("직접 고침");
    else if (v.status === "review") setTag("확인 필요", true);
    else if (v.status === "failed") setTag("번역 실패", true);
    act("other").textContent = `${MODEL_NAMES[otherModel]}로`;
    act("other").title = `${MODEL_NAMES[otherModel]} 모델로 다시 번역`;
    act("hide").textContent = v.hidden ? "번역 다시 보이기" : "이 대사만 원문 보기";
    act("reset").hidden = !v.edited;

    const busy = (on, msg) => {
      pop.classList.toggle("busy", on);
      if (msg) foot.textContent = msg;
    };

    const lineContext = () => {
      const opts = jobOpts(1, e);
      return {scope:opts.scope, options:opts.options,
        page:snap.segments.filter(s=>s.id!==sid).slice(-16).map(s=>{
          const item=segView(e,s);
          return {source:item.src.slice(0,500), translation:(item.tgt || '').slice(0,500)};
        })};
    };

    async function retranslate(model) {
      const text = srcBox.value.trim();
      if (!text) return void (foot.textContent = "원문이 비어 있어요");
      busy(true, `${MODEL_NAMES[model]}로 번역하는 중…`);
      try {
        const r = await send("translateLine", { text, src: snap.src, tgt: snap.tgt, model, ...lineContext(), fresh: true });
        if (!r.text) return void (foot.textContent = `번역 실패: ${explain(r.error || "unknown")}`);
        tgtBox.value = r.text;
        setTag("저장 안 됨", true);
        foot.textContent = r.status === "review" ? "숫자·표현·의미를 원문과 비교해 주세요." : "마음에 들면 저장하세요.";
      } catch (err) {
        foot.textContent = `번역 실패: ${explain(err.message)}`;
      } finally {
        busy(false);
      }
    }

    async function persist(body) {
      const a = e.anchor;
      const wholeImage = a.type === "element" && a.fx === 0 && a.fy === 0 && a.fw === 1 && a.fh === 1;
      if (!wholeImage || !snap.image_hash) return false;
      const work = BalloonLensReading.workSettings(location.href, settings.workProfiles);
      await send("saveEdit", { image_hash: snap.image_hash, src: snap.src, tgt: snap.tgt, bbox: base.bbox,
        scope: work.matched ? work.scope : "", ...body });
      return true;
    }

    async function save() {
      const src = srcBox.value.trim(), tgt = tgtBox.value.trim();
      if (!tgt) return void (foot.textContent = "번역을 비워둘 수는 없어요");
      busy(true, "저장하는 중…");
      try {
        const kept = await persist({ src_text: src === ocrSrc ? "" : src, tgt_text: tgt });
        setOverride(e, sid, { src, tgt, status:'done', edited: true, hidden: false });
        closePopover();
        toast(kept ? "저장했어요. 다음에 이 페이지를 열어도 유지돼요." : "화면 캡처로 번역한 대사라, 이 페이지를 닫기 전까지만 유지돼요.");
      } catch (err) {
        busy(false, `저장하지 못했어요: ${explain(err.message)}`);
      }
    }

    async function reset() {
      busy(true, "되돌리는 중…");
      try {
        const r = await send("translateLine", { text: ocrSrc, src: snap.src, tgt: snap.tgt, model: settings.model, ...lineContext(), fresh: false });
        if (!r.text) throw new Error('자동 번역 결과가 없어 기존 수정을 유지했어요');
        await persist({ clear: true });
        setOverride(e, sid, { src: ocrSrc, tgt: r.text, status:r.status, edited: false, hidden: false });
        closePopover();
      } catch (err) {
        busy(false, `되돌리지 못했어요: ${explain(err.message)}`);
      }
    }

    async function saveTerm() {
      const source = ref("termSrc").value.trim(), target = ref("termTgt").value.trim();
      if (!source || !target) return void (foot.textContent = "원문 표기와 한국어를 둘 다 적어 주세요");
      try {
        if (await addTerm(source, target)) closePopover();
      } catch (err) {
        foot.textContent = err.message;
      }
    }

    pop.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") closePopover();
      else if (ev.key === "Enter" && ev.target.closest?.(".term")) saveTerm();
      else if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) save();
    });
    pop.addEventListener("click", (ev) => {
      const name = ev.target.closest("[data-act]")?.dataset.act;
      if (name === "close") closePopover();
      if (name === "retry") retranslate(settings.model);
      if (name === "other") retranslate(otherModel);
      if (name === "save") save();
      if (name === "reset") reset();
      if (name === "regions") openEditor(e);
      if (name === "term") {
        const form = ref("termForm");
        form.hidden = !form.hidden;
        if (!form.hidden) {
          const picked = (box) => box.value.slice(box.selectionStart, box.selectionEnd).trim();
          ref("termSrc").value = picked(srcBox);
          ref("termTgt").value = picked(tgtBox);
          ref("termSrc").focus();
        }
        positionPopover();
      }
      if (name === "termSave") saveTerm();
      if (name === "hide") {
        setOverride(e, sid, { hidden: !v.hidden });
        closePopover();
      }
      if (name === "copy") {
        navigator.clipboard.writeText(tgtBox.value).then(
          () => (foot.textContent = "번역을 복사했어요"),
          () => (foot.textContent = "복사하지 못했어요")
        );
      }
    });

    wrap.appendChild(pop);
    popover = { e, sid, pop };
    e.segs.get(sid)?.classList.add("active");
    positionPopover();
    tgtBox.focus();
  }

  // ---------- toast ----------
  let toastTimer = 0;
  // ---------- dialogue list panel ----------
  let script = null; // { panel, filter, query, sig, shown }

  function orderedEntries() {
    const y = (e) => (e.anchor.type === 'page' ? e.anchor.y : e.anchor.fy);
    return [...entries].filter((e) => e.lastSnap).sort((a, b) => {
      const ea = a.anchor.el, eb = b.anchor.el;
      if (ea && eb && ea !== eb) return ea.compareDocumentPosition(eb) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
      if (!!ea !== !!eb) return ea ? -1 : 1;
      return y(a) - y(b);
    });
  }

  function scriptLines() {
    const lines = [];
    orderedEntries().forEach((e, group) => {
      for (const base of e.lastSnap.segments) {
        const el = e.segs.get(base.id);
        if (!el) continue; // a duplicate from an overlapping capture
        const v = segView(e, base);
        lines.push({ e, sid: base.id, key: `${group}:${base.id}`, group, src: v.src || '', tgt: v.tgt || '',
          status: v.status, edited: !!v.edited, overflow: el.classList.contains('overflow') });
      }
    });
    return lines;
  }

  const STATE_LABEL = { review: '확인 필요', failed: '실패', pending: '번역 중' };

  function renderScript(force = false) {
    if (!script) return;
    const lines = scriptLines();
    const active = popover ? `${orderedEntries().indexOf(popover.e)}:${popover.sid}` : '';
    const sig = JSON.stringify([script.filter, script.query, active,
      lines.map((l) => [l.key, l.src, l.tgt, l.status, l.edited, l.overflow])]);
    if (!force && sig === script.sig) return;
    script.sig = sig;
    const issue = (l) => ['review', 'failed'].includes(l.status) || l.overflow;
    script.shown = lines.filter((l) =>
      (script.filter === 'all' || (script.filter === 'edited' ? l.edited : issue(l))) &&
      (!script.query || `${l.src}\n${l.tgt}`.toLowerCase().includes(script.query)));
    const list = script.panel.querySelector('[data-ref="list"]');
    const scrollTop = list.scrollTop;
    list.replaceChildren();
    let group = -1;
    for (const l of script.shown) {
      if (l.group !== group) {
        group = l.group;
        const head = document.createElement('div');
        head.className = 'script-group';
        head.textContent = `이미지 ${group + 1}`;
        list.append(head);
      }
      const item = document.createElement('button');
      item.className = `script-item${l.key === active ? ' active' : ''}`;
      item.dataset.line = l.key;
      const tgt = document.createElement('span');
      tgt.className = 'tgt';
      tgt.textContent = l.tgt || (l.status === 'pending' ? '번역 중…' : '(번역 없음)');
      const src = document.createElement('span');
      src.className = 'src';
      src.textContent = l.src || '(읽은 글자 없음)';
      item.append(tgt, src);
      const label = l.edited ? '직접 고침' : STATE_LABEL[l.status] || (l.overflow ? '글이 넘침' : '');
      if (label) {
        const tag = document.createElement('span');
        tag.className = `state${l.edited ? ' edited' : ''}`;
        tag.textContent = label;
        item.append(tag);
      }
      list.append(item);
    }
    if (!script.shown.length) {
      const empty = document.createElement('p');
      empty.className = 'script-empty';
      empty.textContent = lines.length ? '조건에 맞는 대사가 없어요.' : '아직 번역된 대사가 없어요.';
      list.append(empty);
    }
    list.scrollTop = scrollTop;
    script.panel.querySelector('[data-ref="count"]').textContent = `${script.shown.length} / ${lines.length}줄`;
    script.panel.querySelectorAll('[data-filter]').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.filter === script.filter)));
  }

  function toggleScript(open) {
    const want = open === undefined ? !script : !!open;
    if (!want) {
      script?.panel.remove();
      script = null;
      return;
    }
    if (script) return;
    const panel = document.createElement('aside');
    panel.className = 'script';
    panel.setAttribute('aria-label', '대사 목록');
    panel.innerHTML = `
      <div class="script-head"><b>대사 목록</b><span class="muted" data-ref="count"></span>
        <button class="x" data-act="close" title="닫기 (Esc)">${ICON.close}</button></div>
      <input data-ref="query" type="search" placeholder="원문·번역에서 찾기" />
      <div class="chips" role="radiogroup" aria-label="보기">
        <button role="radio" data-filter="all">전체</button>
        <button role="radio" data-filter="issues">확인 필요</button>
        <button role="radio" data-filter="edited">직접 고침</button>
      </div>
      <div class="script-list" data-ref="list"></div>
      <div class="script-foot">
        <button class="btn" data-act="copyTgt" title="지금 보이는 대사의 번역만 복사">번역 복사</button>
        <button class="btn" data-act="copyBoth" title="지금 보이는 대사의 원문과 번역을 복사">원문+번역 복사</button>
      </div>`;
    wrap.appendChild(panel);
    script = { panel, filter: 'all', query: '', sig: '', shown: [] };
    const query = panel.querySelector('[data-ref="query"]');
    query.addEventListener('input', () => {
      script.query = query.value.trim().toLowerCase();
      renderScript(true);
    });
    panel.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') toggleScript(false);
    });
    panel.addEventListener('click', (ev) => {
      const chip = ev.target.closest('[data-filter]');
      if (chip) {
        script.filter = chip.dataset.filter;
        return renderScript(true);
      }
      const act = ev.target.closest('[data-act]')?.dataset.act;
      if (act === 'close') return toggleScript(false);
      if (act === 'copyTgt' || act === 'copyBoth') return copyScript(act === 'copyBoth');
      const key = ev.target.closest('[data-line]')?.dataset.line;
      const line = key && script.shown.find((l) => l.key === key);
      if (line) {
        focusLine(line.e, line.sid);
        renderScript(true);
      }
    });
    renderScript(true);
    query.focus();
  }

  async function copyScript(withSource) {
    const lines = script.shown.filter((l) => withSource || l.tgt);
    const text = withSource
      ? lines.map((l) => `${l.src}\n→ ${l.tgt}`).join('\n\n')
      : lines.map((l) => l.tgt).join('\n');
    try {
      await navigator.clipboard.writeText(text);
      toast(`${lines.length}줄을 복사했어요.`);
    } catch {
      toast('복사하지 못했어요. 페이지를 한 번 누른 뒤 다시 시도해 주세요.');
    }
  }

  // ---------- region editor & glossary ----------
  function wholeImage(e) {
    const a = e.anchor;
    return a.type === "element" && a.fx === 0 && a.fy === 0 && a.fw === 1 && a.fh === 1 && !!e.resubmit;
  }

  function openEditor(e) {
    const snap = e.lastSnap;
    if (!snap?.image_hash || !wholeImage(e)) {
      return toast("화면 캡처로 번역한 부분은 영역을 저장할 수 없어요. 원본 이미지를 읽은 경우에만 쓸 수 있어요.");
    }
    if (!FINISHED.has(e.status)) return toast("이 이미지 번역이 끝난 뒤에 편집할 수 있어요.");
    closePopover();
    setPeek(false);
    editor?.close();
    const save = async (layout, message) => {
      try {
        await send("saveLayout", { image_hash: snap.image_hash, src: snap.src, layout });
      } catch (err) {
        toast(`영역을 저장하지 못했어요: ${explain(err.message)}`);
        return false;
      }
      if (alive(e)) reprocess(e, message);
      return true;
    };
    editor = BalloonLensEditor.open({
      mount: wrap, layer: e.layer, snap,
      onSave: (layout) => save(layout, layout ? "영역을 저장했어요. 이 이미지를 다시 번역하는 중…" : "영역 수정을 지웠어요. 다시 번역하는 중…"),
      onReset: () => save(null, "처음 검출한 영역으로 되돌렸어요. 다시 번역하는 중…"),
      onClose: () => { editor = null; },
    });
  }

  // The image the reader is looking at: largest visible finished original.
  function editVisibleImage() {
    let best = null, bestArea = 0;
    for (const [img, rec] of images) {
      if (!rec.main?.lastSnap) continue;
      const r = img.getBoundingClientRect();
      const area = Math.max(0, Math.min(innerHeight, r.bottom) - Math.max(0, r.top)) *
        Math.max(0, Math.min(innerWidth, r.right) - Math.max(0, r.left));
      if (area > bestArea) [best, bestArea] = [rec.main, area];
    }
    if (!best) return toast("편집할 이미지가 화면에 없어요. 원본 이미지 번역이 끝난 뒤에 쓸 수 있어요.");
    openEditor(best);
  }

  function reprocess(e, message) {
    for (const el of e.segs.values()) {
      el._cleanup?.remove();
      el.remove();
    }
    e.segs.clear();
    e.overrides.clear();
    Object.assign(e, { lastSnap: null, jobId: null, retries: 0, status: "new" });
    toast(message);
    e.resubmit();
  }

  async function addTerm(source, target) {
    const work = BalloonLensReading.workSettings(location.href, settings.workProfiles);
    if (!work.matched) {
      toast("작품을 등록하면 용어를 저장해서 다음 페이지에도 쓸 수 있어요.",
        { label: "작품 등록", run: () => send("openWorkSettings", { url: location.href }) });
      return false;
    }
    const { workProfiles = [] } = await chrome.storage.local.get("workProfiles");
    const profile = workProfiles.find((p) => p.url === work.url);
    if (!profile) throw new Error("작품 설정을 찾지 못했어요");
    // One entry per source spelling: a new choice replaces the old one.
    const lines = String(profile.glossary || "").split("\n")
      .filter((line) => line.trim() && line.split(/=|->|→/)[0].trim() !== source);
    lines.push(`${source} = ${target}`);
    const glossary = lines.join("\n");
    if (glossary.length > 4000) throw new Error("용어집이 4000자를 넘어요. 설정 화면에서 정리해 주세요");
    profile.glossary = glossary;
    await chrome.storage.local.set({ workProfiles }); // storage change re-translates this page with the term
    toast(`용어를 추가했어요: ${source} = ${target} · 이 페이지를 다시 번역해요.`);
    return true;
  }

  function toast(message, action) {
    shadow.querySelector(".toast")?.remove();
    clearTimeout(toastTimer);
    const t = document.createElement("div");
    t.className = "toast";
    t.setAttribute("role", "status");
    const span = document.createElement("span");
    span.textContent = message;
    t.append(span);
    if (action) {
      const b = document.createElement("button");
      b.textContent = action.label;
      b.addEventListener("click", () => {
        t.remove();
        action.run();
      });
      t.append(b);
    }
    wrap.appendChild(t);
    toastTimer = setTimeout(() => t.remove(), action ? 12000 : 4500);
  }

  // ---------- polling ----------
  async function pollLoop(myLoop) {
    while (mounted && myLoop === loopId) {
      for (const e of [...entries]) {
        if (!e.jobId || FINISHED.has(e.status) || myLoop !== loopId) continue;
        try {
          const snap = await send("getJob", { jobId: e.jobId });
          helperDown = 0;
          downToastShown = false;
          apply(e, snap);
          if (FINISHED.has(snap.status)) pump();
          if (FINISHED.has(snap.status)) console.debug("[BalloonLens]", snap.status, { fetchMs: e.fetchMs, ...snap.timings });
        } catch (err) {
          if (err.message === "not_found" && e.resubmit && e.retries < 1) {
            e.retries++; // helper restarted and lost the job; its OCR cache makes a resubmit cheap
            e.jobId = null;
            e.resubmit();
          } else if (err.message === "helper_unreachable" || err.message === "helper_timeout") {
            helperDown++;
            break;
          } else fail(e, err.message);
        }
      }
      if (helperDown > 6 && !downToastShown) {
        downToastShown = true;
        toast("도우미 연결이 끊겼어요.", {
          label: "다시 켜기",
          run: () => send("startHelper").then(() => toast("도우미를 다시 켰어요."), (err) => toast(explain(err.message))),
        });
      }
      updatePill();
      await sleep(helperDown > 6 ? 3000 : POLL_MS);
    }
  }

  function updatePill() {
    const s = status();
    const total = s.done + s.pending + s.failed;
    const busy = s.jobsActive > 0 || waiting.length > 0 || submitting > 0 || captureBusy || s.retrying;
    let label;
    if (s.helperDown) label = "도우미 연결 끊김";
    else if (busy) label = total ? `번역 중 ${s.done}/${total}` : "이미지 읽는 중";
    else if (total) label = `번역 ${s.done}개${s.review ? ` · 확인 ${s.review}` : ''}${s.failed ? ` · 실패 ${s.failed}` : ""}`;
    else if (!auto) label = "영역을 선택하세요";
    else label = images.size || Date.now() - mountedAt < 4000 ? "이미지 찾는 중" : "번역할 이미지가 없어요";
    pillLabel.textContent = label;
    pill.title = !auto && !total ? "" : lastError ? `마지막 오류: ${explain(lastError)}` : "";
    pillDot.className = `dot${s.helperDown ? " err" : busy ? " busy" : ""}`;
    pillBar.style.width = total ? `${(s.done / total) * 100}%` : busy ? "12%" : "0%";
    // Once everything is shown, shrink the pill so it covers less of the page.
    if (busy || s.helperDown || !total) idleSince = 0;
    else if (!idleSince) idleSince = Date.now();
    pill.classList.toggle("compact", !!idleSince && Date.now() - idleSince > 2500);
    const state = s.helperDown ? "error" : busy ? "busy" : "done";
    if (mounted && state !== lastProgress) {
      lastProgress = state;
      send("progress", { state }).catch(() => {});
    }
    renderScript();
  }

  async function tick() {
    while (mounted) {
      if (location.href !== href) {
        href = location.href; // SPA navigation: drop results for the previous screen
        restart();
      }
      for (const [img, rec] of images) if (!img.isConnected) dropRecord(img, rec);
      entries.forEach(place);
      await captureTick();
      updatePill();
      await sleep(700);
    }
  }
})();
