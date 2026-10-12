"""Subscription read-ahead in the real extension (plan §8): auto mode on a page of eight images with the Claude model.

Expected: the first screen (three images) plus the image right below it, so four page jobs. Scrolling down one screen
brings in two more images on screen and one below, so seven jobs in total. A local model would queue every image inside
the 150% margin at once; a subscription call must not.
The counts come from the helper's metrics file (one row per finished page job), so the translation cache does not
change them. The helper is isolated (port 8788, throwaway data) and uses fake CLIs, so no subscription usage is spent.
Run:  python tools/e2e_subscription_auto.py   (exit code 0 = every check passed)
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "helper"
FIXTURES = HELPER / "tests" / "fixtures"
sys.path.insert(0, str(HELPER))
from balloonlens import config  # noqa: E402

EXT_ID = "pnccemlpjodglelglkjjdnnmdknejgop"  # fixed by the manifest key; the helper accepts only this one
ORIGIN = f"chrome-extension://{EXT_ID}"
HELPER_PORT = 8788
PAGE_PORT = 8798
IMAGES = 8
FIRST_SCREEN_JOBS = 4  # three images on screen plus the image right below
SCROLLED_JOBS = 7      # after one screen down: five images on screen plus the image right below
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))


def http(method, path, body=None, token=None, origin=None):
    headers, data = {}, None
    if token:
        headers["X-BalloonLens-Token"] = token
    if origin:
        headers["Origin"] = origin
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{HELPER_PORT}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def port_busy(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_until(predicate, timeout=30.0, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(step)
    return False


def page_jobs(path):
    """One metrics row per finished page job; keep the Claude ones."""
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("model") == config.CLAUDE_MODEL]


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Helper:
    def __init__(self, data, claude_log):
        self.data = data
        self.out = open(data / "auto-helper.log", "a", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env.update({
            "BALLOONLENS_DATA": str(data),
            "BALLOONLENS_PORT": str(HELPER_PORT),
            "BALLOONLENS_CLAUDE_CONFIG_DIR": str(data / "claude-config"),
            "BALLOONLENS_CLAUDE_COMMAND": json.dumps([sys.executable, str(FIXTURES / "fake_claude.py"),
                                                      "--mode", "ok", "--log", str(claude_log)]),
        })
        self.proc = subprocess.Popen([sys.executable, "-m", "balloonlens"], cwd=str(HELPER), env=env,
                                     stdout=self.out, stderr=subprocess.STDOUT)
        self.token = None
        for _ in range(60):
            if self.proc.poll() is not None:
                break
            try:
                status, body = http("POST", "/v1/pair", origin=ORIGIN)
            except OSError:  # not listening yet
                time.sleep(0.5)
                continue
            if status == 200:
                self.token = body["token"]
                return
            time.sleep(0.5)
        raise RuntimeError(f"helper did not start; see {data / 'auto-helper.log'}")

    def put(self, path, body):
        return http("PUT", path, body, token=self.token)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.out.close()


def make_site(site):
    """Eight tall images stacked on one page, each 300 px on screen (one screen is 800 px)."""
    (site / "img").mkdir(parents=True)
    sample = (ROOT / "samples" / "synthetic" / "ja_small.png").read_bytes()
    for i in range(1, IMAGES + 1):
        (site / "img" / f"{i}.png").write_bytes(sample)
    tags = "".join(f'<img src="img/{i}.png" alt="">' for i in range(1, IMAGES + 1))
    (site / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>read-ahead</title>'
        "<style>body{margin:0}img{display:block;width:300px;height:300px}</style>"
        f"<body>{tags}</body>", encoding="utf-8")


def copy_extension(out):
    ext = out / "extension"
    shutil.copytree(ROOT / "extension", ext)
    manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    # Automation can't click Chrome's permission prompt, so grant page access up front in the test copy.
    manifest["host_permissions"].append("<all_urls>")
    (ext / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    # The test copy talks to the test helper port; the manifest key (and so the extension ID) stays the same.
    for name in ("manifest.json", "sw.js"):
        path = ext / name
        path.write_text(path.read_text(encoding="utf-8").replace("127.0.0.1:8787", f"127.0.0.1:{HELPER_PORT}"),
                        encoding="utf-8")
    return ext


def main():
    if port_busy(HELPER_PORT):
        raise SystemExit(f"port {HELPER_PORT} is busy; stop the old test helper first")
    temp = Path(tempfile.mkdtemp(prefix="bl-auto-"))
    out = ROOT / "bench_out" / "subscription-auto" / str(time.time_ns())
    out.mkdir(parents=True)
    data = out / "data"
    data.mkdir()
    site = out / "site"
    make_site(site)
    server = ThreadingHTTPServer(("127.0.0.1", PAGE_PORT), partial(Quiet, directory=str(site)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    metrics = data / "metrics.jsonl"
    errors = []
    helper = None
    try:
        helper = Helper(data, out / "claude-calls.jsonl")
        ext = copy_extension(out)
        with sync_playwright() as p:
            exe = None
            if not Path(p.chromium.executable_path).exists():
                candidates = sorted(Path(p.chromium.executable_path).parents[2].glob("chromium-*/chrome-win64/chrome.exe"))
                exe = str(candidates[-1]) if candidates else None
            ctx = p.chromium.launch_persistent_context(
                str(out / "profile"), headless=True, executable_path=exe,
                args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
            sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=20000)
            check("extension service worker runs under the fixed ID", EXT_ID in sw.url, sw.url)
            sw.evaluate("async () => { await chrome.storage.local.set({model: 'claude-haiku-5-5', autoSites: ['127.0.0.1']}); }")

            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(f"page: {e}"))
            page.set_viewport_size({"width": 1280, "height": 800})
            page.goto(f"http://127.0.0.1:{PAGE_PORT}/index.html")

            wait_until(lambda: len(page_jobs(metrics)) >= FIRST_SCREEN_JOBS, timeout=240)
            time.sleep(8)  # long enough for any extra job to finish and show up
            first = len(page_jobs(metrics))
            check(f"first screen: {FIRST_SCREEN_JOBS} page jobs (three on screen plus the one below)",
                  first == FIRST_SCREEN_JOBS, f"{first} jobs")

            page.evaluate("window.scrollTo(0, 800)")
            wait_until(lambda: len(page_jobs(metrics)) >= SCROLLED_JOBS, timeout=240)
            time.sleep(8)
            scrolled = len(page_jobs(metrics))
            check(f"one screen down: {SCROLLED_JOBS} page jobs in total", scrolled == SCROLLED_JOBS, f"{scrolled} jobs")
            ctx.close()
    finally:
        if helper is not None:
            helper.stop()
        server.shutdown()

    check("no page errors in the test page", not errors, "; ".join(errors))
    shutil.rmtree(temp, ignore_errors=True)
    passed = sum(1 for _, ok in checks if ok)
    print(f"\nSubscription read-ahead E2E: {passed}/{len(checks)} checks passed")
    print(f"screenshots and logs: {out}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
