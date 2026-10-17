const $ = (id) => document.getElementById(id);
const ALL_URLS = { origins: ["<all_urls>"] };
const DISPLAY = { fontScale: 1, bgOpacity: 0.92, showSource: false };
const MODELS = [
  { name: "translategemma:4b", label: "TranslateGemma 4B", size: "3.3GB", role: "빠르게 · 한 줄 번역" },
  { name: "qwen3:8b", label: "Qwen3 8B", size: "5.2GB", role: "페이지 문맥" },
  { name: "qwen3.5:9b", label: "Qwen3.5 9B", size: "6.6GB", role: "페이지 문맥 + 검수" },
];
const explain = (code) => BalloonLensReading.explainCode(code);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function call(type, payload = {}) {
  const res = await chrome.runtime.sendMessage({ type, ...payload });
  if (!res?.ok) throw new Error(res?.error || "no_response");
  return res.data;
}

function button(label, onClick, primary = false) {
  const b = document.createElement("button");
  b.className = `btn${primary ? " primary" : ""}`;
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

function setStep(id, state, desc, action) {
  const li = $(id);
  li.classList.remove("ok", "todo", "wait");
  li.classList.add(state);
  li.querySelector(".icon").textContent = state === "ok" ? "✓" : li.dataset.n;
  if (desc) li.querySelector(".desc").textContent = desc;
  li.querySelector(".act").replaceChildren(...(action ? [action] : []));
}

// ---------- setup checklist ----------
let demoDone = false;
const pulling = new Set();

async function refresh() {
  const setup = await call("setup", { checkNative: true }).catch((e) => ({ error: e.message }));
  const h = setup.helper;
  const backends = h ? await call("backends").then((d) => d.backends || []).catch(() => []) : [];

  const nativeDetail = $("step-native").querySelector(".detail");
  if (setup.native) {
    setStep("step-native", "ok", "설치됨 · 크롬에서 필요할 때 알아서 켜요.");
    nativeDetail.hidden = true;
  } else {
    setStep("step-native", "todo", h ? "지금은 직접 실행한 도우미를 쓰는 중이에요. 자동으로 켜지게 하려면 설치하세요." : "번역은 이 PC의 도우미 프로그램이 해요. 한 번만 설치하면 돼요.", button("다시 확인", refresh));
    nativeDetail.hidden = false;
  }

  if (h) {
    setStep("step-helper", "ok", `실행 중 · v${h.version}${h.ocr_ready ? "" : " · 글자 인식 모델 올리는 중"}`);
  } else if (setup.native) {
    setStep("step-helper", "todo", "꺼져 있어요. 번역을 켜면 알아서 켜져요.", button("지금 켜기", startHelper, true));
  } else {
    setStep("step-helper", "wait", explain(setup.error || "helper_unreachable"));
  }

  renderModels(h, backends);

  if (setup.permission) setStep("step-permission", "ok", "허용됨 · 이미지는 이 PC 밖으로 나가지 않아요.");
  else setStep("step-permission", "todo", null, button("허용", grant, true));

  setStep("step-demo", demoDone ? "ok" : h ? "todo" : "wait", null, button(demoDone ? "다시 해보기" : "샘플 번역", runDemo, !demoDone && !!h));

  const subReady = backends.some((b) => b.state === "ready");
  const localReady = !!h?.ollama && h.models.some((m) => m.installed && m.kind !== "subscription");
  const ready = !!h && setup.permission && (subReady || localReady);
  const summary = $("setupSummary");
  summary.textContent = ready ? "사용할 준비가 됐어요" : "설정이 남아 있어요";
  summary.className = `badge ${ready ? "ok" : "warn"}`;
}

const BACKEND_TITLE = { claude: "Claude Code 구독 · Haiku 5.5" };
const LOGIN_HELP = {
  claude: {
    cmd: '$env:CLAUDE_CONFIG_DIR = "$env:LOCALAPPDATA\\BalloonLens\\claude-config"; claude auth login',
    note: "PowerShell에 한 줄로 붙여 넣고 실행하세요. 브라우저에서 Claude 계정으로 로그인하면 돼요. 이 폴더는 번역 전용이라 개인 설정(CLAUDE.md)을 읽지 않아요.",
  },
};
const NEEDS_LOGIN = ["not_installed", "not_logged_in", "api_key_detected", "unsupported"];

function labeled(text, input) {
  const label = document.createElement("label");
  label.className = "field-inline";
  const span = document.createElement("span");
  span.textContent = text;
  label.append(span, input);
  return label;
}

// Runs one action, shows a failure, and redraws the cards either way.
async function attempt(fn, failTitle) {
  try {
    return await fn();
  } catch (e) {
    alert(`${failTitle}: ${explain(e.message)}`);
    return null;
  } finally {
    refresh();
  }
}

function loginHelp(id) {
  const info = LOGIN_HELP[id];
  const box = document.createElement("div");
  box.className = "copy-box";
  const note = document.createElement("p");
  note.className = "muted";
  note.textContent = info.note;
  const code = document.createElement("code");
  code.textContent = info.cmd;
  const copy = button("명령 복사", async () => {
    try {
      await navigator.clipboard.writeText(info.cmd);
      copy.textContent = "복사했어요";
    } catch {
      copy.textContent = "복사하지 못했어요. 직접 선택해 주세요";
    }
  });
  box.append(note, code, copy);
  return box;
}


function usageLine(b) {
  const p = document.createElement("p");
  p.className = "muted subscription-usage";
  p.dataset.backend = b.id;
  p.textContent = BalloonLensReading.subscriptionUsageText(b.subscription_usage);
  return p;
}

function backendCard(b) {
  const card = document.createElement("div");
  card.className = "backend-card";
  const [stateText, kind] = BalloonLensReading.stateLabel(b.state);
  const head = document.createElement("div");
  head.className = "row";
  const title = document.createElement("b");
  title.textContent = BACKEND_TITLE[b.id] || b.label;
  const badge = document.createElement("span");
  badge.className = `badge ${kind}`;
  badge.textContent = stateText;
  head.append(title, badge);
  const detail = document.createElement("p");
  detail.className = "muted";
  detail.textContent = b.detail || "";
  detail.hidden = !b.detail;
  const body = document.createElement("div");
  body.className = "bc-body";
  const act = document.createElement("div");
  act.className = "bc-act";

  if (NEEDS_LOGIN.includes(b.state)) body.append(loginHelp(b.id));
 if (b.paused) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = `${b.paused.reason} · 약 ${Math.max(1, Math.round(b.paused.retry_in_s / 60))}분 뒤 다시 시도할 수 있어요`;
    body.append(p);
    act.append(button("일시정지 풀기", () => attempt(() => call("resumeBackend", { name: b.id }), "풀지 못했어요")));
  }
  act.append(button("다시 확인", () => attempt(() => call("checkBackend", { name: b.id }), "확인하지 못했어요")));
  body.append(usageLine(b));
  card.append(head, detail, body, act);
  return card;
}

