// Shared reading layout, also exercised without a browser by tools/test_reading.cjs.
(() => {
  function workSettings(url, profiles = []) {
    const empty = { instructions: '', glossary: '', scope: url.split('#')[0], matched: false };
    let page;
    try { page = new URL(url); } catch { return empty; }
    const matches = profiles.filter(p => {
      try {
        const base = new URL(p.url), path = base.pathname.replace(/\/$/, '');
        return base.origin === page.origin && (page.pathname === path || page.pathname.startsWith(path + '/'));
      } catch { return false; }
    }).sort((a,b) => b.url.length - a.url.length);
    return matches.length ? { ...matches[0], scope: matches[0].url, matched: true } : empty;
  }

  function sourceFontSize(segment) {
    const [, , w, h] = segment.bbox;
    const count = Math.max(1, [...String(segment.src || '').replace(/\s/g,'')].length);
    return Math.max(1, Math.min(Math.min(w,h), Math.sqrt(w*h/count))*.9);
  }
  function sourceFrame(segment, W, H) {
    const [x,y,sw,sh]=segment.bbox;
    const safe=segment.presentation?.frame;
    let left=0,top=0,right=W,bottom=H,w,h;
    if (safe) {
      left=Math.max(0,safe[0]); top=Math.max(0,safe[1]);
      right=Math.min(W,safe[0]+safe[2]); bottom=Math.min(H,safe[1]+safe[3]);
      const area=sw*sh*1.5;
      // Verified empty space allows a vertical source to become horizontal,
      // but a tiny phrase never acquires the entire bubble's area.
      w=Math.min(right-left,Math.max(sw,Math.sqrt(area*.9)));
      h=Math.min(bottom-top,area/Math.max(1,w));
    } else {
      // With no verified bubble, do not cover neighboring artwork to fit text.
      const pad=Math.min(3,Math.min(sw,sh)*.1);
      w=Math.min(W,sw+2*pad); h=Math.min(H,sh+2*pad);
    }
    w=Math.max(1,w); h=Math.max(1,h);
    return [Math.max(left,Math.min(right-w,x+sw/2-w/2)),Math.max(top,Math.min(bottom-h,y+sh/2-h/2)),w,h];
  }
  function fitText(text, width, height, measure, scale = 1, preferredSize = 26) {
    const clean = String(text || '').replace(/\s+/g, ' ').trim();
    const cap=Math.max(11,Math.min(26,preferredSize))*scale;
    const min = Math.min(cap,Math.max(11, 14*scale)), max = Math.max(min, Math.min(cap, height/1.3));
    function wrap(size) {
      const tokens = [];
      let broken = false;
      for (const word of clean.split(' ')) {
        if (measure(word, size) <= width) tokens.push({text:word, space:true});
        else {
          broken = true;
          for (const [i,ch] of [...word].entries()) tokens.push({text:ch, space:i === 0});
        }
      }
      const n = tokens.length, cost = Array(n+1).fill(Infinity), next = Array(n).fill(n);
      cost[n] = 0;
      for (let i=n-1; i>=0; i--) {
        let line = '';
        for (let j=i; j<n; j++) {
          line += (j>i && tokens[j].space ? ' ' : '') + tokens[j].text;
          const w = measure(line, size);
          if (w>width && j>i) break;
          const punctuation = j+1<n && /^[!?.,、。！？]/.test(tokens[j+1].text) ? width*width*2 : 0;
          const orphan = j===n-1 && i>0 && w<width*.3 ? width*width : 0;
          const value = (width-w)**2 + cost[j+1] + punctuation + orphan;
          if (value<cost[i]) { cost[i]=value; next[i]=j+1; }
        }
      }
      const lines=[];
      for (let i=0;i<n;) {
        const end=Math.max(i+1,next[i]);
        lines.push(tokens.slice(i,end).map((t,k)=>(k && t.space ? ' ' : '')+t.text).join(''));
        i=end;
      }
      return {size, lines, broken, overflow: lines.length*size*1.3>height || lines.some(l=>measure(l,size)>width)};
    }
    for (let size=max;size>=min;size-=.5) {
      // Preserve ordinary words before choosing a larger font with character breaks.
      if (clean.split(' ').some(word => measure(word, min) <= width && measure(word, size) > width)) continue;
      const result=wrap(size);
      if (!result.overflow && !result.broken) return result;
    }
    // Tiny balloons: a slightly smaller font reads better than words split into syllables or text spilling out.
    const floor = Math.max(11, min - 3*scale);
    for (let size=min-.5;size>=floor;size-=.5) {
      const result=wrap(size);
      if (!result.overflow && !result.broken) return result;
    }
    for (let size=max;size>=min;size-=.5) {
      const result=wrap(size);
      if (!result.overflow) return result;
    }
    return wrap(min);
  }
  // A line that does not fit its box (or fits only in a font below the readable size) may get a bigger box, grown
  // around the original one by these factors on both sides and never past the image. The smallest box that shows
  // the whole line at a readable size wins. fitText stays the strict fit; this is the one the page uses.
  const GROW_STEPS = [1.25, 1.5, 1.75, 2];
  // free(width, height) says whether a box of that content size fits in the image without covering a neighbouring
  // balloon. A grown box that is not free is skipped, so growth never hides another balloon.
  function fitGrowing(text, width, height, maxWidth, maxHeight, measure, scale = 1, preferredSize = 26,
                      free = () => true) {
    const readable = Math.min(14 * scale, Math.max(11, Math.min(26, preferredSize)) * scale);
    const plain = fitText(text, width, height, measure, scale, preferredSize);
    const original = {...plain, width, height};
    if (!plain.overflow && plain.size >= readable) return original;
    let fallback = plain.overflow ? null : original; // whole line, but not at a readable size
    let roomiest = null; // the largest free box: as much of the line as fits without covering a neighbour
    for (const k of GROW_STEPS) {
      const w = Math.max(width, Math.min(maxWidth, width * k));
      const h = Math.max(height, Math.min(maxHeight, height * k));
      if (!free(w, h)) continue;
      const grown = {...fitText(text, w, h, measure, scale, preferredSize), width: w, height: h};
      roomiest = grown;
      if (!grown.overflow && grown.size >= readable) return grown;
      if (!grown.overflow && !fallback) fallback = grown;
    }
    return fallback || roomiest || original;
  }
  function nextFontScale(value, action) {
    if (action === 'reset') return 1;
    return Math.round(Math.max(.7, Math.min(1.5, (Number(value) || 1)+(action==='up' ? .1 : -.1)))*10)/10;
  }
  function needsRetry(segment) {
    return !segment.edited && (segment.status==='failed' || (segment.status==='review' && !segment.tgt));
  }
  function speechPreset(name) {
    return ({natural:'인물의 말투와 관계를 원문에 맞게 유지하고 자연스러운 한국어 구어체로 번역하세요. 관계를 임의로 만들지 마세요.',
      casual:'자연스러운 한국어 반말로 번역하세요. -요/-습니다를 섞지 말고 의미와 감정을 유지하세요.',
      polite:'자연스러운 한국어 존댓말(해요체)로 번역하세요. 딱딱한 문어체를 피하고 의미와 감정을 유지하세요.'})[name] || '';
  }
  // ---- v0.5.0: subscription backends and shared wording (popup, options and the page use these) ----
  const MODEL_INFO = {
    'claude-haiku-5-5': { label: 'Claude 구독', detail: 'Claude Haiku 5.5 · Claude Code 로그인', kind: 'subscription', backend: 'claude' },
    'qwen3.5:9b': { label: '로컬 Qwen3.5', detail: 'Qwen3.5 9B · 이 PC · Ollama', kind: 'local', backend: null },
    'qwen3:8b': { label: '로컬 Qwen3', detail: 'Qwen3 8B · 이 PC · Ollama', kind: 'local', backend: null },
    'translategemma:4b': { label: '로컬 TranslateGemma', detail: 'TranslateGemma 4B · 이 PC · Ollama', kind: 'local', backend: null },
  };
  const MODEL_IDS = Object.keys(MODEL_INFO);
  const REVIEW_MODES = [null, 'off', 'selective', 'all'];
  const isSubscription = (model) => MODEL_INFO[model]?.kind === 'subscription';
  const STATE_LABEL = {
    ready: ['준비됨', 'ok'],
    not_installed: ['설치 필요', 'err'],
    not_logged_in: ['로그인 필요', 'warn'],
    api_key_detected: ['API 키 로그인', 'err'],
    unsupported: ['지원 안 함', 'err'],
    backend_error: ['오류', 'err'],
    unchecked: ['확인 전', 'warn'],
  };
  const stateLabel = (state) => STATE_LABEL[state] || [state || '확인 전', 'warn'];
  const ERRORS = {
    helper_unreachable: '도우미에 연결할 수 없어요',
    helper_timeout: '도우미 응답이 너무 늦어요',
    helper_start_timeout: '도우미가 켜지지 않았어요. 설치 창(BalloonLens-Setup.exe)에서 "상태 다시 확인"을 눌러 주세요. 8787 포트를 다른 프로그램이 쓰면 그 프로그램을 끄세요',
    native_missing: '도우미 설치가 필요해요. BalloonLens-Setup.exe를 한 번 실행해 주세요',
    pair_forbidden: '도우미가 이 확장을 허용하지 않았어요',
    queue_full: '처리할 이미지가 너무 많아요. 잠시 뒤 다시 해 주세요',
    image_too_large: '이미지가 너무 커요',
    region_too_small: '선택한 영역이 너무 작아요',
    tab_not_active: '번역할 탭으로 돌아온 뒤 다시 선택해 주세요',
    extension_reloaded: '확장이 새로 로드됐어요. 페이지를 새로고침해 주세요',
    not_installed: 'Claude Code가 설치돼 있지 않아요',
    not_logged_in: '구독 로그인이 필요해요. 설정의 로그인 안내를 따라 주세요',
    api_key_detected: 'API 키로 로그인돼 있어요. 구독 로그인으로 바꿔 주세요',
    unsupported: '지원하지 않는 로그인 방식이에요',
    quota_paused: '구독 사용량 한도에 가까워 잠시 멈췄어요',
    quota_exhausted: '구독 사용량 한도에 도달했어요',
    auth_expired: '구독 로그인이 만료됐어요. 다시 로그인해 주세요',
    backend_error: '구독 도구를 실행하지 못했어요',
    timeout: '구독 번역 응답이 너무 늦어요',
    tool_attempt: '모델이 허용되지 않은 동작을 시도해서 그 응답은 버렸어요',
    payload_too_large: '한 번에 보내기에 페이지 글이 너무 많아요',
    process_failed: '구독 번역 호출이 실패했어요',
    bad_output: '모델 응답을 읽지 못했어요',
    schema_violation: '모델 응답 형식이 맞지 않아요',
    empty_output: '모델이 빈 응답을 보냈어요',
    unsupported_model: '지원하지 않는 번역 모델이에요',
    suggest_unparseable: '제안을 만들지 못했어요',
    bad_caps: '한도 숫자를 확인해 주세요 (5시간 한도는 7일 한도보다 클 수 없어요)',
    bad_percent: '퍼센트는 0에서 100 사이로 적어 주세요',
  };
  const explainCode = (code) => ERRORS[code] || code;

  // Settings backup: only known keys with sane values come back; work notes only for works in the file.
  const BACKUP_KEYS = ['src','tgt','model','reading','fontScale','bgOpacity','showSource','autoSites','workProfiles','vision','reviewMode'];
  function validateBackup(data) {
    if (!data || data.app !== 'BalloonLens' || !data.settings || typeof data.settings !== 'object') {
      throw new Error('말풍선 렌즈 설정 파일이 아니에요');
    }
    const s = data.settings, settings = {};
    const pick = (key, ok) => { if (key in s && ok(s[key])) settings[key] = s[key]; };
    const text = (v, max) => typeof v === 'string' && v.length <= max;
    pick('src', v => ['ja','en'].includes(v));
    pick('tgt', v => ['ko','en','ja'].includes(v));
    pick('model', v => MODEL_IDS.includes(v));
    pick('reading', v => ['rtl','ltr','ttb'].includes(v));
    pick('fontScale', v => typeof v === 'number' && v >= .7 && v <= 1.5);
    pick('bgOpacity', v => typeof v === 'number' && v >= .5 && v <= 1);
    for (const key of ['showSource','vision']) pick(key, v => typeof v === 'boolean');
    pick('reviewMode', v => REVIEW_MODES.includes(v));
    // Backups from v0.4 kept a yes/no "review everything" flag instead.
    if (!('reviewMode' in settings) && typeof s.reviewAll === 'boolean') settings.reviewMode = s.reviewAll ? 'all' : null;
    pick('autoSites', v => Array.isArray(v) && v.length <= 200 && v.every(h => text(h, 253) && /^[a-z0-9.-]+$/i.test(h)));
    if (Array.isArray(s.workProfiles) && s.workProfiles.length <= 200 && s.workProfiles.every(p =>
        p && text(p.url, 200) && /^https?:\/\//.test(p.url) && text(p.instructions ?? '', 2000) && text(p.glossary ?? '', 4000))) {
      settings.workProfiles = s.workProfiles.map(p => ({url: p.url, instructions: p.instructions || '', glossary: p.glossary || ''}));
    }
    const known = new Set((settings.workProfiles || []).map(p => p.url));
    const works = {};
    for (const [scope, w] of Object.entries(data.works || {})) {
      if (!known.has(scope) || !w || typeof w !== 'object') continue;
      works[scope] = {title: String(w.title || '').slice(0, 120), summary: String(w.summary || '').slice(0, 400),
        characters: String(w.characters || '').slice(0, 800)};
    }
    return {settings, works, skipped: Object.keys(s).filter(k => !(k in settings) && k !== 'reviewAll')};
  }
  // Subscription models pay per image (plan §8), so the page queue reads ahead by one image: every image on screen
  // (in both directions: a horizontal viewer shows several pages side by side), plus the nearest image that comes
  // next in the reading direction. If that one is already queued, the read-ahead is done.
  // "Next" is the image below a vertical page, or to the left (rtl) / right (ltr) of a horizontal one.
  function isNextImage(item, viewportWidth, viewportHeight, reading) {
    const sharesColumn = item.left < viewportWidth && item.right > 0;
    if (sharesColumn && item.top >= viewportHeight) return true;
    if (reading === 'rtl') return item.right <= 0;
    if (reading === 'ltr') return item.left >= viewportWidth;
    return false;
  }
  function planReadAhead(items, viewportWidth, viewportHeight, readAheadBusy, reading = 'ttb') {
    const plan = [];
    let next = null, nextGap = Infinity;
    for (const item of items) {
      if (item.bottom > 0 && item.top < viewportHeight && item.right > 0 && item.left < viewportWidth) {
        if (!item.queued) plan.push({item, priority: 2});
      } else if (isNextImage(item, viewportWidth, viewportHeight, reading)) {
        const gap = Math.max(item.top - viewportHeight, item.left - viewportWidth, -item.bottom, -item.right);
        if (gap < nextGap) { next = item; nextGap = gap; }
      }
    }
    if (next && !next.queued && !readAheadBusy) plan.push({item: next, priority: 3});
    return plan;
  }
  // The part of an image that is on screen, as fractions of its height: [top, bottom]. The helper reads the balloons there first.
  // null when the image has no height or none of it is on screen.
  function visibleBand(rect, viewportHeight) {
    if (!(rect.height > 0)) return null;
    const at = (y) => Math.min(1, Math.max(0, y / rect.height));
    const band = [at(-rect.top), at(viewportHeight - rect.top)];
    return band[1] > band[0] ? band : null;
  }
  function subscriptionUsageText(usage, formatTime = (ts) => new Date(ts * 1000).toLocaleString('ko-KR', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  })) {
    const windows = usage?.windows || [];
    if (!windows.length) return usage?.state === 'loading' ? 'Claude 남은 사용량 확인 중…' : 'Claude 남은 사용량 확인 불가';
    const parts = ['five_hour', 'seven_day'].map((id) => {
      const w = windows.find((item) => item.id === id);
      const label = id === 'five_hour' ? '5시간' : '7일';
      const value = w?.remaining_percent;
      const remaining = typeof value === 'number' && Number.isFinite(value) ? `${value}%` : '확인 불가';
      const reset = w?.resets_at ? ` (${formatTime(w.resets_at)} 초기화)` : '';
      return `${label} 남음 ${remaining}${reset}`;
    });
    if (usage.stale) parts.unshift('마지막 확인값');
    if (usage.checked_at) parts.push(`확인 ${formatTime(usage.checked_at)}`);
    return parts.join(' · ');
  }
  globalThis.BalloonLensReading = {fitText, fitGrowing, sourceFrame, sourceFontSize, workSettings, nextFontScale, needsRetry, speechPreset,
    validateBackup, BACKUP_KEYS, MODEL_INFO, MODEL_IDS, REVIEW_MODES, isSubscription,
    stateLabel, explainCode, planReadAhead, visibleBand, subscriptionUsageText};
  if (typeof module !== 'undefined') module.exports = globalThis.BalloonLensReading;
})();
