const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search); // ?tabId= lets tests open the popup as a normal page
const ALL_URLS = { origins: ["<all_urls>"] };
const DEFAULTS = { src: "ja", tgt: "ko", model: "claude-haiku-5-5", reading: "rtl", autoSites: [] };
const MODEL_INFO = { "translategemma:4b": "TranslateGemma 4B · 3.3GB", "qwen3:8b": "Qwen3 8B · 5.2GB", "qwen3.5:9b": "Qwen3.5 9B · 6.6GB" };
const RESTRICTED = /^(chrome|edge|about|chrome-extension|devtools|view-source):|^https:\/\/(chromewebstore\.google\.com|chrome\.google\.com\/webstore)/;
const explain = (code) => BalloonLensReading.explainCode(code);

let tab = null;
let tabState = null;
let busy = false;

async function call(type, payload = {}) {
  const res = await chrome.runtime.sendMessage({ type, ...payload });
  if (!res?.ok) throw new Error(res?.error || "no_response");
  return res.data;
}

function setConn(text, kind) {
  $("conn").textContent = text;
  $("conn").className = `badge ${kind || ""}`;
}

function showNotice(kind, title, text = "", button = null, progress = null) {
  const n = $("notice");
  if (!kind) return void (n.hidden = true);
  n.hidden = false;
  n.className = `card notice ${kind}`;
  $("noticeTitle").textContent = title;
  $("noticeText").textContent = text;
  const btn = $("noticeBtn");
  btn.hidden = !button;
  if (button) {
    btn.textContent = button.label;
    btn.onclick = button.run;
  }
  $("noticeBar").hidden = progress == null;
  if (progress != null) $("noticeBar").firstElementChild.style.width = `${Math.round(progress * 100)}%`;
}

const currentModel = () => document.querySelector('[data-field="model"] [aria-checked="true"]')?.dataset.value;
const host = () => {
  try {
    return new URL(tab.url).hostname;
  } catch {
    return "";
  }
};

async function init() {
  tab = params.has("tabId")
    ? { id: Number(params.get("tabId")), url: params.get("url") || "" }
    : (await chrome.tabs.query({ active: true, currentWindow: true }))[0];
  if (params.has("tabId") && !tab.url) tab.url = (await chrome.tabs.get(tab.id).catch(() => ({}))).url || "";
  const saved = { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };

  for (const f of ["src", "tgt"]) {
    $(f).value = saved[f];
    $(f).addEventListener("change", () => chrome.storage.local.set({ [f]: $(f).value }));
  }
  document.querySelectorAll(".seg-ctl").forEach((group) => {
    const field = group.dataset.field;
    const mark = (value) => group.querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.value === value)));
    mark(saved[field]);
    group.addEventListener("click", async (ev) => {
      const b = ev.target.closest("button");
      if (!b) return;
      mark(b.dataset.value);
      await chrome.storage.local.set({ [field]: b.dataset.value });
      if (field === "model") refreshSetup();
    });
  });
  renderSite(saved.autoSites);
  async function renderFont() {
    const {fontScale=1}=await chrome.storage.local.get('fontScale');
    $('fontReset').textContent=`${Math.round(fontScale*100)}%`;
    $('fontDown').disabled=fontScale<=.7;
    $('fontUp').disabled=fontScale>=1.5;
  }
  for (const [id,action] of [['fontDown','down'],['fontUp','up'],['fontReset','reset']]) {
    $(id).addEventListener('click',async()=>{
      const {fontScale=1}=await chrome.storage.local.get('fontScale');
      await chrome.storage.local.set({fontScale:BalloonLensReading.nextFontScale(fontScale,action)});
      await renderFont();
    });
  }
  await renderFont();
  $('workSettings').addEventListener('click', () => chrome.tabs.create({url:chrome.runtime.getURL('options.html')+'?workUrl='+encodeURIComponent(tab.url || '')}));
  const {workProfiles=[]} = await chrome.storage.local.get('workProfiles');
  const work = BalloonLensReading.workSettings(tab.url || '', workProfiles);
  $('workSettings').textContent = work.matched ? '이 작품의 말투·용어·기억' : '이 작품 등록하기';
  $('workState').textContent = work.matched
    ? `작품 기억 켜짐 · ${work.url}`
    : '등록하면 이름·말투와 앞 페이지 대사를 기억해서 번역에 참고해요';

  const cmds = await chrome.commands.getAll();
  const toggleKey = cmds.find((c) => c.name === "toggle-translate")?.shortcut;
  $("openShortcuts").textContent = toggleKey ? `단축키 ${toggleKey}` : "단축키 설정";

  await Promise.all([refreshSetup(), refreshTab()]);
  setInterval(refreshTab, 800);
  setInterval(() => { if (!busy) refreshSetup(); }, 5000);
}

