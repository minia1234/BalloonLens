"""Subscription path end to end: the real extension pages and service worker talk to a real helper process.

The helper runs with a fake claude CLI (no subscription use, no model calls, no Ollama). The extension is a
test copy that points at the helper's test port. Checks go through the extension's own message handlers and pages:
  - the popup shows Claude ready with no extra step; a line translated through the Claude path comes back
  - the usage cap blocks the next call
  - the settings page shows the Claude card as ready, with no consent button
  - no dialogue text in the helper's log or database
Run:  python tools/e2e_subscription_live.py   (exit code 0 = every check passed)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "helper"
FIXTURES = HELPER / "tests" / "fixtures"
sys.path.insert(0, str(HELPER))
from balloonlens import config  # noqa: E402

EXT_ID = "pnccemlpjodglelglkjjdnnmdknejgop"  # fixed by the manifest key; the helper accepts only this one
ORIGIN = f"chrome-extension://{EXT_ID}"
PORT = 8788
DIALOGUE = "待って！"
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
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def log_rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def wait_until(predicate, timeout=30.0, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(step)
    return False


class Helper:
    def __init__(self, data, claude_log):
        self.log_path = data / "live-helper.log"
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env.update({
            "BALLOONLENS_DATA": str(data),
            "BALLOONLENS_PORT": str(PORT),
            "BALLOONLENS_CLAUDE_CONFIG_DIR": str(data / "claude-config"),
            "BALLOONLENS_CLAUDE_COMMAND": json.dumps([sys.executable, str(FIXTURES / "fake_claude.py"),
                                                      "--mode", "ok", "--log", str(claude_log)]),
            "ANTHROPIC_API_KEY": "sk-ant-api03-LIVELEAKCHECK",
        })
        self.out = open(self.log_path, "a", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-m", "balloonlens"], cwd=str(HELPER), env=env,
                                     stdout=self.out, stderr=subprocess.STDOUT)
        self.token = None
        for _ in range(60):
            if self.proc.poll() is not None:
                break
            try:
                status, data_ = http("POST", "/v1/pair", origin=ORIGIN)
            except OSError:  # not listening yet
                time.sleep(0.5)
                continue
            if status == 200:
                self.token = data_["token"]
                return
            time.sleep(0.5)
        raise RuntimeError(f"helper did not start; see {self.log_path}")

    def get(self, path):
        return http("GET", path, token=self.token)[1]

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


def backend_row(helper, name):
    return next((b for b in helper.get("/v1/backends")["backends"] if b["id"] == name), {})


def main():
    temp = Path(tempfile.mkdtemp(prefix="bl-live-"))
    out = ROOT / "bench_out" / "subscription-live" / str(time.time_ns())
    out.mkdir(parents=True)
    data = out / "data"
    data.mkdir()
    claude_log = out / "claude-calls.jsonl"
    ext = out / "extension"
    shutil.copytree(ROOT / "extension", ext)
    # The test copy talks to the test helper port; the manifest key (and so the extension ID) stays the same.
    for name in ("manifest.json", "sw.js"):
        path = ext / name
        path.write_text(path.read_text(encoding="utf-8").replace("127.0.0.1:8787", f"127.0.0.1:{PORT}"), encoding="utf-8")
    errors = []
    helper = None
    try:
        helper = Helper(data, claude_log)
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

            popup = ctx.new_page()
            popup.on("pageerror", lambda e: errors.append(f"popup: {e}"))
            popup.goto(f"{ORIGIN}/popup.html?tabId=1&url=https%3A%2F%2Fexample.test%2Fmanga")
            check("the popup shows Claude ready without a consent step",
                  wait_until(lambda: "준비됨" in popup.inner_text("#conn"), 30), popup.inner_text("#conn"))

            line = sw.evaluate("""async () => await handlers.translateLine({text: '待って！', src: 'ja', tgt: 'ko',
                model: 'claude-haiku-5-5', scope: '', fresh: true, options: {}, page: []})""")
            check("a line through the Claude path comes back translated",
                  line.get("status") == "done" and line.get("text") == "번역문", str(line))
            rows = log_rows(claude_log)
            check("exactly one Claude call so far", len(rows) == 1, str(len(rows)))
            if rows:
                check("the line is on stdin, not on the command line",
                      DIALOGUE not in " ".join(rows[0]["argv"]) and DIALOGUE in rows[0]["stdin"])
                check("the API key from the helper environment is not passed on",
                      "ANTHROPIC_API_KEY" not in rows[0]["env"])

            helper.put("/v1/backends/claude/caps", {"per_5h": 1, "per_7d": 150})
            capped = sw.evaluate("""async () => await handlers.translateLine({text: 'またね。', src: 'ja', tgt: 'ko',
                model: 'claude-haiku-5-5', scope: '', fresh: true, options: {}, page: []})""")
            check("the usage cap stops the next Claude call", capped.get("error") == "quota_paused", str(capped))
            check("no process starts while capped", len(log_rows(claude_log)) == 1)
            helper.put("/v1/backends/claude/caps", {"per_5h": 20, "per_7d": 150})

            options = ctx.new_page()
            options.on("pageerror", lambda e: errors.append(f"options: {e}"))
            options.goto(f"{ORIGIN}/options.html")
            options.wait_for_selector("#models .backend-card", timeout=30000)
            cards = options.locator("#models .backend-card")
            check("settings shows the Claude card", cards.count() == 1, str(cards.count()))
            check("the Claude card says ready", "준비됨" in cards.nth(0).inner_text(), cards.nth(0).inner_text()[:120])
            check("the Claude card has no consent button", cards.nth(0).get_by_role("button", name="사용에 동의").count() == 0)
            options.screenshot(path=str(out / "options-subscriptions.png"), full_page=True)
            ctx.close()
    finally:
        if helper is not None:
            helper.stop()

    leaks = []
    for path in list(data.glob("*.log")) + list(data.glob("*.sqlite3")):
        if DIALOGUE.encode("utf-8") in path.read_bytes():
            leaks.append(path.name)
    check("no dialogue text in the helper log or database", not leaks, ", ".join(leaks))
    check("no page errors in the extension pages", not errors, "; ".join(errors))
    shutil.rmtree(temp, ignore_errors=True)
    passed = sum(1 for _, ok in checks if ok)
    print(f"\nSubscription live E2E: {passed}/{len(checks)} checks passed")
    print(f"screenshots and logs: {out}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