function renderModels(h, backends = []) {
  const box = $("models");
  box.replaceChildren();
  if (!h) return setStep("step-models", "wait", "도우미가 켜지면 확인할 수 있어요.");
  for (const b of backends) box.append(backendCard(b));

  const title = document.createElement("p");
  title.className = "field-label";
  title.textContent = "로컬 모델 · 선택 사항 (Ollama)";
  box.append(title);
  if (!h.ollama) {
    const row = document.createElement("div");
    row.className = "model-row";
    const text = document.createElement("small");
    text.className = "muted";
    text.textContent = "Ollama가 꺼져 있거나 설치돼 있지 않아요. 로컬 모델은 쓰지 않아도 돼요.";
    row.append(text, button("Ollama 받기", () => chrome.tabs.create({ url: "https://ollama.com/download" })));
    box.append(row);
  } else {
    for (const m of MODELS) {
      const installed = h.models.find((x) => x.name === m.name)?.installed;
      const row = document.createElement("div");
      row.className = "model-row";
      row.innerHTML = `<div><b></b><small class="muted"></small></div><div class="model-act"></div>`;
      row.querySelector("b").textContent = m.label;
      row.querySelector("small").textContent = `${m.role} · ${m.size}`;
      const act = row.querySelector(".model-act");
      if (installed) {
        const badge = document.createElement("span");
        badge.className = "badge ok";
        badge.textContent = "설치됨";
        act.append(badge);
      } else if (pulling.has(m.name)) {
        act.innerHTML = '<div class="bar"><i></i></div>';
      } else {
        act.append(button("받기", () => pull(m.name, row)));
      }
      box.append(row);
    }
  }
  const subReady = backends.some((b) => b.state === "ready");
  const localReady = h.ollama && h.models.some((m) => m.installed && m.kind !== "subscription");
  setStep("step-models", subReady || localReady ? "ok" : "todo",
    subReady ? "구독 번역을 쓸 수 있어요." : localReady ? "로컬 모델이 준비됐어요." : "Claude에 로그인하거나 로컬 모델을 하나 받아 주세요.");
}