function renderSite(autoSites) {
  const h = host();
  const restricted = RESTRICTED.test(tab.url || "");
  $("siteHost").textContent = restricted ? "이 페이지에서는 쓸 수 없어요" : h || "처음 켤 때 사이트 접근을 허용해 주세요";
  $("autoSite").checked = !!h && autoSites.includes(h);
  $("autoSite").disabled = restricted;
}

async function refreshSetup() {
  let setup;
  try {
    setup = await call("setup");
  } catch (e) {
    setup = { error: e.message };
  }
  const s = setup.helper;
  if (!s) {
    $("subCard").hidden = true;
    if (setup.error === "helper_unreachable" && setup.native) {
      setConn("꺼짐", "warn");
      showNotice("warn", "도우미가 꺼져 있어요", "번역을 켜면 알아서 켜져요.", { label: "지금 켜기", run: startHelper });
    } else if (setup.error === "helper_unreachable") {
      setConn("설정 필요", "err");
      showNotice("err", "처음 설정이 필요해요", "도우미를 한 번 설치해야 해요. 설치 창(BalloonLens-Setup.exe)을 실행해 주세요.", { label: "설정 열기", run: openOptions });
    } else {
      setConn("연결 오류", "err");
      showNotice("err", "도우미와 연결하지 못했어요", explain(setup.error), { label: "설정 열기", run: openOptions });
    }
    return;
  }
  const current = currentModel();
  if (BalloonLensReading.isSubscription(current)) return renderSubscription(current);
  $("subCard").hidden = true;
  const model = s.models.find((m) => m.name === current);
  if (!s.ollama) {
    setConn("Ollama 꺼짐", "warn");
    showNotice("warn", "Ollama가 꺼져 있어요", "로컬 모델을 쓰려면 Ollama 앱을 실행하세요. 구독 번역으로 바꿀 수도 있어요.");
  } else if (model && !model.installed) {
    setConn("모델 없음", "warn");
    showNotice("warn", "이 모드의 번역 모델이 아직 없어요", MODEL_INFO[model.name], { label: "받기", run: () => pullModel(model.name) });
  } else {
    setConn(s.ocr_ready ? "준비됨" : "준비 중", s.ocr_ready ? "ok" : "warn");
    showNotice(null);
  }
}

// Subscription status: consent first, then a pause from the usage limit, then the CLI's own state.
async function renderSubscription(model) {
  const info = BalloonLensReading.MODEL_INFO[model];
  const data = await call("backends").catch(() => null);
  const row = data?.backends?.find((b) => b.id === info.backend);
  $("subCard").hidden = false;
  $("subTitle").textContent = info.label;
  $("consentBox").hidden = true;
  $("pausedBox").hidden = true;
  if (!row) {
    $("subState").textContent = "";
    $("subUsage").textContent = "";
    return setConn("확인 중", "warn");
  }
  const [text, kind] = BalloonLensReading.stateLabel(row.state);
  $("subState").textContent = text;
  $("subState").className = `badge ${kind}`;
  const u = row.usage || {};
  const caps = row.caps || {};
  $("subUsage").textContent = `오늘 ${u.calls_today ?? 0}회 · 5시간 ${u.calls_5h ?? 0}/${caps.per_5h ?? "-"} · 7일 ${u.calls_7d ?? 0}/${caps.per_7d ?? "-"}`;
  if (!row.consent?.current) {
    $("consentList").replaceChildren(...BalloonLensReading.CONSENT_TEXT.map((line) => {
      const li = document.createElement("li");
      li.textContent = line;
      return li;
    }));
    $("consentBox").hidden = false;
    return setConn("동의 필요", "warn");
  }
  if (row.paused) {
    const minutes = Math.max(1, Math.round(row.paused.retry_in_s / 60));
    $("pausedText").textContent = `${row.paused.reason}. 약 ${minutes}분 뒤 다시 시도할 수 있어요. 그동안은 로컬 모델로 이어서 번역할 수 있어요.`;
    $("pausedBox").hidden = false;
    return setConn("한도 대기", "warn");
  }
  if (row.state === "ready") return setConn("준비됨", "ok");
  setConn(text, kind);
  // A sandbox, key or login-method problem will not fix itself: offer the local model. A missing login needs settings.
  const localFirst = ["sandbox_failed", "api_key_detected", "unsupported"].includes(row.state);
  showNotice(kind === "err" ? "err" : "warn", `${info.label}을 쓸 수 없어요`, explain(row.state),
    localFirst ? { label: "로컬로 쓰기", run: useLocalModel } : { label: "설정 열기", run: openOptions });
}

