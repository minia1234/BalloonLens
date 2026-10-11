"""End-to-end: region editor, glossary quick-add and work memory with the real extension and a real,
isolated helper (port 8788, throwaway data folder), so the reader's own cache and edits are untouched.

Needs Ollama with the chosen model (default qwen3.5:9b). Screenshots go to bench_out/regions/<run>/.
Usage: helper\\.venv\\Scripts\\python.exe tools\\e2e_regions.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

from e2e_extension import EXT_ID, FIRST_BALLOON, ROOT, Handler, step

PORT = 8788
OUT = ROOT / "bench_out" / "regions" / str(time.time_ns())
WORK = "http://127.0.0.1:8799/tools"
PAGE = "http://127.0.0.1:8799/tools/e2e_page.html"
MODEL = os.environ.get("BALLOONLENS_TEST_MODEL", "qwen3.5:9b")
# Detected boxes on samples/synthetic/ja_page.png (reading order ids 0..3).
LEFT_TOP = (298, 266, 44, 218)


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_helper():
    if port_open(PORT):
        raise SystemExit(f"port {PORT} is busy; stop the other test helper first")
    env = dict(os.environ, BALLOONLENS_PORT=str(PORT), BALLOONLENS_DATA=str(OUT / "data"))
    log = open(OUT / "helper.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "balloonlens", "--warmup"], cwd=ROOT / "helper", env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        if port_open(PORT):
            return proc
        time.sleep(0.5)
    proc.kill()
    raise SystemExit("isolated helper did not start; see helper.log")


def main():
    OUT.mkdir(parents=True)
    ext = OUT / "ext"
    shutil.copytree(ROOT / "extension", ext)
    manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    manifest["host_permissions"].append("<all_urls>")
    (ext / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for name in ("manifest.json", "sw.js"):
        path = ext / name
        path.write_text(path.read_text(encoding="utf-8").replace("127.0.0.1:8787", f"127.0.0.1:{PORT}"), encoding="utf-8")

    helper = start_helper()
    server = ThreadingHTTPServer(("127.0.0.1", 8799), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    errors, report = [], {"model": MODEL}
    try:
        with sync_playwright() as p:
            exe = None
            if not Path(p.chromium.executable_path).exists():
                found = sorted(Path(p.chromium.executable_path).parents[2].glob("chromium-*/chrome-win64/chrome.exe"))
                exe = str(found[-1]) if found else None
            ctx = p.chromium.launch_persistent_context(
                str(OUT / "profile"), executable_path=exe, headless=True, no_viewport=True,
                args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}", "--window-size=1280,900"])
            sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=20000)
            assert EXT_ID in sw.url
            sw.evaluate("""([model, work]) => chrome.storage.local.set({model,
              workProfiles: [{url: work, instructions: '친구끼리는 반말', glossary: ''}]})""", [MODEL, WORK])
            sw.evaluate("""() => {
              globalThis.testJobs = [];
              const original = handlers.translateImage;
              handlers.translateImage = async (...args) => {
                const result = await original(...args);
                testJobs.push(result.jobId);
                return result;
              };
            }""")
            for pg in ctx.pages:
                pg.on("pageerror", lambda e: errors.append(f"extension page: {e}"))

            page = ctx.new_page()
            page.on("console", lambda m: m.type == "error" and errors.append(f"page: {m.text}"))
            page.on("pageerror", lambda e: errors.append(f"page exception: {e}"))
            cdp = ctx.new_cdp_session(page)
            cdp.send("DOM.enable")

            def shadow(match):
                """Call fn on the first node inside our closed shadow root whose attributes satisfy match."""
                nodes = cdp.send("DOM.getFlattenedDocument", {"depth": -1, "pierce": True})["nodes"]
                for node in nodes:
                    attrs = dict(zip(node.get("attributes", [])[::2], node.get("attributes", [])[1::2]))
                    if match(attrs):
                        return node["backendNodeId"]
                return None

            def call(backend_id, fn):
                obj = cdp.send("DOM.resolveNode", {"backendNodeId": backend_id})["object"]
                value = cdp.send("Runtime.callFunctionOn", {"objectId": obj["objectId"], "returnByValue": True,
                                                            "functionDeclaration": fn})["result"].get("value")
                cdp.send("Runtime.releaseObject", {"objectId": obj["objectId"]})
                return value

            def click_shadow(match, label):
                node = shadow(match)
                assert node, f"{label} not found"
                call(node, "function() { this.click(); }")

            def center(match, label):
                node = shadow(match)
                assert node, f"{label} not found"
                r = call(node, "function() { const r = this.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }")
                return r[0] + r[2] / 2, r[1] + r[3] / 2, r

            def tab_state():
                return sw.evaluate("(id) => handlers.tabState({ tabId: id })", tab_id)

            def wait_state(cond, label, timeout=300):
                end = time.time() + timeout
                st = None
                while time.time() < end:
                    st = tab_state()
                    if st and cond(st):
                        return st
                    time.sleep(0.5)
                raise AssertionError(f"timeout waiting for {label}: {st}")

            def first_image_job():
                return sw.evaluate("""async () => {
                  for (const jobId of [...testJobs].reverse()) {
                    const job = await handlers.getJob({jobId}).catch(() => null);
                    if (job && job.width === 1200 && job.segments.length && ['done','partial'].includes(job.status)) return job;
                  }
                  return null;
                }""")

            step("chapter 1: translate, remembered by the work")
            page.goto(f"{PAGE}?chapter=1")
            tab_id = sw.evaluate("async () => (await chrome.tabs.query({ url: 'http://127.0.0.1:8799/*' }))[0].id")
            sw.evaluate("(id) => handlers.start({ tabId: id })", tab_id)
            st = wait_state(lambda s: s["done"] >= 8 and s["jobsActive"] == 0 and s["pending"] == 0, "chapter 1")
            print("state:", st)
            memory = sw.evaluate("(scope) => handlers.getWork({scope})", WORK)
            assert memory["pages"]["count"] >= 2, memory
            job1 = first_image_job()
            report["chapter1_timings"] = job1["timings"]
            print("first image timings:", job1["timings"])

            step("fix a translation, then open the region editor")
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.5)
            img = page.evaluate("(() => { const r = document.querySelector('img').getBoundingClientRect(); return [r.left, r.top, r.width]; })()")
            scale = img[2] / 1200
            to_page = lambda x, y: (img[0] + x * scale, img[1] + y * scale)
            fx, fy, fw, fh = FIRST_BALLOON
            page.mouse.click(*to_page(fx + fw / 2, fy + fh / 2))
            time.sleep(0.4)
            page.keyboard.press("Control+A")
            page.keyboard.insert_text("어디 가? (고침)")
            page.keyboard.press("Control+Enter")
            time.sleep(1)
            click_shadow(lambda a: a.get("data-act") == "edit", "pill edit button")
            time.sleep(0.5)
            page.screenshot(path=str(OUT / "01_editor.png"))

            step("region editor: move the fixed region, delete one, draw it again, resize a translation box")
            page.mouse.click(*to_page(fx + fw / 2, fy + fh / 2))
            page.keyboard.press("ArrowRight")
            page.keyboard.press("ArrowRight")
            x, y, w, h = LEFT_TOP
            page.mouse.click(*to_page(x + w / 2, y + h / 2))
            page.keyboard.press("Delete")
            click_shadow(lambda a: a.get("data-act") == "add", "add button")
            page.mouse.move(*to_page(x - 6, y - 6))
            page.mouse.down()
            page.mouse.move(*to_page(x + w + 6, y + h + 6), steps=8)
            page.mouse.up()
            click_shadow(lambda a: a.get("data-mode") == "frame", "frame mode")
            page.mouse.click(*to_page(fx + fw / 2, fy + fh / 2))
            hx, hy, _ = center(lambda a: a.get("data-h") == "se", "resize handle")
            page.mouse.move(hx, hy)
            page.mouse.down()
            page.mouse.move(hx + 40, hy + 30, steps=6)
            page.mouse.up()
            time.sleep(0.3)
            page.screenshot(path=str(OUT / "02_editor_changed.png"))
            before_jobs = sw.evaluate("testJobs.length")
            click_shadow(lambda a: a.get("data-act") == "save", "save button")
            end = time.time() + 30
            while time.time() < end and sw.evaluate("testJobs.length") == before_jobs:
                time.sleep(0.3)
            wait_state(lambda s: s["jobsActive"] == 0 and s["pending"] == 0 and s["done"] >= 8, "re-translation")
            job = first_image_job()
            layout = job["layout"]
            print("layout:", json.dumps(layout, ensure_ascii=False))
            first_key = ",".join(map(str, FIRST_BALLOON))
            assert layout["removed"] == sorted([",".join(map(str, LEFT_TOP)), first_key]), layout
            assert len(layout["added"]) == 2 and layout["frames"] == {}, layout
            assert sum(1 for a in layout["added"] if a["frame"]) == 1 and all("moved_from" not in a for a in layout["added"])
            user = [s for s in job["segments"] if s["origin"]["kind"] == "user"]
            moved = next(s for s in user if s["bbox"][0] == fx + 2)
            print("moved region:", moved["src"], "->", moved["tgt"], moved["edited"])
            assert moved["edited"] and moved["tgt"] == "어디 가? (고침)", moved  # the fix followed the move
            added = next(s for s in user if s is not moved)
            print("reader region:", added["src"], "->", added["tgt"], added["status"])
            assert "静" in added["src"] and added["tgt"], added
            framed = [s for s in job["segments"] if s["presentation"].get("fill")]
            assert len(framed) == 1 and framed[0]["presentation"]["frame"][2] > fw, framed
            report["layout"] = layout
            report["reader_region"] = {"src": added["src"], "tgt": added["tgt"], "status": added["status"]}
            time.sleep(0.8)
            page.screenshot(path=str(OUT / "03_after_regions.png"))

            step("glossary quick-add from a balloon popover")
            page.mouse.click(*to_page(fx + fw / 2, fy + fh / 2))
            time.sleep(0.4)
            click_shadow(lambda a: a.get("data-act") == "term", "term button")
            page.keyboard.insert_text("どこ")
            page.keyboard.press("Tab")
            page.keyboard.insert_text("어디")
            page.keyboard.press("Enter")
            end = time.time() + 10
            glossary = ""
            while time.time() < end and "どこ = 어디" not in glossary:
                glossary = sw.evaluate("async () => (await chrome.storage.local.get('workProfiles')).workProfiles[0].glossary")
                time.sleep(0.3)
            assert glossary == "どこ = 어디", glossary
            wait_state(lambda s: s["done"] >= 8 and s["jobsActive"] == 0 and s["pending"] == 0, "re-translation with term")

            step("chapter 2: earlier chapter is context, unchanged images are reused")
            before_jobs = sw.evaluate("testJobs.length")
            page.goto(f"{PAGE}?chapter=2")
            sw.evaluate("(id) => handlers.start({ tabId: id })", tab_id)
            wait_state(lambda s: s["done"] >= 8 and s["jobsActive"] == 0 and s["pending"] == 0, "chapter 2")
            jobs = sw.evaluate("""async (from) => Promise.all(testJobs.slice(from).map(jobId => handlers.getJob({jobId})))""",
                               before_jobs)
            timings = [j["timings"] for j in jobs if j["status"] in ("done", "partial")]
            print("chapter 2 timings:", timings)
            assert all(j["memory"] for j in jobs), "memory was not on for the registered work"
            assert any(t.get("context_lines", 0) > 0 for t in timings), timings
            assert any(t.get("reused_lines", 0) > 0 for t in timings), timings
            report["chapter2_timings"] = timings
            page.screenshot(path=str(OUT / "04_chapter2.png"))
            sw.evaluate("(id) => handlers.stop({ tabId: id })", tab_id)

            step("options: memory stats, confirmed notes saved to the helper")
            options = ctx.new_page()
            options.on("pageerror", lambda e: errors.append(f"options exception: {e}"))
            options.goto(f"chrome-extension://{EXT_ID}/options.html?workUrl={PAGE}")
            options.wait_for_selector("#memoryStats:has-text('기억한 이미지')", timeout=15000)
            print("memory stats:", options.inner_text("#memoryStats"))
            options.fill("#workTitle", "테스트 만화")
            options.fill("#workCharacters", "ユキ = 유키 · 반말")
            options.click("#saveWork")
            options.wait_for_selector("#workStatus:has-text('저장했어요')", timeout=10000)
            saved = sw.evaluate("(scope) => handlers.getWork({scope})", WORK)
            assert saved["work"]["title"] == "테스트 만화" and "유키" in saved["work"]["characters"], saved
            assert options.input_value("#workGlossary") == "どこ = 어디"
            options.locator("#memoryStats").scroll_into_view_if_needed()
            options.screenshot(path=str(OUT / "05_options_memory.png"), full_page=True)
            ctx.close()
    finally:
        server.shutdown()
        server.server_close()
        helper.terminate()
        helper.wait(timeout=20)
    print("\nconsole errors:", errors or "none")
    assert not errors, errors
    (OUT / "result.json").write_text(json.dumps({"passed": True, **report}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"passed · {OUT}")


if __name__ == "__main__":
    main()
