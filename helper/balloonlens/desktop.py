"""Windows selected-area translator: python -m balloonlens.desktop.

Tk owns all windows; background workers only talk to the helper and post events. The watcher waits for a stable
screen, cancels superseded work and draws results only for the region/frame that requested them.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk

from PIL import ImageChops, ImageGrab

from . import config
from .backends.subscription_usage import ClaudeUsage
from .backends.base import clean_env
from .desktop_client import DesktopClient, HelperError, TranslationCancelled
from .desktop_view import RegionSelector, TranslationOverlay
from .desktop_windows import Hotkeys, WindowsDesktop

SOURCES = {"일본어": "ja", "영어": "en"}
TARGETS = {"한국어": "ko", "영어": "en", "일본어": "ja"}
MODELS = {"Claude 구독": config.CLAUDE_MODEL, "로컬 Qwen3.5": "qwen3.5:9b"}
DIRECTIONS = {"만화 ←": "rtl", "가로 →": "ltr", "웹툰 ↓": "ttb"}
SETTINGS_PATH = config.DATA_DIR / "desktop.json"
LOGIN_WAIT_S = 300.0  # how long the app keeps asking whether the Claude login window has finished
LOGIN_POLL_S = 3.0


def _changed(current, previous):
    if previous is None or current.size != previous.size:
        return True
    histogram = ImageChops.difference(current, previous).histogram()
    return sum(histogram[20:]) > max(4, current.width * current.height * 0.003)


def _time_text(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%m/%d %H:%M")


def usage_text(usage):
    windows = usage.get("windows") or []
    if not windows:
        return "Claude 남은 사용량 확인 중…" if usage.get("state") == "loading" else "Claude 남은 사용량 확인 불가"
    parts = []
    for key, label in (("five_hour", "5시간"), ("seven_day", "7일")):
        window = next((w for w in windows if w["id"] == key), {})
        value = window.get("remaining_percent")
        remaining = f"{value:g}%" if isinstance(value, (int, float)) else "확인 불가"
        reset = f" · {_time_text(window['resets_at'])} 초기화" if window.get("resets_at") else ""
        parts.append(f"{label} 남음 {remaining}{reset}")
    if usage.get("stale"):
        parts.append("마지막 확인값")
    return "\n".join(parts)


class DesktopApp:
    def __init__(self, root, native):
        self.root, self.native = root, native
        self.client = DesktopClient()
        self.account_usage = ClaudeUsage(config.CLAUDE_CONFIG_DIR)
        self.events = queue.Queue()
        self.region = None
        self.active = self.busy = self.peeking = self.closed = False
        self.selector = None
        self.generation = 0
        self.cancel = threading.Event()
        self.login_cancel = threading.Event()
        self.last_probe = self.sent_probe = None
        self.active_probe = None
        self.stable_since = 0
        self.capture_pending = self.manual_pending = False
        self._capture_restore = []
        self._build()
        self.overlay = TranslationOverlay(root, native)
        self.control_excluded = native.exclude_capture(root)
        self._load()
        self.hotkeys = Hotkeys(self.events)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self._events)
        root.after(500, self._watch)
        root.after(200, self._usage)

    def _build(self):
        self.root.title("말풍선 렌즈 · 화면 범위 번역")
        self.root.geometry("610x600")
        self.root.minsize(540, 520)
        ttk.Style().configure("TLabel", font=("맑은 고딕", 10))
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="화면 범위 번역", font=("맑은 고딕", 18, "bold")).pack(anchor="w")
        ttk.Label(outer, text="어떤 브라우저·이미지 뷰어에서도 선택한 범위만 번역해요.").pack(anchor="w", pady=(5, 14))
        self.region_text = tk.StringVar(value="아직 범위를 선택하지 않았어요.")
        ttk.Label(outer, textvariable=self.region_text).pack(anchor="w")
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=10)
        ttk.Button(buttons, text="범위 선택  Ctrl+Alt+R", command=self.select_region).pack(side="left")
        self.toggle_button = ttk.Button(buttons, text="자동 번역 시작", command=self.toggle)
        self.toggle_button.pack(side="left", padx=6)
        ttk.Button(buttons, text="이번 화면 번역", command=self.translate_now).pack(side="left")
        row = ttk.Frame(outer)
        row.pack(fill="x", pady=(0, 12))
        ttk.Button(row, text="원문 / 번역  Ctrl+Alt+O", command=self.peek).pack(side="left")
        ttk.Label(row, text="Ctrl+Alt+T 번역 · Ctrl+Alt+S 시작/중지").pack(side="left", padx=10)
        fields = ttk.Frame(outer)
        fields.pack(fill="x")
        self.src = tk.StringVar(value="일본어")
        self.tgt = tk.StringVar(value="한국어")
        self.model = tk.StringVar(value="Claude 구독")
        self.reading = tk.StringVar(value="만화 ←")
        for index, (label, values, variable) in enumerate((
                ("원문", SOURCES, self.src), ("번역", TARGETS, self.tgt),
                ("방식", MODELS, self.model), ("읽는 방향", DIRECTIONS, self.reading))):
            ttk.Label(fields, text=label).grid(row=0, column=index, sticky="w", padx=(0, 8))
            box = ttk.Combobox(fields, textvariable=variable, values=list(values), state="readonly", width=13)
            box.grid(row=1, column=index, sticky="ew", padx=(0, 8), pady=(4, 0))
            box.bind("<<ComboboxSelected>>", self._settings_changed)
            fields.columnconfigure(index, weight=1)
        self.status = tk.StringVar(value="범위를 드래그하면 자동 번역을 시작해요.")
        ttk.Label(outer, textvariable=self.status, wraplength=560, foreground="#0f766e").pack(anchor="w", pady=(14, 6))
        self.usage = tk.StringVar(value="Claude 남은 사용량 확인 중…")
        ttk.Label(outer, textvariable=self.usage, foreground="#616978", wraplength=560).pack(anchor="w", pady=(0, 12))
        ttk.Button(outer, text="Claude 구독 로그인", command=self.login).pack(anchor="w", pady=(0, 10))
        ttk.Label(outer, text="대사 목록 · 작은 말풍선에서 잘린 번역도 여기서 읽을 수 있어요.").pack(anchor="w")
        self.transcript = scrolledtext.ScrolledText(outer, height=10, font=("맑은 고딕", 11), wrap="word", state="disabled")
        self.transcript.pack(fill="both", expand=True, pady=(6, 0))

    def _load(self):
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                return
        except (OSError, ValueError):
            return
        for key, variable, choices in (("src", self.src, SOURCES), ("tgt", self.tgt, TARGETS),
                                       ("model", self.model, MODELS), ("reading", self.reading, DIRECTIONS)):
            if saved.get(key) in choices:
                variable.set(saved[key])
        region = saved.get("region")
        if isinstance(region, list) and len(region) == 4 and all(type(n) is int for n in region):
            left, top, right, bottom = self.native.bounds()
            x1, y1, x2, y2 = region
            if left <= x1 < x2 <= right and top <= y1 < y2 <= bottom and x2 - x1 >= 32 and y2 - y1 >= 32:
                self.region = tuple(region)
                self._region_label()
                self.status.set("마지막 범위를 불러왔어요. 자동 번역 시작을 누르면 이어서 번역해요.")

    def _save(self):
        data = {"src": self.src.get(), "tgt": self.tgt.get(), "model": self.model.get(),
                "reading": self.reading.get(), "region": self.region}
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = SETTINGS_PATH.with_name("desktop." + str(os.getpid()) + ".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            temporary.replace(SETTINGS_PATH)
        except OSError:
            self.status.set("범위·언어 설정을 저장하지 못했어요. 현재 실행에서는 사용할 수 있어요.")

    def _region_label(self):
        x1, y1, x2, y2 = self.region
        self.region_text.set(f"선택 범위: {x2 - x1} × {y2 - y1}px · 위치 {x1}, {y1}")

    def _reset(self):
        self.generation += 1
        self.cancel.set()
        self.last_probe = self.sent_probe = self.active_probe = None
        self.stable_since = 0
        self.overlay.clear()

    def _settings_changed(self, _event=None):
        self._reset()
        self._save()

    def select_region(self):
        if self.closed or self.selector is not None:
            return
        self._reset()
        self.root.withdraw()
        self.root.after(120, self._open_selector)

    def _open_selector(self):
        if not self.closed:
            self.selector = RegionSelector(self.root, self.native, self._selected)

    def _selected(self, region):
        self.selector = None
        if self.closed:
            return
        self.root.deiconify()
        if region is not None:
            self.region = region
            self._region_label()
            self._save()
            self.start()
        elif self.active:
            self.status.set("범위 선택을 취소했어요. 이전 범위에서 계속 번역해요.")

    def start(self):
        if not self.region:
            return self.select_region()
        self._reset()
        self.active = True
        self.peeking = False
        self.toggle_button.configure(text="자동 번역 중지")
        self.status.set("선택 영역을 보고 있어요. 화면이 바뀌고 잠시 멈추면 번역해요.")

    def stop(self):
        self.active = False
        self.manual_pending = False
        self._reset()
        self.toggle_button.configure(text="자동 번역 시작")
        self.status.set("중지했어요. 선택 영역은 그대로 유지해요.")

    def toggle(self):
        self.stop() if self.active else self.start()

    def peek(self):
        self.peeking = not self.peeking
        self.overlay.hide() if self.peeking else self.overlay.show()

    def _restore_capture_windows(self):
        for window in self._capture_restore:
            if window.winfo_exists():
                window.deiconify()
        self._capture_restore = []

    def _capture(self, callback):
        if self.capture_pending or not self.region or self.selector is not None or self.closed:
            return
        self.capture_pending = True
        region, generation = self.region, self.generation
        if not self.overlay.capture_excluded and self.overlay.window.state() != "withdrawn":
            self.overlay.hide()
            self._capture_restore.append(self.overlay.window)
        if not self.control_excluded and self.root.state() == "normal":
            x, y, w, h = self.root.winfo_rootx(), self.root.winfo_rooty(), self.root.winfo_width(), self.root.winfo_height()
            if x < region[2] and x + w > region[0] and y < region[3] and y + h > region[1]:
                self.root.withdraw()
                self._capture_restore.append(self.root)

        def capture():
            try:
                if self.closed or self.selector is not None or generation != self.generation:
                    return
                image = ImageGrab.grab(bbox=region, all_screens=True, include_layered_windows=True).convert("RGB")
            except OSError:
                self.stop()
                self.status.set("선택 영역을 캡처하지 못했어요. 화면 범위를 다시 선택해 주세요.")
                return
            finally:
                self.capture_pending = False
                if not self.closed:
                    self._restore_capture_windows()
            callback(image)

        self.root.after(100 if self._capture_restore else 0, capture)

    def _watch(self):
        if self.closed:
            return
        self.root.after(500, self._watch)
        if self.active and self.region and self.selector is None:
            self._capture(self._observe)

    def _observe(self, image):
        if not self.active:
            return
        probe = image.convert("L")
        probe.thumbnail((320, 320))
        now = time.monotonic()
        if _changed(probe, self.last_probe):
            self.last_probe = probe
            self.sent_probe = None
            self.stable_since = now
            self.overlay.clear()
            if self.busy and _changed(probe, self.active_probe):
                self.cancel.set()
                self.generation += 1
            return
        if now - self.stable_since >= 0.7 and not self.busy and _changed(probe, self.sent_probe):
            self._translate(image, probe)

    def translate_now(self):
        if not self.region:
            return self.select_region()
        if self.busy:
            self.cancel.set()
            self.generation += 1
            self.manual_pending = True
            return

        def run(image):
            probe = image.convert("L")
            probe.thumbnail((320, 320))
            self._translate(image, probe)

        self._capture(run)

    def _translate(self, image, probe):
        if self.busy or self.closed or self.selector is not None:
            return
        self.busy = True
        self.last_probe = probe
        self.stable_since = time.monotonic()
        self.sent_probe = self.active_probe = probe
        self.overlay.clear()
        self.cancel = threading.Event()
        cancel, generation = self.cancel, self.generation
        options = {"src": SOURCES[self.src.get()], "tgt": TARGETS[self.tgt.get()],
                   "model": MODELS[self.model.get()], "reading": DIRECTIONS[self.reading.get()]}
        self.status.set("번역 준비 중…")

        def work():
            try:
                result = self.client.translate(image, options, cancel,
                    lambda state: self.events.put(("progress", (generation, state))))
                self.events.put(("result", (generation, result)))
            except TranslationCancelled:
                self.events.put(("cancelled", generation))
            except HelperError as exc:
                self.events.put(("error", (generation, str(exc))))
            except Exception as exc:
                self.events.put(("error", (generation, f"번역을 진행하지 못했어요 ({type(exc).__name__}).")))
            finally:
                self.events.put(("idle", generation))

        # Let an in-flight request cancel its helper job before the desktop process exits.
        threading.Thread(target=work, name="desktop-translation", daemon=False).start()

    def _result(self, snapshot):
        self.overlay.draw(self.region, snapshot)
        if not self.peeking:
            self.overlay.show()
        shown = [s for s in snapshot.get("segments", []) if s.get("tgt")]
        lines = []
        for segment in snapshot.get("segments", []):
            lines.extend([segment.get("src", ""), segment.get("tgt") or "(번역 없음)", ""])
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.insert("1.0", "\n".join(lines))
        self.transcript.configure(state="disabled")
        self.status.set(f"{'일부 번역' if snapshot.get('status') == 'partial' else '번역 완료'} · {len(shown)}개 대사"
                        if shown else "선택 영역에 읽을 수 있는 대사가 없어요.")

    def _events(self):
        if self.closed:
            return
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "hotkey":
                    {"select": self.select_region, "translate": self.translate_now,
                     "peek": self.peek, "toggle": self.toggle}[data]()
                elif kind == "hotkey_warning":
                    self.status.set(data)
                elif kind == "login":
                    self.status.set(data)
                elif kind == "progress" and data[0] == self.generation:
                    self.status.set({"queued": "번역 순서를 기다리고 있어요…", "ocr": "선택 영역의 글자를 읽고 있어요…",
                                     "translate": "대사를 번역하고 있어요…"}.get(data[1], "번역 마무리 중…"))
                elif kind == "result" and data[0] == self.generation:
                    self._result(data[1])
                elif kind == "error" and data[0] == self.generation:
                    self.stop()
                    self.status.set(data[1])
                elif kind == "idle":
                    self.busy = False
                    self.active_probe = None
                    if self.manual_pending:
                        self.manual_pending = False
                        self.root.after_idle(self.translate_now)
        except queue.Empty:
            pass
        self.root.after(100, self._events)

    def _usage(self):
        if self.closed:
            return
        self.usage.set(usage_text(self.account_usage.snapshot()))
        self.root.after(5000, self._usage)

    def login(self):
        try:
            config.CLAUDE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            config.CLAUDE_SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
            env = clean_env(os.environ, {"CLAUDE_CONFIG_DIR": str(config.CLAUDE_CONFIG_DIR)})
            subprocess.Popen([*config.claude_command(), "auth", "login"], cwd=str(config.CLAUDE_SANDBOX_DIR),
                             env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        except OSError:
            self.status.set("Claude 로그인 창을 켜지 못했어요. Claude Code가 설치돼 있는지 확인해 주세요 (https://claude.com/claude-code).")
            return
        self.status.set("열린 Claude 로그인 창에서 로그인해 주세요. 끝나면 여기에 알려 드릴게요.")
        threading.Thread(target=self._wait_for_login, name="desktop-login-check", daemon=True).start()

    def _wait_for_login(self):
        """Asks the helper for a fresh Claude status until it is ready. Runs in the background; it only posts an event."""
        deadline = time.monotonic() + LOGIN_WAIT_S
        while time.monotonic() < deadline and not self.closed:
            state = None
            try:
                self.client.ensure_running(self.login_cancel)
                state = self.client.request("POST", "/v1/backends/claude/check", timeout=30)["status"]["state"]
            except (HelperError, TranslationCancelled, OSError, ValueError, KeyError):
                pass
            if state == "ready":
                self.events.put(("login", "Claude 로그인을 확인했어요. 이제 자동 번역을 시작할 수 있어요."))
                return
            time.sleep(LOGIN_POLL_S)
        if not self.closed:
            self.events.put(("login", "Claude 로그인이 아직 확인되지 않았어요. 로그인 창에서 로그인을 끝냈는지 확인해 주세요."))

    def close(self):
        self.closed = True
        self.cancel.set()
        self.login_cancel.set()
        self._save()
        self.hotkeys.close()
        self.overlay.close()
        self.root.destroy()


def main():
    if sys.platform != "win32":
        raise SystemExit("화면 범위 번역 프로그램은 Windows에서 실행해 주세요.")
    native = WindowsDesktop()
    root = tk.Tk()
    try:
        DesktopApp(root, native)
    except Exception as exc:
        messagebox.showerror("말풍선 렌즈", f"프로그램을 시작하지 못했어요 ({type(exc).__name__}).\nSetup.cmd로 번역 환경을 준비해 주세요.", parent=root)
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