async function pull(name, row) {
  pulling.add(name);
  const act = row.querySelector(".model-act");
  act.innerHTML = '<div class="bar"><i></i></div><small class="muted">시작하는 중…</small>';
  try {
    await call("pullModel", { name });
    for (;;) {
      await sleep(800);
      const st = await call("pullStatus", { name });
      if (st.state === "pulling") {
        act.querySelector("i").style.width = st.total ? `${Math.round((st.completed / st.total) * 100)}%` : "5%";
        act.querySelector("small").textContent = st.status || "";
        continue;
      }
      if (st.state === "error") throw new Error(st.error);
      break;
    }
  } catch (e) {
    act.replaceChildren(button("다시 받기", () => pull(name, row)));
    alert(`모델을 받지 못했어요: ${explain(e.message)}`);
  } finally {
    pulling.delete(name);
    refresh();
  }
}

async function startHelper(ev) {
  const b = ev.currentTarget;
  b.disabled = true;
  b.textContent = "켜는 중…";
  try {
    await call("startHelper");
  } catch (e) {
    setStep("step-helper", "todo", explain(e.message), button("다시 시도", startHelper, true));
    return;
  }
  refresh();
}

async function grant() {
  await chrome.permissions.request(ALL_URLS);
  refresh();
}

// ---------- demo ----------
async function runDemo() {
  const area = $("demo");
  area.hidden = false;
  const stage = area.querySelector(".demo-stage");
  const img = stage.querySelector("img");
  img.src = chrome.runtime.getURL("assets/demo.png");
  stage.querySelectorAll(".seg, .cleanup").forEach((n) => n.remove());
  const status = $("demoStatus");
  status.textContent = "번역하는 중… 처음엔 모델을 올리느라 10~20초 걸릴 수 있어요.";
  const { model = "qwen3.5:9b" } = await chrome.storage.local.get("model");
  const t0 = performance.now();
  try {
    const { jobId } = await call("translateImage", {
      url: img.src,
      opts: { src: "ja", tgt: "ko", model, reading: "rtl", priority: 1, session: "options-demo", scope: "demo" },
    });
    for (;;) {
      const snap = await call("getJob", { jobId });
      for (const s of snap.segments) {
        let el = stage.querySelector(`[data-id="${s.id}"]`);
        if (!el && snap.width) {
          el = document.createElement("div");
          el.dataset.id = s.id;
          const [l, t, w, h] = BalloonLensReading.sourceFrame(s,snap.width,snap.height).map((v,i)=>v/(i%2 ? snap.height : snap.width));
          Object.assign(el.style, { left: `${l * 100}%`, top: `${t * 100}%`, width: `${w * 100}%`, height: `${h * 100}%` });
          stage.append(el);
          if (s.presentation?.cleanup) {
            const patch = document.createElement('img');
            patch.className = 'cleanup'; patch.src = s.presentation.cleanup;
            const [x,y,w,h] = s.presentation.cleanup_bbox;
            Object.assign(patch.style,{position:'absolute',left:`${x/snap.width*100}%`,top:`${y/snap.height*100}%`,width:`${w/snap.width*100}%`,height:`${h/snap.height*100}%`});
            stage.insertBefore(patch, el);
            el._cleanup = patch;
          }
        }
        if (!el) continue;
        el.className = `seg ${s.status}`;
        el.textContent = s.status === "done" || s.status === "review" ? s.tgt : "";
        if (el._cleanup) { el._cleanup.hidden = !s.tgt; el.style.background = 'transparent'; }
      }
      if (["done", "partial", "failed", "cancelled"].includes(snap.status)) {
        demoDone = snap.status === "done";
        const secs = ((performance.now() - t0) / 1000).toFixed(1);
        status.textContent = demoDone ? `완료 · ${secs}초 · 만화 페이지에서 팝업의 “번역 켜기”를 눌러 보세요.` : `끝남 (${snap.status}) ${explain(snap.error || "")}`;
        break;
      }
      await sleep(400);
    }
  } catch (e) {
    status.textContent = `번역하지 못했어요: ${explain(e.message)}`;
  }
  refresh();
}