async function startHelper() {
  showNotice("warn", "도우미 켜는 중…", "처음엔 몇 초 걸려요.");
  try {
    await call("startHelper");
    refreshSetup();
  } catch (e) {
    showNotice("err", "도우미를 켜지 못했어요", explain(e.message), { label: "설정 열기", run: openOptions });
  }
}

async function pullModel(name) {
  try {
    await call("pullModel", { name });
  } catch (e) {
    return showNotice("err", "모델을 받지 못했어요", explain(e.message));
  }
  const timer = setInterval(async () => {
    const st = await call("pullStatus", { name }).catch(() => null);
    if (!st) return;
    if (st.state === "pulling") {
      showNotice("warn", "모델 받는 중…", `${MODEL_INFO[name]} · ${st.status || ""}`, null, st.total ? st.completed / st.total : 0);
    } else {
      clearInterval(timer);
      st.state === "done" ? refreshSetup() : showNotice("err", "모델을 받지 못했어요", st.error || "");
    }
  }, 700);
}

function setBusyButton(btn, text) {
  const spin = document.createElement("span");
  spin.className = "spin";
  btn.replaceChildren(spin, text);
}

async function refreshTab() {
  const restricted = RESTRICTED.test(tab.url || "");
  tabState = restricted ? null : await call("tabState", { tabId: tab.id }).catch(() => null);
  if (!busy) {
    const t = $("toggle");
    t.disabled = restricted;
    t.textContent = restricted ? "이 페이지는 번역할 수 없어요" : tabState?.running ? "번역 끄기" : "번역 켜기";
    t.classList.toggle("primary", !tabState?.running);
  }
  $("region").disabled = restricted;
  $("peek").disabled = !tabState?.mounted;
  $("peek").setAttribute("aria-pressed", String(!!tabState?.peek));
  $('retryFailed').disabled=busy || !!tabState?.retrying || !tabState?.mounted || !(tabState.failed || tabState.failedJobs);
  $('nextIssue').disabled=!tabState?.mounted || !(tabState.review || tabState.failed || tabState.overflow);
  $('scriptList').disabled=restricted;
  $('editRegions').disabled=!tabState?.mounted;
  if (tabState?.retrying) $('quickStatus').textContent='실패한 대사를 다시 번역하고 있어요…';

  const p = $("progress");
  if (!tabState?.mounted) return void (p.hidden = true);
  p.hidden = false;
  const total = tabState.done + tabState.pending + tabState.failed;
  const working = tabState.pending > 0 || tabState.jobsActive > 0;
  $("progressTitle").textContent = tabState.helperDown ? "도우미 연결 끊김" : working ? "번역 중" : tabState.failed || tabState.failedJobs ? '일부 번역 실패' : tabState.review ? '확인할 대사가 있어요' : total ? "이 페이지 번역 완료" : "이미지 찾는 중";
  const parts = total ? [`${tabState.done} / ${total}`] : [];
  if (tabState.failed) parts.push(`실패 ${tabState.failed}`);
  if (tabState.edited) parts.push(`고침 ${tabState.edited}`);
  if (tabState.review) parts.push(`확인 ${tabState.review}`);
  $("progressText").textContent = parts.join(" · ");
  $("progressBar").style.width = total ? `${Math.round((tabState.done / total) * 100)}%` : "0%";
  p.classList.toggle("error", !!tabState.helperDown);
}

