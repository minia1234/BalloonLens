"""End-to-end check of the subscription path with a real helper process and fake CLIs. No browser, no model.

What it proves, against a helper started with `python -m balloonlens` on port 8788:
  - one call per translation, with the line text on stdin only (never in argv or env)
  - the API keys in the helper's environment are not handed to the CLI
  - app-imposed call caps are removed and successive translations continue
  - no dialogue text in the helper's log, or in the helper's SQLite file
Run:  python tools/e2e_subscription.py   (exit code 0 = every check passed)
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

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "helper"
FIXTURES = HELPER / "tests" / "fixtures"
sys.path.insert(0, str(HELPER))

from balloonlens import config  # noqa: E402

PORT = 8788
ORIGIN = "chrome-extension://e2eextensionid"
DIALOGUE = "待って！"
SECRET_ENV = "sk-ant-api03-E2ELEAKCHECK"
ALLOWED_PAYLOAD_KEYS = {"task", "src", "tgt", "target_ids", "page", "context", "glossary", "fixed",
                        "ocr_uncertain_ids", "work_notes", "drafts", "retry_reasons"}

checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))


def call(method, path, body=None, token=None, origin=None, raw=None):
    headers = {}
    data = None
    if raw is not None:
        data = raw
        headers["Content-Type"] = "image/png"
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


class Helper:
    """One helper process. Stopping and starting it again keeps the same data folder."""

    def __init__(self, data, claude_log):
        self.data = data
        self.log_path = data / "e2e-helper.log"
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env.update({
            "BALLOONLENS_DATA": str(data),
            "BALLOONLENS_PORT": str(PORT),
            "BALLOONLENS_EXTENSION_IDS": "e2eextensionid",
            "BALLOONLENS_CLAUDE_CONFIG_DIR": str(data / "claude-config"),
            "BALLOONLENS_CLAUDE_COMMAND": json.dumps([sys.executable, str(FIXTURES / "fake_claude.py"),
                                                      "--mode", "ok", "--log", str(claude_log)]),
            "ANTHROPIC_API_KEY": SECRET_ENV,
        })
        self.out = open(self.log_path, "a", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-m", "balloonlens"], cwd=str(HELPER), env=env,
                                     stdout=self.out, stderr=subprocess.STDOUT)
        self.token = None
        for _ in range(60):
            if self.proc.poll() is not None:
                break
            try:
                status, data = call("POST", "/v1/pair", origin=ORIGIN)
            except OSError:  # not listening yet
                time.sleep(0.5)
                continue
            if status == 200:
                self.token = data["token"]
                return
            time.sleep(0.5)
        raise RuntimeError("helper did not start; see " + str(self.log_path))

    def request(self, method, path, body=None):
        return call(method, path, body, token=self.token)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.out.close()


def translate_line(helper, model, text=DIALOGUE):
    return helper.request("POST", "/v1/translate-line", {"text": text, "src": "ja", "tgt": "ko", "model": model})[1]


def phase_one(data, claude_log):
    print("Phase 1: one call per line, no app-imposed usage cap")
    helper = Helper(data, claude_log)
    try:
        backends = {b["id"]: b for b in helper.request("GET", "/v1/backends")[1]["backends"]}
        check("the Claude backend is the only subscription backend", set(backends) == {"claude"})

        done = translate_line(helper, config.CLAUDE_MODEL)
        check("Claude translates one line", done.get("status") == "done" and done.get("text") == "번역문", str(done))
        rows = log_rows(claude_log)
        check("exactly one Claude call", len(rows) == 1, str(len(rows)))
        if rows:
            call_row = rows[0]
            argv_text = " ".join(call_row["argv"])
            check("dialogue is not on the command line", DIALOGUE not in argv_text)
            check("dialogue goes through stdin", DIALOGUE in call_row["stdin"])
            check("API keys are not handed to the CLI", "ANTHROPIC_API_KEY" not in call_row["env"])
            payload = json.loads(call_row["stdin"])
            check("payload has text fields only (no image)", set(payload) <= ALLOWED_PAYLOAD_KEYS,
                  str(sorted(set(payload) - ALLOWED_PAYLOAD_KEYS)))

        removed, _ = helper.request("PUT", "/v1/backends/claude/caps", {"per_5h": 1, "per_7d": 5})
        check("the app-imposed cap endpoint is removed", removed == 404)
        continued = translate_line(helper, config.CLAUDE_MODEL, "またね。")
        check("the next translation continues without an app cap", continued.get("status") == "done", str(continued))
        check("two translations made exactly two calls", len(log_rows(claude_log)) == 2)
    finally:
        helper.stop()


def phase_three(data, claude_log):
    """A real page image through the real OCR, then the Claude path with the fake CLI (no subscription usage)."""
    print("Phase 3: a page image through OCR and the Claude path (fake CLI)")
    helper = Helper(data, claude_log)
    try:
        image =(ROOT / "samples" / "synthetic" / "ja_small.png").read_bytes()
        before = len(log_rows(claude_log))
        status, created = call("POST", f"/v1/jobs?src=ja&tgt=ko&model={config.CLAUDE_MODEL}", raw=image,
                               token=helper.token)
        check("the page job is accepted", status == 200 and "id" in created, str(created))
        snap = {}
        deadline = time.time() + 300
        while time.time() < deadline:
            snap = helper.request("GET", f"/v1/jobs/{created.get('id', '')}")[1]
            if snap.get("status") in ("done", "partial", "failed", "cancelled"):
                break
            time.sleep(1)
        check("the page job finishes", snap.get("status") in ("done", "partial"),
              f"{snap.get('status')} {snap.get('error')}")
        texts = [s.get("tgt") for s in snap.get("segments", []) if s.get("tgt")]
        check("the page has lines translated by the Claude path", bool(texts) and all(t == "번역문" for t in texts),
              str(texts[:3]))
        rows = log_rows(claude_log)[before:]
        check("the page used one Claude call", len(rows) == 1, str(len(rows)))
        metrics = helper.data / "metrics.jsonl"
        job_rows = []
        deadline = time.time() + 10
        while time.time() < deadline:
            job_rows = [json.loads(line) for line in metrics.read_text(encoding="utf-8").splitlines() if line.strip()] \
                if metrics.exists() else []
            if any(r.get("model") == config.CLAUDE_MODEL for r in job_rows):
                break
            time.sleep(0.5)
        page_row = next((r for r in reversed(job_rows) if r.get("model") == config.CLAUDE_MODEL), {})
        check("the metrics row for the page carries token counts", "tokens_in" in page_row and "tokens_out" in page_row,
              str(sorted(page_row)))
        if rows:
            payload = json.loads(rows[0]["stdin"])
            check("the page call carries text only (no image bytes)",
                  set(payload) <= ALLOWED_PAYLOAD_KEYS and "base64" not in rows[0]["stdin"],
                  str(sorted(set(payload) - ALLOWED_PAYLOAD_KEYS)))
    finally:
        helper.stop()


def scan_for_dialogue(data):
    hits = []
    for path in list(data.glob("*.log")) + list(data.glob("*.jsonl")) + list(data.glob("*.sqlite3")):
        if DIALOGUE.encode("utf-8") in path.read_bytes():
            hits.append(path.name)
    return hits


def main():
    temp = Path(tempfile.mkdtemp(prefix="bl-e2e-"))
    data = temp / "data"
    data.mkdir()
    claude_log = temp / "claude-calls.jsonl"
    try:
        phase_one(data, claude_log)
        if "--with-ocr" in sys.argv:
            phase_three(data, claude_log)
        leaks = scan_for_dialogue(data)
        check("no dialogue text in the helper log or the database", not leaks, ", ".join(leaks))
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    passed = sum(1 for _, ok in checks if ok)
    print(f"\nSubscription E2E: {passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