// ---------- display ----------
async function initDisplay() {
  const s = { ...DISPLAY, ...(await chrome.storage.local.get(Object.keys(DISPLAY))) };
  for (const key of Object.keys(DISPLAY)) {
    const el = $(key);
    const isCheck = el.type === "checkbox";
    isCheck ? (el.checked = s[key]) : (el.value = s[key]);
    el.addEventListener(isCheck ? "change" : "input", () => {
      chrome.storage.local.set({ [key]: isCheck ? el.checked : Number(el.value) });
      renderPreview();
    });
  }
  renderPreview();
}

function renderPreview() {
  const scale = Number($("fontScale").value);
  const opacity = Number($("bgOpacity").value);
  $("fontScaleVal").textContent = `${Math.round(scale * 100)}%`;
  $("bgOpacityVal").textContent = `${Math.round(opacity * 100)}%`;
  $("pvSeg").style.fontSize = `${17 * scale}px`;
  $("pvSeg").style.background = `rgba(255,255,255,${opacity})`;
  $("pvSrc").hidden = !$("showSource").checked;
}

// ---------- sites & shortcuts ----------
async function renderSites() {
  const { autoSites = [] } = await chrome.storage.local.get("autoSites");
  const list = $("sites");
  list.replaceChildren(
    ...autoSites.map((site) => {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = site;
      li.append(
        name,
        button("빼기", async () => {
          const { autoSites: cur = [] } = await chrome.storage.local.get("autoSites");
          await chrome.storage.local.set({ autoSites: cur.filter((x) => x !== site) });
        })
      );
      return li;
    })
  );
  $("sitesEmpty").hidden = autoSites.length > 0;
}

async function renderShortcuts() {
  const cmds = await chrome.commands.getAll();
  $("shortcuts").replaceChildren(
    ...cmds
      .filter((c) => c.name !== "_execute_action")
      .map((c) => {
        const li = document.createElement("li");
        const desc = document.createElement("span");
        desc.textContent = c.description;
        const key = document.createElement("kbd");
        key.textContent = c.shortcut || "없음";
        li.append(desc, key);
        return li;
      })
  );
}

$("editShortcuts").addEventListener("click", () => chrome.tabs.create({ url: "chrome://extensions/shortcuts" }));
$("repair").addEventListener("click", async () => {
  $("repairResult").textContent = "연결하는 중…";
  try {
    const s = await call("repair");
    $("repairResult").textContent = `다시 연결됐어요 · v${s.version}`;
  } catch (e) {
    $("repairResult").textContent = explain(e.message);
  }
  refresh();
});

chrome.storage.onChanged.addListener((changes) => {
  if ("autoSites" in changes) renderSites();
});

