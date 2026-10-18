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
from tkinter import messagebox

from PIL import ImageChops, ImageGrab

from . import config
from .backends.subscription_usage import ClaudeUsage
from .backends.base import clean_env
from .backends.prompts import valid_target
from .desktop_client import DesktopClient, HelperError, TranslationCancelled
from .desktop_controls import SOURCES, TARGETS, MODELS, DIRECTIONS, REVIEWS
from .desktop_view import RegionSelector, TranslationOverlay
from .desktop_windows import Hotkeys, WindowsDesktop
from .ui_locale import UITranslator, language_key

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


def usage_text(usage, translate=str):
    windows = usage.get("windows") or []
    if not windows:
        return translate("Claude 남은 사용량 확인 중…" if usage.get("state") == "loading" else "Claude 남은 사용량 확인 불가")
    parts = []
    for key, label in (("five_hour", "5시간"), ("seven_day", "7일")):
        window = next((w for w in windows if w["id"] == key), {})
        value = window.get("remaining_percent")
        remaining = f"{value:g}%" if isinstance(value, (int, float)) else "확인 불가"
        reset = " · " + translate(f"초기화 {_time_text(window['resets_at'])}") if window.get("resets_at") else ""
        parts.append(translate(f"{label} 남음 {remaining}") + reset)
    if usage.get("stale"):
        parts.append(translate("마지막 확인값"))
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
        self._bar_position = None
        self._drag_origin = None
        self.paused = False
        self._start_after_selection = False
        self._last_snapshot = None
        self.ui = UITranslator()
        self._build()
        self.overlay = TranslationOverlay(root, native)
        self.control_excluded = native.exclude_capture(root)
        self.capture_windows[0] = (root, self.control_excluded)
        self._load()
        self._apply_ui()
        self._sync_bar()
        self._translation_options = self._options()
        self.hotkeys = Hotkeys(self.events)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self._events)
        root.after(500, self._watch)
        root.after(200, self._usage)

    def _build(self):
        from .desktop_controls import build
        build(self)

    def _position_bar(self):
        self.root.update_idletasks()
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        left, top, right, bottom = self.native.bounds()
        x, y = self._bar_position or (self.native.user32.GetSystemMetrics(0) - width - 24,
                                     self.native.user32.GetSystemMetrics(1) - height - 72)
        x, y = max(left, min(right - width, x)), max(top, min(bottom - height, y))
        self.native.place(self.root, (x, y, x + width, y + height))

    def _drag_start(self, event):
        self._drag_origin = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_move(self, event):
        if self._drag_origin is not None:
            self._bar_position = (event.x_root - self._drag_origin[0], event.y_root - self._drag_origin[1])
            self._position_bar()

    def _place_popup(self, window):
        window.update_idletasks()
        left, top, right, bottom = self.native.bounds()
        if window is self.settings_window:
            self.settings_canvas.configure(width=self.settings_frame.winfo_reqwidth(),
                                           height=min(bottom - top - 100, self.settings_frame.winfo_reqheight()))
            window.update_idletasks()
        width = max(450, window.winfo_reqwidth())
        height = min(bottom - top - 24, window.winfo_reqheight())
        x = max(left, min(right - width, self.root.winfo_x() + self.root.winfo_width() - width))
        y = self.root.winfo_y() - height - 10
        if y < top:
            y = min(bottom - height, self.root.winfo_y() + self.root.winfo_height() + 10)
        self.native.place(window, (x, max(top, y), x + width, max(top, y) + height))

    def show_settings(self):
        if self.selector is None:
            self.settings_window.deiconify()
            self._place_popup(self.settings_window)

    def show_transcript(self):
        self.transcript_window.deiconify()
        self._place_popup(self.transcript_window)

    def _sync_bar(self):
        if self.active:
            self.bar_status.set("자동 번역 중" if self.automatic.get() else "수동 번역")
            self.toggle_button.configure(text="Ⅱ" if self.automatic.get() else "이번 화면 번역")
            self.region_button.pack_forget()
            self.stop_button.configure(state="normal")
        else:
            self.bar_status.set("일시정지" if self.paused else "준비됨")
            self.toggle_button.configure(text="계속" if self.paused else "번역 시작")
            self.region_button.pack(side="left", before=self.toggle_button, padx=2)
            self.stop_button.configure(state="normal" if self.paused else "disabled")
        self.peek_button.configure(text="번역 다시 보이기" if self.peeking else "원문 보기")
        self.ui.apply_widgets(self.root)
        self._position_bar()

    @staticmethod
    def _number(variable, default, lower, upper):
        try:
            value = max(lower, min(upper, float(variable.get())))
        except (tk.TclError, ValueError):
            value = default
        variable.set(value)
        return value

    def _options(self):
        result = {}
        for key, (box, variable, choices) in zip(("src", "tgt", "model", "reading", "review"), self.ui_choices):
            original = box._ui_values.get(variable.get(), variable.get()).strip()
            result[key] = choices.get(original, original)
        target = result["tgt"]
        from .ui_locale import PACKS
        result["tgt"] = PACKS["aliases"].get(target.lower(), target)
        if result["src"] == result["tgt"]:
            result["src"] = "auto"
        return result

    def _load(self):
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                return
        except (OSError, ValueError):
            return
        try:
            locale = language_key(saved.get("uiLanguage", "ko"))
            self.ui.set(locale)
            self.ui_language.set(locale)
        except (ValueError, TypeError):
            pass
        for key, variable, choices in (("src", self.src, SOURCES), ("tgt", self.tgt, TARGETS),
                                       ("model", self.model, MODELS), ("reading", self.reading, DIRECTIONS),
                                       ("review", self.review, REVIEWS)):
            if key == "src" and saved.get("settingsVersion", 1) < 2:
                continue  # automatic recognition replaces the previous manually selected default
            if saved.get(key) in choices:
                variable.set(saved[key])
            elif key == "tgt" and valid_target(saved.get(key)):
                variable.set(saved[key])
        for key, variable in (("automatic", self.automatic), ("showCorners", self.show_corners)):
            if type(saved.get(key)) is bool:
                variable.set(saved[key])
        for key, variable in (("fontScale", self.font_scale), ("opacity", self.opacity), ("settleTime", self.settle_time)):
            if type(saved.get(key)) in (int, float):
                variable.set(saved[key])
        position = saved.get("barPosition")
        if isinstance(position, list) and len(position) == 2 and all(type(n) is int for n in position):
            self._bar_position = tuple(position)
        self._display_options()
        region = saved.get("region")
        if isinstance(region, list) and len(region) == 4 and all(type(n) is int for n in region):
            left, top, right, bottom = self.native.bounds()
            x1, y1, x2, y2 = region
            if left <= x1 < x2 <= right and top <= y1 < y2 <= bottom and x2 - x1 >= 32 and y2 - y1 >= 32:
                self.region = tuple(region)
                self._region_label()
                self.status.set("마지막 범위를 불러왔어요. 번역 시작을 누르면 이어서 번역해요.")

    def _save(self):
        data = {key: box._ui_values.get(variable.get(), variable.get())
                for key, (box, variable, _choices) in zip(("src", "tgt", "model", "reading", "review"), self.ui_choices)}
        data.update(region=self.region, uiLanguage=self.ui.language, settingsVersion=2,
                    automatic=self.automatic.get(), showCorners=self.show_corners.get(),
                    fontScale=self._number(self.font_scale, 1.0, 0.7, 1.5),
                    opacity=self._number(self.opacity, 1.0, 0.5, 1.0),
                    settleTime=self._number(self.settle_time, 0.8, 0.4, 2.0),
                    barPosition=[self.root.winfo_x(), self.root.winfo_y()])
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

    def _apply_ui(self):
        self.root.title(self.ui.text("말풍선 렌즈 · 화면 범위 번역"))
        self.usage.set(usage_text(self.account_usage.snapshot(), self.ui.text))
        for box, variable, choices in self.ui_choices:
            original = box._ui_values.get(variable.get(), variable.get())
            box._ui_values = {self.ui.text(label): label for label in choices}
            box.configure(values=list(box._ui_values))
            variable.set(self.ui.text(original))
        self.ui.apply_widgets(self.root)
        self.settings_window.title(self.ui.text("번역 설정"))
        self.transcript_window.title(self.ui.text("대사 목록"))
        self._position_bar()

    def change_ui_language(self):
        try:
            language = language_key(self.ui_language.get())
            self.ui.set(language)
        except ValueError:
            self.language_status.set("이 언어의 화면 문구 파일이 없어요. 목록에서 언어를 선택해 주세요.")
            return
        self._apply_ui()
        if self.settings_window.state() == "normal":
            self._place_popup(self.settings_window)
        self._save()
        self.language_status.set("화면 언어를 바꿨어요.")

    def _reset(self):
        self.generation += 1
        self.cancel.set()
        self.last_probe = self.sent_probe = self.active_probe = None
        self.stable_since = 0
        self.overlay.clear()
        self._last_snapshot = None

    def _settings_changed(self, _event=None):
        options = self._options()
        if not valid_target(options["tgt"]):
            self.status.set("목표 언어 이름이나 언어 코드를 입력해 주세요.")
            return
        if options != getattr(self, "_translation_options", options):
            self._reset()
        self._translation_options = options
        self._display_options()
        if self._last_snapshot is not None:
            self.overlay.draw(self.region, self._last_snapshot)
            if not self.peeking:
                self.overlay.show()
        elif self.active:
            self._show_region()
        self._sync_bar()
        self._save()

    def _display_options(self):
        self.overlay.configure_display(self._number(self.font_scale, 1.0, 0.7, 1.5),
                                       self._number(self.opacity, 1.0, 0.5, 1.0), self.show_corners.get())
        self._number(self.settle_time, 0.8, 0.4, 2.0)

    def _show_region(self):
        self.overlay.draw(self.region, {})
        if not self.peeking:
            self.overlay.show()

    def select_region(self):
        if self.closed or self.selector is not None:
            return
        self._reset()
        self.settings_window.withdraw()
        self.transcript_window.withdraw()
        self.root.withdraw()
        self.root.after(120, self._open_selector)

    def _open_selector(self):
        if not self.closed:
            self.selector = RegionSelector(self.root, self.native, self._selected, ui=self.ui)

    def _selected(self, region):
        self.selector = None
        resume = self._start_after_selection
        self._start_after_selection = False
        if self.closed:
            return
        self.root.deiconify()
        if region is not None:
            self.region = region
            self._region_label()
            self._save()
            if self.active or resume:
                self.start()
            else:
                self.status.set("범위를 선택했어요. 번역 시작을 누르세요.")
                self._sync_bar()
        elif self.active:
            self.status.set("범위 선택을 취소했어요. 이전 범위에서 계속 번역해요.")

    def start(self):
        if not self.region:
            self._start_after_selection = True
            return self.select_region()
        if not valid_target(self._options()["tgt"]):
            self.status.set("목표 언어 이름이나 언어 코드를 입력해 주세요.")
            return self.show_settings()
        self._reset()
        self.active = True
        self.paused = False
        self.peeking = False
        self.settings_window.withdraw()
        self.transcript_window.withdraw()
        self._sync_bar()
        self._show_region()
        self.status.set("선택 영역을 보고 있어요. 화면이 바뀌고 잠시 멈추면 번역해요.")
        if not self.automatic.get():
            self.root.after_idle(self.translate_now)

    def stop(self):
        self.active = False
        self.paused = False
        self.manual_pending = False
        self._reset()
        self._sync_bar()
        self.status.set("중지했어요. 선택 영역은 그대로 유지해요.")

    def toggle(self):
        if not self.active:
            return self.start()
        if not self.automatic.get():
            return self.translate_now()
        self.active = False
        self.paused = True
        self.manual_pending = False
        self._reset()
        self.status.set("일시정지했어요. 계속을 누르면 번역을 재개해요.")
        self._sync_bar()

    def peek(self):
        self.peeking = not self.peeking
        self.overlay.hide() if self.peeking else self.overlay.show()
        self._sync_bar()

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
        for window, excluded in self.capture_windows:
            if excluded or window.state() != "normal":
                continue
            x, y, w, h = window.winfo_rootx(), window.winfo_rooty(), window.winfo_width(), window.winfo_height()
            if x < region[2] and x + w > region[0] and y < region[3] and y + h > region[1]:
                window.withdraw()
                self._capture_restore.append(window)

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
            self._show_region()
            self._last_snapshot = None
            if self.busy and _changed(probe, self.active_probe):
                self.cancel.set()
                self.generation += 1
            return
        if self.automatic.get() and now - self.stable_since >= self.settle_time.get() and not self.busy and _changed(probe, self.sent_probe):
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
        options = self._options()
        if not valid_target(options["tgt"]):
            self.status.set("목표 언어 이름이나 언어 코드를 입력해 주세요.")
            return self.show_settings()
        self.busy = True
        self.last_probe = probe
        self.stable_since = time.monotonic()
        self.sent_probe = self.active_probe = probe
        self._show_region()
        self.cancel = threading.Event()
        cancel, generation = self.cancel, self.generation
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
        self._last_snapshot = snapshot
        self.overlay.draw(self.region, snapshot)
        if not self.peeking:
            self.overlay.show()
        shown = [s for s in snapshot.get("segments", []) if s.get("tgt")]
        lines = []
        for segment in snapshot.get("segments", []):
            lines.extend([segment.get("src", ""), segment.get("tgt") or self.ui.text("(번역 없음)"), ""])
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
                    self.bar_status.set("글자 인식 중" if data[1] == "ocr" else "번역 중")
                    self.status.set({"queued": "번역 순서를 기다리고 있어요…", "ocr": "선택 영역의 글자를 읽고 있어요…",
                                     "translate": "대사를 번역하고 있어요…"}.get(data[1], "번역 마무리 중…"))
                elif kind == "result" and data[0] == self.generation:
                    self._result(data[1])
                elif kind == "error" and data[0] == self.generation:
                    self.stop()
                    self.status.set(data[1])
                    self.show_settings()
                elif kind == "idle":
                    self.busy = False
                    self.active_probe = None
                    self._sync_bar()
                    if self.manual_pending:
                        self.manual_pending = False
                        self.root.after_idle(self.translate_now)
        except queue.Empty:
            pass
        self.ui.apply_widgets(self.root)
        self.root.after(100, self._events)

    def _usage(self):
        if self.closed:
            return
        self.usage.set(usage_text(self.account_usage.snapshot(), self.ui.text))
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
