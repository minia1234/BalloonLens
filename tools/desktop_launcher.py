"""Small desktop launcher, also packaged as BalloonLens.exe without bundling the OCR models."""
import ctypes
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import scrolledtext, ttk


def environment_ready(root):
    environment = root / "helper" / ".venv"
    try:
        signature = hashlib.sha256((root / "helper" / "requirements.txt").read_bytes() +
                                   (root / "tools" / "setup_windows.ps1").read_bytes()).hexdigest()
        return (all((environment / "Scripts" / name).is_file() for name in ("python.exe", "pythonw.exe"))
                and (environment / ".balloonlens-ready").read_text(encoding="utf-8").strip() == signature)
    except OSError:
        return False


class FirstRun:
    """Only standard-library code: this window also works before Python/OCR is installed."""
    def __init__(self, root):
        self.root_dir = root
        self.data_dir = Path(os.environ.get("BALLOONLENS_DATA") or Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BalloonLens")
        self.log_path = self.data_dir / "setup.log"
        self.events = queue.Queue()
        self.process = None
        self.closed = self.success = False
        self.translations = {}
        try:
            settings = json.loads((self.data_dir / "desktop.json").read_text(encoding="utf-8"))
            language = settings.get("uiLanguage", "ko")
            catalog = json.loads((root / "extension" / "ui_catalog.json").read_text(encoding="utf-8"))
            packs = json.loads((root / "extension" / "ui_languages.json").read_text(encoding="utf-8"))
            if language != "ko":
                self.translations = {key: values[1 if language == "ja" else 0] for key, values in catalog.items()}
                if language in packs["order"]:
                    column = packs["order"].index(language)
                    self.translations.update({key: values[column] for key, values in packs["strings"].items()})
        except (OSError, ValueError):
            pass
        self.window = tk.Tk()
        self.window.title("BalloonLens")
        self.window.geometry("620x380")
        self.window.minsize(480, 280)
        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        self.status = tk.StringVar(value=self.text("첫 실행 환경을 자동으로 준비해요."))
        ttk.Label(frame, textvariable=self.status, wraplength=570, font=("맑은 고딕", 11)).pack(anchor="w")
        ttk.Label(frame, text=self.text("처음에는 수 GB를 다운로드할 수 있어요. 준비가 끝나면 프로그램이 열려요."),
                  wraplength=570).pack(anchor="w", pady=8)
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 10))
        self.log = scrolledtext.ScrolledText(frame, state="disabled", height=10, wrap="word")
        self.log.pack(fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        self.retry = ttk.Button(buttons, text=self.text("다시 시도"), command=self.begin, state="disabled")
        self.retry.pack(side="left")
        ttk.Button(buttons, text=self.text("설치 기록 열기"), command=self.open_log).pack(side="left", padx=6)
        ttk.Button(buttons, text=self.text("닫기"), command=self.close).pack(side="right")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.after(100, self.poll)
        self.window.after(150, self.begin)

    def text(self, source):
        return self.translations.get(source, source)

    def begin(self):
        self.retry.configure(state="disabled")
        self.status.set(self.text("첫 실행 환경을 자동으로 준비해요."))
        self.progress.start(12)
        threading.Thread(target=self.prepare, name="balloonlens-setup", daemon=False).start()

    def prepare(self):
        complete = False
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PIP_NO_INPUT="1")
            with self.log_path.open("a", encoding="utf-8") as record:
                record.write("\n--- BalloonLens setup ---\n")
                self.process = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(self.root_dir / "tools" / "setup_windows.ps1"), "-EnsureReady"],
                    cwd=str(self.root_dir), env=env, creationflags=subprocess.CREATE_NO_WINDOW,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                if self.closed:
                    self.stop_process()
                for raw in self.process.stdout:
                    try:
                        line = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        line = raw.decode("cp949", "replace")
                    record.write(line)
                    record.flush()
                    self.events.put(("log", line))
                code = self.process.wait()
            complete = code == 0 and environment_ready(self.root_dir)
        except OSError as exc:
            self.events.put(("log", type(exc).__name__ + "\n"))
        finally:
            self.process = None
            self.events.put(("done", complete))

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", value)
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "done":
                    self.progress.stop()
                    if value:
                        self.success = True
                        self.window.destroy()
                        return
                    self.status.set(self.text("환경 준비에 실패했어요. 설치 기록을 확인한 뒤 다시 시도하세요."))
                    self.retry.configure(state="normal")
        except queue.Empty:
            pass
        self.window.after(100, self.poll)

    def open_log(self):
        if self.log_path.is_file():
            os.startfile(str(self.log_path))

    def stop_process(self):
        process = self.process
        if process is not None and process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, timeout=15, check=False)

    def close(self):
        self.closed = True
        threading.Thread(target=self.stop_process, daemon=False).start()
        self.window.destroy()


def main():
    start = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    root = next((folder for folder in (start, *start.parents)
                 if (folder / "helper" / "balloonlens" / "desktop.py").is_file()), None)
    if root is None:
        ctypes.windll.user32.MessageBoxW(None, "BalloonLens 폴더 안에서 실행해 주세요.\n실행 파일과 helper 폴더가 함께 있어야 해요.", "말풍선 렌즈", 0x10)
        return
    if getattr(sys, "frozen", False):
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    if not environment_ready(root):
        setup = FirstRun(root)
        setup.window.mainloop()
        if not setup.success:
            return
    python = root / "helper" / ".venv" / "Scripts" / "pythonw.exe"
    try:
        subprocess.Popen([str(python), "-m", "balloonlens.desktop"], cwd=str(root / "helper"),
                         creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        ctypes.windll.user32.MessageBoxW(None, "프로그램을 켜지 못했어요.\nSetup.cmd로 번역 환경을 준비해 주세요.", "말풍선 렌즈", 0x10)


if __name__ == "__main__":
    main()
