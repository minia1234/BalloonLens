// Region editor: fix where the source text is (what gets read and erased) and, separately, where its
// translation goes. Layout operations are pure and exercised by tools/test_editor.cjs; the UI runs inside
// the content script's shadow root and only talks to it through callbacks.
(() => {
  const MIN = 8;
  let uidCounter = 0;
  const newUid = () => `u${Date.now().toString(36)}${(uidCounter++).toString(36)}`;
  const clone = (v) => JSON.parse(JSON.stringify(v));

  // ---------- pure layout operations ----------
  // state = {regions: [{ref, bbox, frame, auto, src}], layout: {removed, added, frames}}
  // ref is {kind:'ocr', key} for detected regions or {kind:'user', uid} for reader regions.

  function fromSnapshot(snap) {
    const layout = clone(snap.layout || {});
    layout.removed = layout.removed || [];
    // moved_from is consumed by the helper when saved; never resend an old one.
    layout.added = (layout.added || []).map(({ moved_from, ...region }) => region);
    layout.frames = layout.frames || {};
    const regions = (snap.segments || []).map((s) => ({
      ref: clone(s.origin || { kind: 'ocr', key: s.bbox.join(',') }),
      bbox: [...s.bbox],
      frame: s.presentation?.fill ? [...s.presentation.frame] : null,
      auto: !s.presentation?.fill && s.presentation?.frame ? [...s.presentation.frame] : null,
      src: s.src || '',
      saved: s.bbox.join(','), // key the reader's text fixes are stored under
    }));
    return { regions, layout };
  }

  function clampBox([x, y, w, h], W, H) {
    w = Math.max(MIN, Math.min(W, Math.round(w)));
    h = Math.max(MIN, Math.min(H, Math.round(h)));
    return [Math.max(0, Math.min(W - w, Math.round(x))), Math.max(0, Math.min(H - h, Math.round(y))), w, h];
  }

  const addedOf = (s, r) => s.layout.added.find((a) => a.uid === r.ref.uid);

  // A detected region's box can't change in place (its key is its box), so it becomes a reader region.
  function detach(state, i) {
    const s = clone(state);
    const r = s.regions[i];
    if (r.ref.kind === 'ocr') {
      const key = r.ref.key;
      if (!s.layout.removed.includes(key)) s.layout.removed.push(key);
      const frame = s.layout.frames[key] || r.frame || null;
      delete s.layout.frames[key];
      const uid = newUid();
      s.layout.added.push({ uid, bbox: [...r.bbox], frame, moved_from: key });
      Object.assign(r, { ref: { kind: 'user', uid }, frame });
    }
    return s;
  }

  function setBox(state, i, bbox) {
    const s = detach(state, i);
    const r = s.regions[i];
    const region = addedOf(s, r);
    // A saved reader region keeps its fixes too when moved again.
    if (!region.moved_from && r.saved && r.saved !== bbox.join(',')) region.moved_from = r.saved;
    r.bbox = [...bbox];
    r.src = ''; // read again after saving
    r.auto = null;
    region.bbox = [...bbox];
    return s;
  }

  function setFrame(state, i, frame) {
    const s = clone(state);
    const r = s.regions[i];
    r.frame = frame ? [...frame] : null;
    if (r.ref.kind === 'ocr') {
      if (frame) s.layout.frames[r.ref.key] = [...frame];
      else delete s.layout.frames[r.ref.key];
    } else addedOf(s, r).frame = r.frame;
    return s;
  }

  function remove(state, indices) {
    const s = clone(state);
    for (const i of indices) {
      const r = s.regions[i];
      if (r.ref.kind === 'ocr') {
        if (!s.layout.removed.includes(r.ref.key)) s.layout.removed.push(r.ref.key);
        delete s.layout.frames[r.ref.key];
      } else s.layout.added = s.layout.added.filter((a) => a.uid !== r.ref.uid);
    }
    s.regions = s.regions.filter((_, i) => !indices.includes(i));
    return s;
  }

  function add(state, bbox) {
    const s = clone(state);
    const uid = newUid();
    s.layout.added.push({ uid, bbox: [...bbox], frame: null });
    s.regions.push({ ref: { kind: 'user', uid }, bbox: [...bbox], frame: null, auto: null, src: '' });
    return s;
  }

  function split(state, i, axis) {
    const [x, y, w, h] = state.regions[i].bbox;
    const half = Math.floor((axis === 'x' ? w : h) / 2);
    const parts = axis === 'x'
      ? [[x, y, half, h], [x + half, y, w - half, h]]
      : [[x, y, w, half], [x, y + half, w, h - half]];
    if (parts.some((p) => p[2] < MIN || p[3] < MIN)) return state;
    return parts.reduce((s, p) => add(s, p), remove(state, [i]));
  }

  function merge(state, indices) {
    if (indices.length < 2) return state;
    const boxes = indices.map((i) => state.regions[i].bbox);
    const x = Math.min(...boxes.map((b) => b[0]));
    const y = Math.min(...boxes.map((b) => b[1]));
    const right = Math.max(...boxes.map((b) => b[0] + b[2]));
    const bottom = Math.max(...boxes.map((b) => b[1] + b[3]));
    return add(remove(state, indices), [x, y, right - x, bottom - y]);
  }

  function history(initial) {
    let stack = [initial];
    let at = 0;
    return {
      get state() { return stack[at]; },
      push(s) { stack = stack.slice(0, at + 1).concat([s]).slice(-100); at = stack.length - 1; },
      undo() { if (at > 0) at--; return stack[at]; },
      redo() { if (at < stack.length - 1) at++; return stack[at]; },
      get canUndo() { return at > 0; },
      get canRedo() { return at < stack.length - 1; },
    };
  }

  const isEmptyLayout = (l) => !l.removed.length && !l.added.length && !Object.keys(l.frames).length;

  // ---------- UI ----------
  const HANDLES = { nw: [0, 0], n: [.5, 0], ne: [1, 0], e: [1, .5], se: [1, 1], s: [.5, 1], sw: [0, 1], w: [0, .5] };
  const CURSORS = { nw: 'nwse', se: 'nwse', ne: 'nesw', sw: 'nesw', n: 'ns', s: 'ns', e: 'ew', w: 'ew' };
  const HINTS = {
    text: '원문 글자를 감싸도록 상자를 맞춰요 · 끌어서 옮기기 · 모서리로 크기 조절 · Shift+클릭으로 여러 개 선택',
    frame: '번역문이 들어갈 칸을 조절해요 · 점선은 자동 배치 · 원문 글자 위치는 그대로예요',
    adding: '번역할 글자를 드래그해서 감싸세요 · Esc 취소',
  };

  function open({ mount, layer, snap, onSave, onReset, onClose }) {
    const W = snap.width;
    const H = snap.height;
    const hist = history(fromSnapshot(snap));
    const initial = JSON.stringify(hist.state.layout);
    let mode = 'text';
    let adding = false;
    let selected = [];
    let drag = null;
    let busy = false;

    const board = document.createElement('div');
    board.className = 'ed-board';
    const bar = document.createElement('div');
    bar.className = 'ed-bar';
    bar.setAttribute('role', 'toolbar');
    bar.innerHTML = `
      <div class="ed-row"><b>영역 편집</b><span class="ed-hint" data-ref="hint"></span></div>
      <div class="ed-row">
        <span class="ed-seg" role="radiogroup" aria-label="편집 대상">
          <button role="radio" data-mode="text">글자 영역</button><button role="radio" data-mode="frame">번역 영역</button>
        </span>
        <button data-act="add">+ 영역 추가</button>
        <button data-act="splitx">좌우 나누기</button>
        <button data-act="splity">위아래 나누기</button>
        <button data-act="merge">합치기</button>
        <button data-act="auto">자동 배치로</button>
        <button data-act="delete">삭제</button>
        <button data-act="undo" title="Ctrl+Z">되돌리기</button>
        <button data-act="redo" title="Ctrl+Y">다시 실행</button>
      </div>
      <div class="ed-row ed-end">
        <span class="ed-hint" data-ref="status"></span>
        <button data-act="reset" title="이 이미지의 영역 수정을 모두 지워요">처음 검출로</button>
        <button data-act="cancel">취소</button>
        <button data-act="save" class="primary">저장하고 다시 번역</button>
      </div>`;
    const ref = (n) => bar.querySelector(`[data-ref="${n}"]`);
    const btn = (n) => bar.querySelector(`[data-act="${n}"]`);
    layer.classList.add('editing');
    layer.append(board);
    mount.append(bar);

    const pct = (v, total) => `${(v / total) * 100}%`;
    const boxOf = (r) => (mode === 'text' ? r.bbox : r.frame || r.auto || r.bbox);

    function place(el, [x, y, w, h]) {
      Object.assign(el.style, { left: pct(x, W), top: pct(y, H), width: pct(w, W), height: pct(h, H) });
    }

    function render() {
      board.replaceChildren();
      board.classList.toggle('adding', adding);
      hist.state.regions.forEach((r, i) => {
        for (const kind of ['text', 'frame']) {
          if (kind === 'frame' && mode === 'text') continue;
          const el = document.createElement('div');
          const active = kind === mode && !adding;
          el.className = `ed-box ed-${kind}${active ? '' : ' faint'}${kind === 'frame' && !r.frame ? ' auto' : ''}${active && selected.includes(i) ? ' sel' : ''}`;
          place(el, kind === 'text' ? r.bbox : r.frame || r.auto || r.bbox);
          if (active) el.dataset.i = i;
          const tag = document.createElement('span');
          tag.className = 'tag';
          tag.textContent = kind === 'text' ? `${i + 1} ${r.src ? r.src.slice(0, 10) : '(새 영역)'}` : `${i + 1} 번역${r.frame ? '' : ' · 자동'}`;
          el.append(tag);
          if (active && selected.length === 1 && selected[0] === i) {
            for (const [h, [fx, fy]] of Object.entries(HANDLES)) {
              const handle = document.createElement('span');
              handle.className = 'ed-h';
              handle.dataset.h = h;
              Object.assign(handle.style, { left: `calc(${fx * 100}% - 6px)`, top: `calc(${fy * 100}% - 6px)`, cursor: `${CURSORS[h]}-resize` });
              el.append(handle);
            }
          }
          board.append(el);
        }
      });
      bar.querySelectorAll('[data-mode]').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.mode === mode)));
      btn('add').setAttribute('aria-pressed', String(adding));
      const one = selected.length === 1;
      btn('splitx').disabled = btn('splity').disabled = !(one && mode === 'text');
      btn('merge').disabled = !(selected.length > 1 && mode === 'text');
      btn('auto').disabled = !(selected.length && mode === 'frame');
      btn('delete').disabled = !selected.length;
      btn('undo').disabled = !hist.canUndo;
      btn('redo').disabled = !hist.canRedo;
      btn('save').disabled = busy;
      ref('hint').textContent = adding ? HINTS.adding : HINTS[mode];
      const changed = JSON.stringify(hist.state.layout) !== initial;
      ref('status').textContent = busy ? '저장하는 중…' : changed ? '저장 안 된 변경이 있어요' : `영역 ${hist.state.regions.length}개`;
    }

    function commit(next, nextSelection = []) {
      if (next === hist.state) return;
      hist.push(next);
      selected = nextSelection;
      render();
    }

    function point(ev, rect) {
      return [(ev.clientX - rect.left) * (W / rect.width), (ev.clientY - rect.top) * (H / rect.height)];
    }

    board.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      const rect = board.getBoundingClientRect();
      const [px, py] = point(ev, rect);
      if (adding) {
        drag = { kind: 'draw', rect, px, py, ghost: document.createElement('div') };
        drag.ghost.className = 'ed-ghost';
        board.append(drag.ghost);
      } else {
        const target = ev.target.closest('[data-i]');
        if (!target) {
          selected = [];
          return render();
        }
        const i = Number(target.dataset.i);
        if (ev.shiftKey) {
          selected = selected.includes(i) ? selected.filter((n) => n !== i) : [...selected, i];
          return render();
        }
        if (!selected.includes(i) || selected.length > 1) {
          selected = [i];
          render();
        }
        const handle = ev.target.dataset.h;
        drag = { kind: handle ? 'resize' : 'move', handle, i, rect, px, py, start: [...boxOf(hist.state.regions[i])], moved: false };
      }
      board.setPointerCapture(ev.pointerId);
    });

    board.addEventListener('pointermove', (ev) => {
      if (!drag) return;
      const [px, py] = point(ev, drag.rect);
      const dx = px - drag.px;
      const dy = py - drag.py;
      if (drag.kind === 'draw') {
        drag.box = clampBox([Math.min(px, drag.px), Math.min(py, drag.py), Math.abs(dx), Math.abs(dy)], W, H);
        return place(drag.ghost, drag.box);
      }
      if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
      drag.moved = true;
      let [x, y, w, h] = drag.start;
      if (drag.kind === 'move') {
        x += dx;
        y += dy;
      } else {
        const hd = drag.handle;
        if (hd.includes('w')) { x += Math.min(dx, w - MIN); w -= Math.min(dx, w - MIN); }
        if (hd.includes('e')) w += dx;
        if (hd.includes('n')) { y += Math.min(dy, h - MIN); h -= Math.min(dy, h - MIN); }
        if (hd.includes('s')) h += dy;
      }
      drag.box = clampBox([x, y, w, h], W, H);
      const el = board.querySelector(`[data-i="${drag.i}"]`);
      if (el) place(el, drag.box);
    });

    board.addEventListener('pointerup', () => {
      const d = drag;
      drag = null;
      if (!d) return;
      if (d.kind === 'draw') {
        adding = false;
        d.ghost.remove();
        if (d.box && d.box[2] >= 12 && d.box[3] >= 12) {
          const next = add(hist.state, d.box);
          return commit(next, [next.regions.length - 1]);
        }
        return render();
      }
      if (!d.moved || !d.box) return;
      commit(mode === 'text' ? setBox(hist.state, d.i, d.box) : setFrame(hist.state, d.i, d.box), [d.i]);
    });

    function nudge(dx, dy) {
      if (selected.length !== 1) return;
      const i = selected[0];
      const [x, y, w, h] = boxOf(hist.state.regions[i]);
      const box = clampBox([x + dx, y + dy, w, h], W, H);
      commit(mode === 'text' ? setBox(hist.state, i, box) : setFrame(hist.state, i, box), [i]);
    }

    async function finish(action) {
      if (busy) return;
      const changed = JSON.stringify(hist.state.layout) !== initial;
      if (action === 'cancel') {
        if (changed && !window.confirm(globalThis.BalloonLensUI?.t('편집한 영역을 저장하지 않고 닫을까요?') || '편집한 영역을 저장하지 않고 닫을까요?')) return;
        return close();
      }
      if (action === 'save' && !changed) return close();
      busy = true;
      render();
      const layout = hist.state.layout;
      const ok = action === 'reset' ? await onReset() : await onSave(isEmptyLayout(layout) ? null : clone(layout));
      busy = false;
      if (ok) close();
      else render();
    }

    bar.addEventListener('click', (ev) => {
      const modeBtn = ev.target.closest('[data-mode]');
      if (modeBtn) {
        mode = modeBtn.dataset.mode;
        adding = false;
        selected = [];
        return render();
      }
      const act = ev.target.closest('[data-act]')?.dataset.act;
      const s = hist.state;
      if (act === 'add') {
        adding = !adding;
        mode = 'text';
        selected = [];
        render();
      }
      if (act === 'splitx' || act === 'splity') commit(split(s, selected[0], act === 'splitx' ? 'x' : 'y'));
      if (act === 'merge') {
        const next = merge(s, selected);
        commit(next, [next.regions.length - 1]);
      }
      if (act === 'auto') commit(selected.reduce((acc, i) => setFrame(acc, i, null), s), selected);
      if (act === 'delete') commit(remove(s, selected));
      if (act === 'undo') { hist.undo(); selected = []; render(); }
      if (act === 'redo') { hist.redo(); selected = []; render(); }
      if (['save', 'cancel', 'reset'].includes(act)) finish(act);
    });

    function onKey(ev) {
      if (ev.target !== document.body && ev.target !== document.documentElement && !ev.composedPath().includes(bar)) {
        // Don't steal keys from the page's own text fields.
        const tag = ev.target?.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || ev.target?.isContentEditable) return;
      }
      const ctrl = ev.ctrlKey || ev.metaKey;
      let handled = true;
      if (ev.key === 'Escape') adding ? ((adding = false), render()) : finish('cancel');
      else if (ev.key === 'Delete' || ev.key === 'Backspace') selected.length && commit(remove(hist.state, selected));
      else if (ctrl && ev.key.toLowerCase() === 'z' && !ev.shiftKey) { hist.undo(); selected = []; render(); }
      else if (ctrl && (ev.key.toLowerCase() === 'y' || (ev.shiftKey && ev.key.toLowerCase() === 'z'))) { hist.redo(); selected = []; render(); }
      else if (ev.key.startsWith('Arrow') && selected.length === 1) {
        const step = ev.shiftKey ? 10 : 1;
        nudge({ ArrowLeft: -step, ArrowRight: step }[ev.key] || 0, { ArrowUp: -step, ArrowDown: step }[ev.key] || 0);
      } else handled = false;
      if (handled) {
        ev.preventDefault();
        ev.stopPropagation();
      }
    }
    window.addEventListener('keydown', onKey, true);

    function close() {
      window.removeEventListener('keydown', onKey, true);
      layer.classList.remove('editing');
      board.remove();
      bar.remove();
      onClose?.();
    }

    render();
    return { close };
  }

  globalThis.BalloonLensEditor = { open, fromSnapshot, clampBox, setBox, setFrame, remove, add, split, merge, history };
  if (typeof module !== 'undefined') module.exports = globalThis.BalloonLensEditor;
})();
