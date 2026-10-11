"""End-to-end: load the real extension into Playwright Chromium and use it against the running helper.

Needs the helper running (python -m balloonlens) and Ollama. Screenshots go to bench_out/e2e/.
Usage: helper\\.venv\\Scripts\\python.exe tools\\e2e_extension.py
"""
import json
import os
import shutil
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "bench_out" / "e2e" / str(time.time_ns())
EXT_ID = "pnccemlpjodglelglkjjdnnmdknejgop"
PAGE = "http://127.0.0.1:8799/tools/e2e_page.html"
# Known OCR box of the first balloon in samples/synthetic/ja_page.png (1200x1700)
FIRST_BALLOON = (867, 236, 79, 224)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(ROOT), **k)

    def do_GET(self):
        if self.path.startswith("/protected/"):
            # Hotlink protection: only requests coming from our page (with a Referer) get the image.
            if not self.headers.get("Referer"):
                self.send_error(403)
                return
            self.path = "/samples/synthetic/" + self.path.split("/")[-1]
        super().do_GET()

    def log_message(self, *a):
        pass


def step(name):
    print(f"\n== {name}")


def main():
    OUT.mkdir(parents=True)
    ext = OUT / "ext"
    shutil.copytree(ROOT / "extension", ext)
    manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    # Automation can't click Chrome's permission prompt, so grant image access up front in the test copy.
    manifest["host_permissions"].append("<all_urls>")
    (ext / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    # An isolated helper keeps test edits and caches out of the reader's data.
    helper_port = int(os.environ.get("BALLOONLENS_PORT", "8787"))
    if helper_port != 8787:
        for name in ("manifest.json", "sw.js"):
            path = ext / name
            path.write_text(path.read_text(encoding="utf-8").replace("127.0.0.1:8787", f"127.0.0.1:{helper_port}"),
                            encoding="utf-8")

    server = ThreadingHTTPServer(("127.0.0.1", 8799), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    errors = []

    with sync_playwright() as p:
        # Reuse an already-downloaded Playwright Chromium if the pinned build isn't installed.
        exe = None
        if not Path(p.chromium.executable_path).exists():
            found = sorted(Path(p.chromium.executable_path).parents[2].glob("chromium-*/chrome-win64/chrome.exe"))
            exe = str(found[-1]) if found else None
        ctx = p.chromium.launch_persistent_context(
            # Real window size, no viewport emulation: captureVisibleTab grabs the actual window, so an emulated
            # viewport would make screenshots and page coordinates disagree.
            str(OUT / "profile"), executable_path=exe, headless=True, no_viewport=True,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}", "--window-size=1280,900"],
        )
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=20000)
        assert EXT_ID in sw.url, f"unexpected extension id: {sw.url}"
        print("extension id ok:", EXT_ID)
        if os.environ.get('BALLOONLENS_TEST_MODEL'):
            sw.evaluate('(model) => chrome.storage.local.set({model})', os.environ['BALLOONLENS_TEST_MODEL'])
        if os.environ.get("BALLOONLENS_TEST_NATIVE") == "1":
            step("Chrome native messaging starts the helper")
            setup = sw.evaluate("() => handlers.setup({checkNative: true})")
            assert setup["native"] and setup["helper"] is None, setup
            started = sw.evaluate("() => handlers.startHelper()")
            assert started["ok"], started
        sw.evaluate("""() => {
          globalThis.testJobs = { images: [], captures: [] };
          for (const [name, key] of [['translateImage', 'images'], ['translateRegion', 'captures']]) {
            const original = handlers[name];
            handlers[name] = async (...args) => {
              const result = await original(...args);
              testJobs[key].push(result.jobId);
              return result;
            };
          }
        }""")

        def completed_capture_count():
            return sw.evaluate("""async () => {
              const jobs = await Promise.all(testJobs.captures.map(jobId => handlers.getJob({jobId})));
              return jobs.filter(j => j.status === 'done').length;
            }""")

        def tab_state(tab_id):
            return sw.evaluate("(id) => handlers.tabState({ tabId: id })", tab_id)

        def wait_state(tab_id, cond, timeout=180, label=""):
            end = time.time() + timeout
            st = None
            while time.time() < end:
                st = tab_state(tab_id)
                if st and cond(st):
                    return st
                time.sleep(0.5)
            raise AssertionError(f"timeout waiting for {label}: {st}")

        step("welcome page opens on install, auto-pairs, runs the sample")
        end = time.time() + 8
        welcome = None
        while time.time() < end and not welcome:
            welcome = next((pg for pg in ctx.pages if "options.html" in pg.url), None)
            time.sleep(0.3)
        tabs = sw.evaluate("async () => (await chrome.tabs.query({})).map((t) => t.url || t.pendingUrl)")
        print("welcome auto-opened:", bool(welcome), "| tabs:", tabs)
        if not welcome:
            welcome = ctx.new_page()
            welcome.goto(f"chrome-extension://{EXT_ID}/options.html#welcome")
        elif os.environ.get("BALLOONLENS_TEST_NATIVE") == "1":
            # Native startup above bypasses the settings button's refresh callback.
            welcome.reload()
        welcome.on("console", lambda m: m.type == "error" and errors.append(f"options: {m.text}"))
        welcome.on('pageerror', lambda e: errors.append(f'options exception: {e}'))
        welcome.wait_for_selector("#step-helper.ok", timeout=30000)
        welcome.click("#step-demo .act button")
        # Extension pages forbid string eval (CSP), so poll from Python instead of wait_for_function.
        end = time.time() + 180
        demo = ""
        while time.time() < end:
            demo = welcome.inner_text("#demoStatus")
            if demo.startswith(("완료", "끝남", "번역하지 못했어요")):
                break
            time.sleep(0.5)
        print("demo:", demo)
        assert demo.startswith("완료"), f"sample translation did not finish: {demo}"
        welcome.screenshot(path=str(OUT / "01_welcome.png"), full_page=True)

        step('work profile controls save and restore')
        welcome.fill('#workUrl', 'https://example.test/manga/friends')
        welcome.fill('#workInstructions', '친구끼리는 반말')
        welcome.fill('#workGlossary', '田中 = 다나카')
        welcome.click('#saveWork')
        welcome.wait_for_selector('#workList option[value="https://example.test/manga/friends"]', state='attached')
        welcome.reload()
        welcome.select_option('#workList', 'https://example.test/manga/friends')
        assert welcome.input_value('#workInstructions') == '친구끼리는 반말'
        assert welcome.input_value('#workGlossary') == '田中 = 다나카'

        step("popup before turning on")
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and errors.append(f"page: {m.text}"))
        page.on("pageerror", lambda e: errors.append(f"page exception: {e}"))
        page.goto(PAGE)
        cdp = ctx.new_cdp_session(page)
        cdp.send('DOM.enable')

        def reading_nodes():
            # DevTools can inspect closed shadow roots without weakening production isolation.
            nodes = cdp.send('DOM.getFlattenedDocument', {'depth': -1, 'pierce': True})['nodes']
            found = []
            for node in nodes:
                attrs = dict(zip(node.get('attributes', [])[::2], node.get('attributes', [])[1::2]))
                classes = attrs.get('class', '').split()
                if 'seg' not in classes and 'cleanup' not in classes:
                    continue
                obj = cdp.send('DOM.resolveNode', {'backendNodeId': node['backendNodeId']})['object']
                data = cdp.send('Runtime.callFunctionOn', {'objectId':obj['objectId'], 'returnByValue':True,
                    'functionDeclaration':'''function() { const s=getComputedStyle(this); return {
                      className:this.className, text:this.textContent, width:this.clientWidth, height:this.clientHeight,
                      scrollWidth:this.scrollWidth, scrollHeight:this.scrollHeight, font:parseFloat(s.fontSize),
                      visible:s.visibility!=='hidden' && s.display!=='none' && !this.hidden,
                      overflow:this.classList.contains('overflow')}; }'''})['result']['value']
                found.append(data)
                cdp.send('Runtime.releaseObject', {'objectId':obj['objectId']})
            return found
        tab_id = sw.evaluate("async () => (await chrome.tabs.query({ url: 'http://127.0.0.1:8799/*' }))[0].id")
        popup = ctx.new_page()
        popup.set_viewport_size({"width": 340, "height": 640})
        popup.goto(f"chrome-extension://{EXT_ID}/popup.html?tabId={tab_id}")
        # A newly started helper may still be warming OCR; refresh connection state until ready.
        end = time.time() + 90
        while time.time() < end and "ok" not in (popup.get_attribute("#conn", "class") or "").split():
            popup.reload()
            time.sleep(1)
        popup.wait_for_selector("#conn.ok", timeout=1000)
        popup.screenshot(path=str(OUT / "02_popup_off.png"), full_page=True)

        step("turn on from popup")
        popup.click("#toggle")
        page.bring_to_front()
        st = wait_state(tab_id, lambda s: s["done"] >= 4 and s["jobsActive"] == 0 and s["pending"] == 0, label="first page")
        print("state:", st)
        time.sleep(0.8)
        page.screenshot(path=str(OUT / "03_translated.png"))
        layout = reading_nodes()
        assert any('cleanup' in n['className'] and n['visible'] for n in layout), 'no clean bubble layer'
        text_nodes = [n for n in layout if 'seg' in n['className'].split() and n['text']]
        # Tiny balloons may go below 14px, but never below 11px, and a word must not be split into one-syllable lines.
        assert text_nodes and all(n['font'] >= 11 for n in text_nodes), layout
        split = [n for n in text_nodes if len(n['text'].replace('\n', '')) > 2
                 and any(len(line.strip()) == 1 and '가' <= line.strip() <= '힣' for line in n['text'].split('\n'))]
        assert not split, split
        assert all(n['overflow'] or (n['scrollWidth'] <= n['width']+1 and n['scrollHeight'] <= n['height']+1) for n in text_nodes), layout
        (OUT/'layout.json').write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding='utf-8')
        page.evaluate("document.querySelector('img').style.height='600px'")
        time.sleep(1)
        resized = reading_nodes()
        assert all(n['overflow'] or n['scrollHeight'] <= n['height']+1 for n in resized if 'seg' in n['className'].split()), resized
        page.evaluate("document.querySelector('img').style.height=''")
        time.sleep(1)
        badge = sw.evaluate("(id) => chrome.action.getBadgeText({ tabId: id })", tab_id)
        print("toolbar badge:", repr(badge))
        assert badge == "ON" and st["failed"] == 0

        step("scroll to hotlink-protected image -> screen capture fallback")
        page.evaluate("document.querySelectorAll('img')[2].scrollIntoView({ block: 'start' })")
        st = wait_state(tab_id, lambda s: s["done"] >= 9 and s["jobsActive"] == 0 and s["pending"] == 0, label="capture (top half)")
        print("state:", st)
        time.sleep(0.8)
        page.screenshot(path=str(OUT / "04_capture_top.png"))
        # The image is taller than the viewport: scrolling on reveals the rest, which is captured separately.
        before = completed_capture_count()
        page.mouse.wheel(0, 520)
        st = wait_state(tab_id, lambda s: completed_capture_count() > before and s["done"] >= 12
                        and s["jobsActive"] == 0 and s["pending"] == 0, label="capture (bottom half)")
        print("state:", st)
        time.sleep(0.8)
        page.screenshot(path=str(OUT / "04_capture_bottom.png"))

        step("click a balloon -> popover, edit, save")
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(0.8)
        rect = page.evaluate("(() => { const r = document.querySelector('img').getBoundingClientRect(); return [r.left, r.top, r.width]; })()")
        scale = rect[2] / 1200
        x, y, w, h = FIRST_BALLOON
        page.mouse.click(rect[0] + (x + w / 2) * scale, rect[1] + (y + h / 2) * scale)
        time.sleep(0.6)
        page.screenshot(path=str(OUT / "05_popover.png"))
        page.keyboard.press("Control+A")
        page.keyboard.insert_text("어디 가는 거야?")
        page.keyboard.press("Control+Enter")
        time.sleep(1.2)
        page.screenshot(path=str(OUT / "06_saved.png"))
        assert tab_state(tab_id)["edited"] == 1, "edit not applied"

        step("popup while running")
        popup.bring_to_front()
        popup.reload()
        popup.wait_for_selector("#progress:not([hidden])", timeout=10000)
        time.sleep(1)
        popup.screenshot(path=str(OUT / "07_popup_on.png"), full_page=True)

        step("reload page -> edit persisted by the helper")
        page.bring_to_front()
        page.reload()
        sw.evaluate("(id) => handlers.start({ tabId: id })", tab_id)
        st = wait_state(tab_id, lambda s: s["done"] >= 4 and s["pending"] == 0 and s["jobsActive"] == 0, label="after reload")
        print("state:", st)
        assert st["edited"] >= 1, "saved edit did not come back after reload"
        saved = sw.evaluate("""async () => {
          const jobs = await Promise.all(testJobs.images.map(jobId => handlers.getJob({jobId})));
          return jobs.some(j => j.segments.some(s => s.edited && s.tgt === '어디 가는 거야?'));
        }""")
        assert saved, "saved Korean text was not preserved exactly"
        time.sleep(0.8)
        page.screenshot(path=str(OUT / "08_after_reload.png"))

        step("peek original + stop")
        sw.evaluate("(id) => handlers.peek({ tabId: id, on: true })", tab_id)
        time.sleep(0.4)
        page.screenshot(path=str(OUT / "09_peek.png"))
        assert all(not n['visible'] for n in reading_nodes()), 'peek left translated pixels visible'
        sw.evaluate("(id) => handlers.stop({ tabId: id })", tab_id)
        time.sleep(0.5)
        assert not tab_state(tab_id)["mounted"], "stop did not unmount"

        step("manual region selection")
        before = completed_capture_count()
        sw.evaluate("(id) => handlers.region({ tabId: id })", tab_id)
        page.mouse.move(rect[0] + (x - 45) * scale, rect[1] + (y - 40) * scale)
        page.mouse.down()
        page.mouse.move(rect[0] + (x + w + 45) * scale, rect[1] + (y + h + 40) * scale, steps=10)
        page.mouse.up()
        st = wait_state(tab_id, lambda s: completed_capture_count() > before and s["done"] > 0
                        and s["pending"] == 0 and s["jobsActive"] == 0, label="manual region")
        assert not st["running"] and st["mounted"] and st["failed"] == 0
        page.screenshot(path=str(OUT / "10_region.png"))
        sw.evaluate("(id) => handlers.stop({ tabId: id })", tab_id)

        step("opted-in site starts automatically after reload")
        sw.evaluate("() => chrome.storage.local.set({autoSites: ['127.0.0.1']})")
        page.reload()
        st = wait_state(tab_id, lambda s: s["running"] and s["done"] >= 4 and s["jobsActive"] == 0,
                        label="automatic site")
        sw.evaluate("(id) => handlers.stop({ tabId: id })", tab_id)

        step("fresh English translation with both installed models")
        for model in ("translategemma:4b", "qwen3:8b"):
            result = sw.evaluate("""model => handlers.translateLine({text: 'Where are you going?',
              src: 'en', tgt: 'ko', model, scope: 'e2e', fresh: true})""", model)
            assert result["status"] == "done" and result["text"], result
            print(model, result["text"])

        step('a failed earlier image does not stall the remaining queue')
        sw.evaluate("""() => {
          globalThis.originalGetJob = handlers.getJob;
          globalThis.failedJobIds = [];
          handlers.getJob = async message => {
            if (failedJobIds.length < 2 && !failedJobIds.includes(message.jobId)) failedJobIds.push(message.jobId);
            if (failedJobIds.includes(message.jobId)) throw new Error('not_found');
            return originalGetJob(message);
          };
        }""")
        page.reload()
        st=wait_state(tab_id, lambda s: s['done']>=4 and s['jobsActive']==0 and s['lastError']=='not_found', label='queue recovery')
        sw.evaluate("() => { handlers.getJob = originalGetJob; }")
        sw.evaluate("(id) => handlers.stop({tabId:id})", tab_id)

        ctx.close()
    server.shutdown()
    print("\nconsole errors:", errors or "none")
    assert not errors, f"browser errors: {errors}"
    print(f"screenshots: {OUT}")
    (OUT/'result.json').write_text(json.dumps({'passed': True, 'console_errors': errors,
        'model': os.environ.get('BALLOONLENS_TEST_MODEL', 'default'),
        'checks':['profile persistence','translation','cleanup','text fit','height resize','capture','edit persistence','peek','region','auto site','queue recovery']}), encoding='utf-8')


if __name__ == "__main__":
    main()