async function run(type, btn, label) {
  busy = true;
  setBusyButton(btn, label);
  try {
    await call(type, { tabId: tab.id });
    if (!params.has("tabId")) window.close();
  } catch (e) {
    showNotice("err", type === "region" ? "영역 번역을 시작하지 못했어요" : "번역을 켜지 못했어요", explain(e.message));
  } finally {
    busy = false;
    $("region").lastChild.textContent = " 영역 번역";
    refreshTab();
    refreshSetup();
  }
}

// permissions.request must run inside the click gesture, before any other await.
$("toggle").addEventListener("click", async () => {
  if (tabState?.running) {
    await call("stop", { tabId: tab.id });
    return refreshTab();
  }
  if (!(await chrome.permissions.request(ALL_URLS))) return;
  run("start", $("toggle"), "켜는 중…");
});

$("region").addEventListener("click", async () => {
  if (!(await chrome.permissions.request(ALL_URLS))) return;
  run("region", $("region"), "준비 중…");
});

$("peek").addEventListener("click", async () => {
  await call("peek", { tabId: tab.id, on: !tabState?.peek });
  refreshTab();
});

$("autoSite").addEventListener("change", async (ev) => {
  const on = ev.target.checked;
  if (on && !(await chrome.permissions.request(ALL_URLS))) return void (ev.target.checked = false);
  if (!tab.url) tab = { ...tab, url: (await chrome.tabs.get(tab.id)).url };
  const h = host();
  if (!h) return void (ev.target.checked = false);
  const { autoSites = [] } = await chrome.storage.local.get("autoSites");
  const next = on ? [...new Set([...autoSites, h])] : autoSites.filter((x) => x !== h);
  await chrome.storage.local.set({ autoSites: next });
  renderSite(next);
  if (on && !tabState?.running) run("start", $("toggle"), "켜는 중…");
});

function openOptions() {
  chrome.runtime.openOptionsPage();
}
// Switching to the local model reuses the normal radio handler, so the choice is saved the same way.
function useLocalModel() {
  document.querySelector('[data-field="model"] [data-value="qwen3.5:9b"]').click();
}
$("openOptions").addEventListener("click", (e) => {
  e.preventDefault();
  openOptions();
});
$("openShortcuts").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: "chrome://extensions/shortcuts" });
});

$("consentAccept").addEventListener("click", async () => {
  const { backend } = BalloonLensReading.MODEL_INFO[currentModel()];
  try {
    await call("acceptConsent", { name: backend, version: BalloonLensReading.CONSENT_VERSION });
  } catch (e) {
    showNotice("err", "동의를 저장하지 못했어요", explain(e.message));
  }
  refreshSetup();
});
// Switching to the local model reuses the normal radio handler, so the choice is saved the same way.
$("pausedLocal").addEventListener("click", useLocalModel);
$("pausedRetry").addEventListener("click", async () => {
  const { backend } = BalloonLensReading.MODEL_INFO[currentModel()];
  try {
    await call("resumeBackend", { name: backend });
  } catch (e) {
    showNotice("err", "다시 시도하지 못했어요", explain(e.message));
  }
  refreshSetup();
});

init();

// The list opens on the page (turning translation on if needed), so the popup gets out of the way.
$('scriptList').addEventListener('click', async () => {
  if (!(await chrome.permissions.request(ALL_URLS))) return;
  try {
    await call('script', {tabId: tab.id, open: true});
    if (!params.has('tabId')) window.close();
  } catch (e) { $('quickStatus').textContent = explain(e.message); }
});
$('editRegions').addEventListener('click', async () => {
  await call('editRegions', {tabId: tab.id}).catch(() => null);
  if (!params.has('tabId')) window.close();
});

for (const type of ['retryFailed','nextIssue']) {
  $(type).addEventListener('click', async()=>{
    $(type).disabled=true;
    try {
      const result=await call(type,{tabId:tab.id});
      $('quickStatus').textContent=result?.message || '';
      if (type==='nextIssue' && result?.found && !params.has('tabId')) window.close();
    } catch(e) { $('quickStatus').textContent=explain(e.message); }
    finally { refreshTab(); }
  });
}
