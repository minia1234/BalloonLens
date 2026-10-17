"""Popup and settings page for the subscription backends, in headless Chromium with a mocked helper.

Checks what the reader sees: the pause banner and its "로컬로 계속" button, and the backend cards on the settings page. The helper itself is not started (see tools/e2e_subscription.py for that).
Run:  python tools/e2e_subscription_ui.py   (exit code 0 = every check passed)
"""
import json
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_extension import EXT_ID  # noqa: E402

checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))


def backend_row(id_, state, paused=None):
    return {"id": id_, "label": id_, "model": "x", "state": state, "detail": "", "version": "2.1.285",
            "checked_at": 0,
            "paused": paused, "caps": {"per_5h": 20, "per_7d": 150},
            "usage": {"calls_today": 2, "calls_5h": 2, "calls_7d": 5}, "calibration": None,
            "sandbox_verified": None if id_ == "claude" else False}


def main():
    out = ROOT / "bench_out" / "subscription-ui" / str(time.time_ns())
    out.mkdir(parents=True)
    ext = out / "extension"
    shutil.copytree(ROOT / "extension", ext)
    errors = []
    with sync_playwright() as p:
        exe = None
        if not Path(p.chromium.executable_path).exists():
            # Same fallback as e2e_convenience.py: use the full Chromium already in the Playwright cache.
            candidates = sorted(Path(p.chromium.executable_path).parents[2].glob("chromium-*/chrome-win64/chrome.exe"))
            exe = str(candidates[-1]) if candidates else None
        ctx = p.chromium.launch_persistent_context(
            str(out / "profile"), headless=True, executable_path=exe,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
        sw.evaluate(f"""async () => {{
          globalThis.calls = [];
          const ready = {{ ok: true, version: '0.5.0', ocr_ready: true, ollama: false, models: [
            {{ name: 'claude-haiku-5-5', installed: false, kind: 'subscription' }},
            {{ name: 'qwen3.5:9b', installed: false, kind: 'local' }}] }};
          handlers.setup = async () => ({{ helper: ready, native: true, permission: true }});
          handlers.backends = async () => ({{ backends: globalThis.backendRows }});
          handlers.resumeBackend = async (msg) => {{ calls.push(['resume', msg]); return {{ ok: true }}; }};
          globalThis.backendRows = [
            {{ id: 'claude', label: 'claude', model: 'claude-haiku-5-5', state: 'ready', detail: '구독: pro', version: '2.1.285',
               checked_at: 0,
               paused: {{ until: 0, reason: '구독 사용량 한도에 도달했어요', retry_in_s: 900 }},
               caps: {{ per_5h: 20, per_7d: 150 }}, usage: {{ calls_today: 3, calls_5h: 3, calls_7d: 9 }},
               subscription_usage: {{state: 'ready', checked_at: 1792152000, stale: false, windows: [
                 {{id: 'five_hour', remaining_percent: 89, resets_at: 1792155600}},
                 {{id: 'seven_day', remaining_percent: 8, resets_at: 1792242000}}]}},
               sandbox_verified: null }}];
          await chrome.storage.local.set({{ model: 'claude-haiku-5-5' }});
        }}""")

        popup = ctx.new_page()
        popup.on("pageerror", lambda e: errors.append(str(e)))
        popup.goto(f"chrome-extension://{EXT_ID}/popup.html?tabId=1&url=https%3A%2F%2Fexample.test%2Fmanga")
        popup.wait_for_selector("#subCard:not([hidden])")
        popup.wait_for_function("() => document.querySelector('#subUsage').textContent.includes('89%')")
        usage_text = popup.inner_text("#subUsage")
        check("popup shows actual remaining 5-hour and weekly percentages", "5시간 남음 89%" in usage_text and "7일 남음 8%" in usage_text, usage_text)
        check("popup no longer shows the local call ledger", "오늘" not in usage_text and "회" not in usage_text, usage_text)
        check("the popup has no consent step", popup.locator("#consentBox").count() == 0)
        popup.wait_for_selector("#pausedBox:not([hidden])")
        check("the pause banner shows the reason and the wait", "약 15분" in popup.inner_text("#pausedText"),
              popup.inner_text("#pausedText"))
        popup.click("#pausedLocal")
        popup.wait_for_function("() => document.querySelector('[data-field=\"model\"] [aria-checked=\"true\"]')?.dataset.value === 'qwen3.5:9b'")
        stored = sw.evaluate("async () => (await chrome.storage.local.get('model')).model")
        check("'로컬로 계속' switches the saved model to the local one", stored == "qwen3.5:9b", str(stored))

        options = ctx.new_page()
        options.on("pageerror", lambda e: errors.append(str(e)))
        options.goto(f"chrome-extension://{EXT_ID}/options.html")
        options.wait_for_selector("#models .backend-card")
        cards = options.locator("#models .backend-card")
        check("settings shows the Claude card", cards.count() == 1, str(cards.count()))
        check("the Claude card has no consent button", "사용에 동의" not in cards.nth(0).inner_text())
        check("settings shows remaining account usage", "5시간 남음 89%" in cards.nth(0).inner_text() and "7일 남음 8%" in cards.nth(0).inner_text())
        check("call cap and manual calibration inputs are removed", cards.nth(0).locator('input').count() == 0 and "한도 저장" not in cards.nth(0).inner_text())
        sw.evaluate("() => { backendRows[0].subscription_usage = {state: 'unavailable', windows: []}; }")
        options.wait_for_function("() => document.querySelector('.subscription-usage').textContent.includes('확인 불가')")
        check("unavailable usage does not imply 100 percent remaining", "100%" not in options.inner_text('.subscription-usage'))
        sw.evaluate("() => { backendRows[0].subscription_usage = {state: 'ready', stale: false, windows: [{id:'five_hour', remaining_percent:0, resets_at:null}, {id:'seven_day',remaining_percent:8,resets_at:null}]}; }")
        options.wait_for_function("() => document.querySelector('.subscription-usage').textContent.includes('5시간 남음 0%')")
        check("settings refreshes usage and preserves zero", "5시간 남음 0%" in options.inner_text('.subscription-usage'))
        options.screenshot(path=str(out / "options.png"), full_page=True)
        popup.screenshot(path=str(out / "popup.png"), full_page=True)
        check("the setup step counts the ready Claude backend",
              "ok" in options.get_attribute("#step-models", "class"))
        check("the review option is a select with a safe default", options.locator("#reviewMode").input_value() == "")
        check("no page errors", not errors, "; ".join(errors))
        ctx.close()

    passed = sum(1 for _, ok in checks if ok)
    print(f"\nSubscription UI E2E: {passed}/{len(checks)} checks passed")
    if passed != len(checks):
        print(json.dumps(checks, ensure_ascii=False))
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