// ---------- backup ----------
$("exportSettings").addEventListener("click", async () => {
  const settings = await chrome.storage.local.get(BalloonLensReading.BACKUP_KEYS);
  const works = {};
  let missed = 0;
  for (const p of settings.workProfiles || []) {
    try {
      works[p.url] = (await call("getWork", { scope: p.url })).work;
    } catch {
      missed++;
    }
  }
  const data = { app: "BalloonLens", version: chrome.runtime.getManifest().version, exported: new Date().toISOString(), settings, works };
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `balloonlens-settings-${new Date().toISOString().slice(0, 10)}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  $("backupStatus").textContent = `저장했어요 · 작품 ${(settings.workProfiles || []).length}개` +
    (missed ? ` (도우미가 꺼져 있어 작품 정보 ${missed}개는 빠졌어요)` : "");
});

$("importSettings").addEventListener("click", () => $("importFile").click());
$("importFile").addEventListener("change", async () => {
  const file = $("importFile").files[0];
  if (!file) return;
  try {
    const restored = BalloonLensReading.validateBackup(JSON.parse(await file.text()));
    const count = restored.settings.workProfiles?.length ?? 0;
    if (!confirm(`설정 파일을 불러올까요? 작품 ${count}개 · 파일에 있는 항목은 지금 설정을 덮어써요.`)) return;
    await chrome.storage.local.set(restored.settings);
    let saved = 0;
    const total = Object.keys(restored.works).length;
    for (const [scope, work] of Object.entries(restored.works)) {
      try {
        await call("saveWork", { scope, work });
        saved++;
      } catch {
        // helper off: settings are still restored
      }
    }
    $("backupStatus").textContent = `불러왔어요 · 작품 정보 ${saved}/${total}개 반영` +
      (restored.skipped.length ? ` · 건너뜀: ${restored.skipped.join(", ")}` : "");
    setTimeout(() => location.reload(), 1200);
  } catch (e) {
    $("backupStatus").textContent = `불러오지 못했어요: ${e.message}`;
  } finally {
    $("importFile").value = "";
  }
});
window.addEventListener("focus", refresh); // e.g. back from running the installer

initDisplay();
renderSites();
renderShortcuts();
refresh();
setInterval(async () => {
  const fields = document.querySelectorAll('.subscription-usage');
  if (!fields.length) return;
  const data = await call('backends').catch(() => null);
  for (const field of fields) {
    const row = data?.backends?.find((b) => b.id === field.dataset.backend);
    field.textContent = BalloonLensReading.subscriptionUsageText(row?.subscription_usage);
  }
}, 5000);

async function initWorks() {
  let {workProfiles = [], vision = false, reviewMode = null} = await chrome.storage.local.get(['workProfiles','vision','reviewMode']);
  const list = $('workList');
  function render() {
    list.replaceChildren(new Option('새 작품', ''), ...workProfiles.map(p => new Option(p.url, p.url)));
  }
  function show(profile = {}) {
    $('workUrl').value = profile.url || '';
    $('workInstructions').value = profile.instructions || '';
    $('workGlossary').value = profile.glossary || '';
    loadMemory(workProfiles.some(p => p.url === profile.url) ? profile.url : '');
  }
  // ----- work memory kept by the helper -----
  let memoryScope = '';
  async function loadMemory(scope) {
    memoryScope = scope;
    for (const id of ['workTitle','workSummary','workCharacters']) $(id).value = '';
    $('suggestions').hidden = true;
    $('suggestWork').disabled = $('clearMemory').disabled = !scope;
    if (!scope) return void ($('memoryStats').textContent = '· 작품을 저장하면 켜져요');
    $('memoryStats').textContent = '· 확인 중';
    try {
      const {work, pages} = await call('getWork', {scope});
      if (memoryScope !== scope) return;
      $('workTitle').value = work.title || '';
      $('workSummary').value = work.summary || '';
      $('workCharacters').value = work.characters || '';
      $('memoryStats').textContent = `· 켜짐 · 기억한 이미지 ${pages.count}개`;
    } catch (e) {
      if (memoryScope === scope) $('memoryStats').textContent = `· 도우미가 켜져 있어야 볼 수 있어요 (${explain(e.message)})`;
    }
  }
  function appendLine(id, line, max) {
    const box = $(id);
    const lines = box.value.split('\n').map(l => l.trim()).filter(Boolean);
    if (lines.includes(line)) return true;
    const next = [...lines, line].join('\n');
    if (next.length > max) {
      $('workStatus').textContent = `${max}자를 넘어서 추가하지 못했어요.`;
      return false;
    }
    box.value = next;
    $('workStatus').textContent = '추가했어요. 저장하면 적용됩니다.';
    return true;
  }
  function suggestion(text, label, apply) {
    const row = document.createElement('div');
    row.className = 'item';
    const span = document.createElement('span');
    span.textContent = text;
    const b = button(label, () => { if (apply() !== false) { b.disabled = true; b.textContent = '추가됨'; } });
    row.append(span, b);
    return row;
  }
  function note(text) {
    const p = document.createElement('p');
    p.className = 'muted';
    p.textContent = text;
    return p;
  }
  $('suggestWork').addEventListener('click', async () => {
    const box = $('suggestions');
    box.hidden = false;
    box.replaceChildren(note('기억한 대사를 읽고 제안을 만드는 중이에요… 모델 응답에 시간이 걸려요.'));
    try {
      const s = await call('suggestWork', {scope: memoryScope});
      if (!s.lines) return void box.replaceChildren(note('아직 기억한 대사가 없어요. 이 작품 페이지를 번역한 뒤 다시 시도하세요.'));
      box.replaceChildren(note(`기억한 대사 ${s.lines}줄을 보고 모델이 추측한 내용이에요. 맞는 것만 추가한 뒤 저장하세요.`));
      if (s.summary) box.append(suggestion(`줄거리: ${s.summary}`, '줄거리에 넣기', () => { $('workSummary').value = s.summary.slice(0, 400); }));
      for (const c of s.characters) {
        const line = `${c.name} = ${c.korean}${c.note ? ` · ${c.note}` : ''}`;
        box.append(suggestion(`인물: ${line}`, '인물에 추가', () => appendLine('workCharacters', line, 800)));
      }
      for (const g of s.glossary) {
        const line = `${g.source} = ${g.target}`;
        box.append(suggestion(`용어: ${line}`, '용어집에 추가', () => appendLine('workGlossary', line, 4000)));
      }
      if (!s.summary && !s.characters.length && !s.glossary.length) box.append(note('확실한 제안을 찾지 못했어요.'));
    } catch (e) {
      box.replaceChildren(note(`제안을 만들지 못했어요: ${explain(e.message)}`));
    }
  });
  $('clearMemory').addEventListener('click', async () => {
    if (!memoryScope || !confirm('이 작품에서 기억한 페이지(원문·번역)를 모두 지울까요? 제목·줄거리·인물과 말투·용어는 유지돼요.')) return;
    try {
      await call('clearWorkPages', {scope: memoryScope});
      $('workStatus').textContent = '기억한 페이지를 비웠어요.';
    } catch (e) { $('workStatus').textContent = explain(e.message); }
    loadMemory(memoryScope);
  });
  $('reviewMode').value = reviewMode ?? '';
  $('reviewMode').addEventListener('change', () => chrome.storage.local.set({reviewMode: $('reviewMode').value || null}));
  render();
  $('speechPreset').addEventListener('change',()=>{
    const text=BalloonLensReading.speechPreset($('speechPreset').value);
    if (text) $('workInstructions').value=text;
    $('workStatus').textContent='지침에 넣었어요. 저장하면 적용됩니다.';
  });
  list.addEventListener('change', () => show(workProfiles.find(p=>p.url===list.value)));
  $('vision').checked = vision;
  $('vision').addEventListener('change', () => chrome.storage.local.set({vision:$('vision').checked}));
  $('saveWork').addEventListener('click', async () => {
    try {
      const url = new URL($('workUrl').value.trim());
      if (!['https:', 'http:'].includes(url.protocol)) throw new Error('http 또는 https 작품 주소를 입력하세요.');
      url.hash = ''; url.search = '';
      const key = url.href.replace(/\/$/, '');
      const profile = {url:key, instructions:$('workInstructions').value.trim(), glossary:$('workGlossary').value.trim()};
      if (key.length>200) throw new Error('작품 주소는 200자 이내로 입력하세요.');
      const current = await chrome.storage.local.get('workProfiles');
      workProfiles = [...(current.workProfiles || []).filter(p=>p.url!==key), profile];
      await chrome.storage.local.set({workProfiles});
      const notes = {title:$('workTitle').value.trim(), summary:$('workSummary').value.trim(), characters:$('workCharacters').value.trim()};
      render(); list.value=key; show(profile);
      try {
        await call('saveWork', {scope:key, work:notes});
        $('workStatus').textContent='저장했어요. 말투·용어는 열린 페이지에 바로, 작품 기억은 다음 번역부터 반영돼요.';
      } catch (e) {
        $('workStatus').textContent='말투·용어는 저장했어요. 제목·줄거리·인물은 도우미가 켜져 있어야 저장돼요.';
      }
      loadMemory(key);
    } catch (e) { $('workStatus').textContent=e.message; }
  });
  $('deleteWork').addEventListener('click', async () => {
    const key=list.value;
    if (!key || !confirm('이 작품의 말투·용어 설정과 기억한 내용을 모두 지울까요?')) return;
    await call('deleteWork', {scope:key}).catch(() => {});
    const current=await chrome.storage.local.get('workProfiles');
    workProfiles=(current.workProfiles || []).filter(p=>p.url!==key);
    await chrome.storage.local.set({workProfiles});
    render(); show(); $('workStatus').textContent='삭제했어요.';
  });
  const currentUrl = new URLSearchParams(location.search).get('workUrl');
  if (currentUrl) {
    const match=BalloonLensReading.workSettings(currentUrl,workProfiles);
    show(match.url ? match : {url:currentUrl});
    list.value=match.url || '';
    $('workUrl').scrollIntoView({block:'center'});
  }
}
initWorks();
