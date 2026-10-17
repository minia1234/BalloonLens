// Interface text only. Dialogue, user inputs and work notes are excluded from DOM localization.
(() => {
  if (globalThis.BalloonLensUI) return;
  const roots = new Set(), records = new WeakMap();
  const excluded = 'script,style,textarea,code,.layers,.seg,.src,.tgt,.ed-board,#workList,#suggestions,.demo-stage,.balloon-preview,[data-ui-raw]';
  let language = 'ko', translations = {}, patterns = [], initialized, operation = 0;
  const choices = ['한국어 (ko)', 'English (en)', '日本語 (ja)', '中文 (zh-Hans)', '繁體中文 (zh-Hant)',
    'Español (es)', 'Français (fr)', 'Deutsch (de)', 'Italiano (it)', 'Português (pt)', 'Русский (ru)',
    'Українська (uk)', 'العربية (ar)', 'हिन्दी (hi)', 'বাংলা (bn)', 'ไทย (th)', 'Tiếng Việt (vi)', 'Bahasa Indonesia (id)'];
  const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  function key(value) {
    value = String(value || 'ko').trim();
    const match = value.match(/\(([A-Za-z0-9-]+)\)$/);
    value = match ? match[1] : value;
    return ({'한국어': 'ko', 'Korean': 'ko', 'English': 'en', '日本語': 'ja', 'Japanese': 'ja'})[value] || value;
  }
  function set(data) {
    language = data.language;
    translations = data.translations || {};
    patterns = Object.entries(translations).filter(([source]) => /\{\w+\}/.test(source))
      .sort((a, b) => b[0].length - a[0].length).map(([source, target]) => {
        const names = [], pieces = [];
        let at = 0;
        for (const match of source.matchAll(/\{(\w+)\}/g)) {
          pieces.push(escape(source.slice(at, match.index)));
          names.push(match[1]);
          pieces.push(['count','width','height','x','y','shown','total'].includes(match[1]) ? '([-\\d.,]+)' : '(.+?)');
          at = match.index + match[0].length;
        }
        pieces.push(escape(source.slice(at)));
        return {pattern: new RegExp('^' + pieces.join('') + '$', 's'), names, target};
      });
    for (const root of roots) apply(root);
    if (location.protocol === 'chrome-extension:') {
      document.documentElement.lang = /^[A-Za-z]{2,8}(?:-[A-Za-z0-9]+)*$/.test(language) ? language : 'en';
      document.documentElement.dir = /^(ar|he|fa|ur|ps|sd|ug|yi|dv)(-|$)/i.test(language) ? 'rtl' : 'ltr';
    }
  }
  function t(source, depth = 0) {
    source = String(source ?? '');
    if (language === 'ko' || depth > 3) return source;
    if (Object.hasOwn(translations, source)) return translations[source];
    for (const {pattern, names, target} of patterns) {
      const match = source.match(pattern);
      if (match) return target.replace(/\{(\w+)\}/g, (all, name) => {
        const index = names.indexOf(name);
        return index < 0 ? all : t(match[index + 1], depth + 1);
      });
    }
    if (source.includes('\n')) return source.split('\n').map((part) => t(part, depth + 1)).join('\n');
    if (source.includes(' · ')) return source.split(' · ').map((part) => t(part, depth + 1)).join(' · ');
    return source;
  }
  function value(node, attribute) {
    const current = attribute ? node.getAttribute(attribute) : node.nodeValue;
    const saved = records.get(node) || {};
    const name = attribute || 'text';
    const previous = saved[name];
    const source = previous && previous.last === current ? previous.source : current;
    const content = String(source || '').trim();
    const translated = t(content);
    const next = source ? source.replace(content, () => translated) : source;
    if (current !== next) {
      if (attribute) node.setAttribute(attribute, next);
      else node.nodeValue = next;
    }
    saved[name] = {source, last: next};
    records.set(node, saved);
  }
  function apply(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode(node) {
        const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
        return element?.closest(excluded) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      },
    });
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.nodeType === Node.TEXT_NODE) value(node);
      else for (const attr of ['title', 'placeholder', 'aria-label', 'alt']) if (node.hasAttribute(attr)) value(node, attr);
    }
    root.querySelectorAll('textarea[placeholder]').forEach((node) => value(node, 'placeholder'));
  }
  async function request(locale, prepare = false) {
    const result = await chrome.runtime.sendMessage({type: 'uiLocale', language: locale, prepare});
    if (!result?.ok) throw new Error('ui_language_unavailable');
    return result.data;
  }
  async function load(locale) {
    const data = await request(locale);
    set({...data, language: locale});
    return data;
  }
  function init(root) {
    roots.add(root);
    let queued = false;
    new MutationObserver(() => {
      if (queued) return;
      queued = true;
      queueMicrotask(() => { queued = false; apply(root); });
    }).observe(root, {subtree: true, childList: true, characterData: true, attributes: true,
                     attributeFilter: ['title', 'placeholder', 'aria-label', 'alt']});
    initialized ??= chrome.storage.local.get('uiLanguage').then(({uiLanguage = 'ko'}) => load(uiLanguage)).catch(() => {});
    apply(root);
    return initialized;
  }
  async function choose(locale, report) {
    locale = key(locale);
    if (!locale || locale.length > 80 || /[\u0000-\u001f]/.test(locale)) return;
    const mine = ++operation;
    report('화면 언어를 준비하고 있어요…');
    try {
      let data = await request(locale, true);
      while (data.state === 'loading' && mine === operation) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        data = await request(locale);
      }
      if (mine !== operation) return;
      if (data.state !== 'ready') throw new Error('ui_language_unavailable');
      set({...data, language: locale});
      await chrome.storage.local.set({uiLanguage: locale});
      report('화면 언어를 바꿨어요.');
    } catch {
      if (mine === operation) report('언어를 준비하지 못했어요. Claude 로그인과 도우미 연결을 확인해 주세요.');
    }
  }
  function selector(root = document) {
    const input = root.querySelector('[data-ui-language]');
    if (!input) return;
    const list = root.querySelector('[data-ui-languages]');
    const values = new Set(choices);
    // ISO language names offered by this browser, plus an editable field for any language or regional variant.
    const display = typeof Intl.DisplayNames === 'function' ? new Intl.DisplayNames(['en'], {type: 'language'}) : null;
    if (display) for (let a = 97; a <= 122; a++) for (let b = 97; b <= 122; b++) {
      const code = String.fromCharCode(a, b), name = display.of(code);
      if (name && name !== code) values.add(`${name} (${code})`);
    }
    for (const label of values) {
      const option = document.createElement('option');
      option.value = label;
      list.append(option);
    }
    initialized.then(() => { input.value = language; });
    const status = root.querySelector('[data-ui-language-status]');
    const report = (text) => { status.textContent = t(text); };
    root.querySelector('[data-ui-language-apply]').addEventListener('click', () => choose(input.value, report));
    input.addEventListener('keydown', (event) => { if (event.key === 'Enter') choose(input.value, report); });
  }
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && changes.uiLanguage) {
      const next = changes.uiLanguage.newValue || 'ko';
      load(next).then(() => {
        for (const root of roots) root.querySelectorAll('[data-ui-language]').forEach((input) => { input.value = next; });
      }).catch(() => {});
    }
  });
  globalThis.BalloonLensUI = {t, init, selector, get language() { return language; }};
})();
