"""BalloonLens 설치 도우미 창. exe로 묶어서 더블클릭으로 쓴다 (tools/build_setup_exe.ps1).

설치 로직은 tools/setup_windows.ps1 한 곳에만 있다. 이 창은 그 스크립트를 부르고, 상태를 보여주고,
로그인 창과 확장 폴더 같은 반복 작업을 버튼 하나로 연다. 로그인 정보는 이 프로그램이 다루지 않는다.
"""
import json
import os
import queue
import re
import subprocess
import time
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import chrome_setup

APP = "BalloonLens 설치 도우미"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
COLORS = {"ok": "#1a7f37", "warn": "#b26a00", "off": "#6b7280"}


# ---- pure helpers (tested in tools/test_setup_gui.py) ----

def find_root(start: Path) -> Path | None:
    """The folder that holds tools/setup_windows.ps1 and helper/, searched from start upward."""
    for folder in [start, *start.parents][:4]:
        if (folder / "tools" / "setup_windows.ps1").is_file() and (folder / "helper").is_dir():
            return folder
    return None


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def powershell_args(root: Path, *flags: str) -> list[str]:
    script = root / "tools" / "setup_windows.ps1"
    return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *flags]


def decode_output(raw: bytes) -> str:
    """Windows PowerShell writes the console code page (cp949 on Korean Windows) unless told otherwise."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp949", errors="replace")


def parse_status(text: str) -> dict:
    """The status object is the only JSON in the output; anything printed before it is ignored."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("status output has no JSON object")
    return json.loads(text[start:end + 1])


def app_version(root: Path) -> str:
    try:
        return json.loads((root / "extension" / "manifest.json").read_text(encoding="utf-8"))["version"]
    except (OSError, KeyError, ValueError):
        return "?"


def claude_text(c: dict) -> str:
    if not c.get("installed"):
        return "설치 필요: https://claude.com/claude-code"
    if not c.get("versionOk"):
        return f"버전을 올려 주세요 (지금 {c.get('version') or '알 수 없음'}, 2.1.268 이상)"
    if not c.get("loggedIn"):
        return f"{c.get('version')} · 로그인 필요 (아래 로그인 창 버튼)"
    return f"{c.get('version')} · 로그인 됨"


def status_rows(status: dict) -> list[tuple[str, str, str]]:
    """(name, text, state) for each line of the window. state is ok, warn or off."""
    claude = status.get("claude") or {}

    def pick(good: bool, good_text: str, bad_text: str, bad_state: str = "warn") -> tuple[str, str]:
        return (good_text, "ok") if good else (bad_text, bad_state)

    deps = pick(status.get("deps"), "준비됨", "설치 필요 (자동 준비가 설치해요)")
    host = pick(status.get("nativeHost"), "등록됨", "등록 필요 (자동 준비가 등록해요)")
    helper = pick(status.get("helper"), "실행 중", "꺼짐 (크롬이 확장을 열 때 자동으로 켜져요)", "off")
    owner = status.get("portOwner") or "free"
    if owner.startswith("other:"):
        helper = (f"포트 8787을 다른 프로그램({owner[6:]})이 쓰고 있어요. 그 프로그램을 끄고 '상태 다시 확인'을 누르세요.", "warn")
    ollama = pick(status.get("ollama"), "설치됨", "없음 (구독 번역에는 필요 없어요)", "off")
    claude_ok = bool(claude.get("installed") and claude.get("versionOk") and claude.get("loggedIn"))
    return [
        ("번역 환경 (Python 패키지)", *deps),
        ("도우미 자동 실행 등록", *host),
        ("도우미", *helper),
        ("Claude Code", claude_text(claude), "ok" if claude_ok else "warn"),
        ("Ollama (선택 사항)", *ollama),
        ("크롬 확장", *extension_row(status.get("chromeExtension"))),
    ]


def extension_row(found: dict | None) -> tuple[str, str]:
    """(text, state) for the Chrome extension line, from what chrome_setup found in Chrome's profiles."""
    if found and found.get("profile"):
        if found.get("enabled"):
            return (f"설치됨 · 프로필 {found.get('name')} (켜짐)", "ok")
        return (f"설치됨 · 프로필 {found.get('name')} · 꺼져 있어요 (크롬에서 켜세요)", "warn")
    if found and found.get("elsewhere"):
        return ("다른 폴더의 BalloonLens가 로드돼 있어요. '크롬 확장 설치 안내'를 보세요", "warn")
    return ("아직 설치되지 않았어요 ('크롬 확장 설치 안내' 버튼, 약 1분)", "warn")


STEP_RE = re.compile(r"\[step (\d+)/(\d+)\] (.*)")


def parse_step(line: str) -> tuple[int, int, str] | None:
    """'[step 2/6] text' from the setup script -> (2, 6, 'text'); anything else -> None."""
    match = STEP_RE.search(line)
    return (int(match.group(1)), int(match.group(2)), match.group(3)) if match else None


def ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def claude_login_args(claude_path: str, config_dir: Path) -> list[str]:
    """A new PowerShell window with the isolated config folder; the user signs in there."""
    script = f"$env:CLAUDE_CONFIG_DIR = {ps_quote(str(config_dir))}; & {ps_quote(claude_path)} auth login"
    return ["powershell.exe", "-NoExit", "-NoProfile", "-Command", script]


def diagnostics_text(status: dict, version: str) -> str:
    """For a bug report. Paths and states only: no dialogue text, no tokens, no file contents."""
    claude = status.get("claude") or {}
    return "\n".join([
        "BalloonLens 설치 도우미 진단",
        f"- 설치 도구 버전: {version}",
        f"- 설치 폴더: {status.get('root', '-')}",
        f"- 데이터 폴더: {status.get('dataDir', '-')}",
        f"- 번역 환경: {'준비됨' if status.get('deps') else '필요'}",
        f"- 자동 실행 등록: {'됨' if status.get('nativeHost') else '안 됨'}",
        f"- 도우미: {'실행 중' if status.get('helper') else '꺼짐'}",
        f"- 포트 상태: {status.get('portOwner') or '알 수 없음'}",
        f"- Claude Code: {claude.get('version') or '없음'} · 로그인 {'됨' if claude.get('loggedIn') else '안 됨'}",
       f"- Ollama(선택): {'있음' if status.get('ollama') else '없음'}",
    ])


def find_chrome() -> str | None:
    candidates = [os.path.join(os.environ.get(key, ""), "Google", "Chrome", "Application", "chrome.exe")
                  for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    return next((path for path in candidates if os.path.isfile(path)), None)


def read_status(root: Path) -> dict:
    proc = subprocess.run(powershell_args(root, "-StatusJson"), capture_output=True,
                          creationflags=CREATE_NO_WINDOW, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(decode_output(proc.stderr).strip() or f"exit {proc.returncode}")
    status = parse_status(decode_output(proc.stdout))
    status["chromeExtension"] = chrome_setup.find_extension(chrome_setup.chrome_user_data(), root / "extension")
    return status


# ---- window ----

class SetupWindow:
    def __init__(self, root_dir: Path, hold_seconds: float | None = None):
        self.root_dir = root_dir
        self.status: dict | None = None
        self.events: queue.Queue = queue.Queue()
        self.win = tk.Tk()
        self.win.title(APP)
        self.win.geometry("640x700")
        self.win.minsize(580, 600)
        self.buttons: list[tk.Button] = []
        self.offer_wizard = False
        self.started = None
        self.step_text = ""
        self.rows: dict[str, tuple[tk.Label, tk.Label]] = {}
        self._build()
        self.win.after(100, self._poll)
        self._log("처음이면 '전부 자동으로 준비'를 누르세요. 그다음 Claude 로그인 창에서 로그인하면 끝이에요.")
        self.refresh()
        if hold_seconds is not None:  # --hold is for screenshots: keep the window on top, close it on time
            self.win.attributes("-topmost", True)
            self.win.after(int(hold_seconds * 1000), self.win.destroy)

    def _build(self):
        frame = tk.Frame(self.win, padx=18, pady=14)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=APP, font=("Malgun Gothic", 15, "bold")).pack(anchor="w")
        tk.Label(frame, text="버튼으로 준비하고, 로그인만 직접 하세요. 로그인 정보는 이 창이 다루지 않아요.",
                 fg="#4b5563", wraplength=600, justify="left").pack(anchor="w", pady=(2, 10))

        status_box = tk.Frame(frame, bd=1, relief="solid", padx=12, pady=8)
        status_box.pack(fill="x")
        for name in ("번역 환경 (Python 패키지)", "도우미 자동 실행 등록", "도우미", "Claude Code",
                     "Ollama (선택 사항)", "크롬 확장"):
            row = tk.Frame(status_box)
            row.pack(fill="x", pady=3)
            dot = tk.Label(row, text="●", width=2, anchor="w")
            dot.pack(side="left")
            tk.Label(row, text=name, width=20, anchor="w", font=("Malgun Gothic", 10, "bold")).pack(side="left")
            text = tk.Label(row, text="확인하는 중...", anchor="w", justify="left", wraplength=400)
            text.pack(side="left", fill="x", expand=True)
            self.rows[name] = (text, dot)

        buttons = [
            ("전부 자동으로 준비", self.auto_setup, 0, 0),
            ("상태 다시 확인", self.refresh, 0, 1),
            ("Claude 로그인 창 열기", lambda: self.login("claude"), 1, 0),
           ("크롬 확장 설치 안내", self.open_extension_wizard, 2, 0),
            ("확장 폴더 열기 (경로 복사)", self.open_extension, 2, 1),
            ("진단 정보 복사", self.copy_diagnostics, 3, 0),
            ("자동 실행 등록 해제", self.uninstall, 3, 1),
            ("로그 파일 열기", self.open_log, 4, 0),
            ("데이터 폴더 열기", self.open_data_dir, 4, 1),
        ]
        grid = tk.Frame(frame)
        grid.pack(fill="x", pady=(12, 8))
        for text, command, row, column in buttons:
            button = tk.Button(grid, text=text, command=command, padx=8, pady=6, width=26)
            button.grid(row=row, column=column, padx=4, pady=4, sticky="we")
            self.buttons.append(button)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        self.progress = ttk.Progressbar(frame, maximum=100)
        self.progress.pack(fill="x", pady=(10, 2))
        self.step_label = tk.Label(frame, text="", anchor="w", fg="#4b5563")
        self.step_label.pack(fill="x")
        tk.Label(frame, text="진행 기록", font=("Malgun Gothic", 10, "bold")).pack(anchor="w")
        self.log = scrolledtext.ScrolledText(frame, height=12, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)

    # -- background work: the window polls the queue, so it never freezes --

    def _run_in_background(self, work):
        self._set_busy(True)
        threading.Thread(target=work, daemon=True).start()

    def _set_busy(self, busy: bool):
        for button in self.buttons:
            button.configure(state="disabled" if busy else "normal")

    def _poll(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(payload)
                step = parse_step(payload)
                if step:
                    index, total, text = step
                    self.progress["value"] = (index - 1) / total * 100
                    self.step_text = f"단계 {index}/{total}: {text}"
            elif kind == "status":
                self._set_busy(False)
                if isinstance(payload, Exception):
                    self._log(f"[x] 상태를 읽지 못했어요: {payload}")
                else:
                    self.status = payload
                    self._show_status()
                    if self.offer_wizard:
                        self.offer_wizard = False
                        if not (payload.get("chromeExtension") or {}).get("profile"):
                            ExtensionWizard(self, self.root_dir)
            elif kind == "setup_done":
                self._set_busy(False)
                self._log("[ok] 자동 준비가 끝났어요." if payload == 0
                          else f"[x] 자동 준비가 실패했어요 (코드 {payload}). 위 기록을 확인하세요.")
                self.offer_wizard = payload == 0
                self.started = None
                self.progress["value"] = 100 if payload == 0 else self.progress["value"]
                self.step_text = "끝났어요" if payload == 0 else "실패했어요"
                self.step_label.configure(text=self.step_text)
                self.refresh()
        self.win.after(100, self._poll)

    def _log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _show_status(self):
        for name, text, state in status_rows(self.status):
            label, dot = self.rows[name]
            label.configure(text=text, fg=COLORS[state])
            dot.configure(fg=COLORS[state])

    # -- actions --

    def refresh(self):
        self._log("상태를 확인하는 중... 번역 환경 확인에 몇 초 걸려요.")

        def work():
            try:
                self.events.put(("status", read_status(self.root_dir)))
            except Exception as error:  # shown in the log; the window keeps working
                self.events.put(("status", error))
        self._run_in_background(work)

    def auto_setup(self):
        if not messagebox.askyesno(APP, "처음 설치는 인터넷에서 수 GB를 받아서 시간이 걸려요.\n계속할까요?"):
            return
        self._log("자동 준비를 시작했어요. 창을 닫지 마세요.")
        self.started = time.time()
        self.step_text = "시작하는 중"
        self.progress["value"] = 0
        self._tick()

        def work():
            proc = subprocess.Popen(powershell_args(self.root_dir), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
            for raw in proc.stdout:
                self.events.put(("log", decode_output(raw).rstrip()))
            self.events.put(("setup_done", proc.wait()))
        self._run_in_background(work)

    def login(self, which: str):
        if not self.status:
            self._log("먼저 상태를 확인해 주세요.")
            return
        if which == "claude":
            path = self.status.get("claudePath")
            if not path:
                messagebox.showinfo(APP, "Claude Code가 없어요. https://claude.com/claude-code 에서 설치한 뒤 다시 확인하세요.")
                return
            config_dir = Path(self.status["dataDir"]) / "claude-config"
            config_dir.mkdir(parents=True, exist_ok=True)
            args = claude_login_args(path, config_dir)
        subprocess.Popen(args, creationflags=CREATE_NEW_CONSOLE)
        self._log("로그인 창을 열었어요. 거기서 로그인한 뒤 이 창에서 '상태 다시 확인'을 누르세요.")

    def open_extension(self):
        folder = self.root_dir / "extension"
        os.startfile(str(folder))
        self.win.clipboard_clear()
        self.win.clipboard_append(str(folder))
        messagebox.showinfo(APP, "확장 폴더를 열고 경로를 복사했어요.\n\n크롬에서 chrome://extensions 를 열고, "
                                 "개발자 모드를 켠 뒤 '압축해제된 확장 프로그램 로드'를 누르고 이 폴더를 고르세요.")

    def open_extension_wizard(self):
        ExtensionWizard(self, self.root_dir)

    def _tick(self):
        if self.started is None:
            return
        elapsed = int(time.time() - self.started)
        self.step_label.configure(text=f"{self.step_text} · 경과 {elapsed // 60}분 {elapsed % 60:02d}초")
        self.win.after(1000, self._tick)

    def open_log(self):
        if not self.status:
            self._log("먼저 상태를 확인해 주세요.")
            return
        path = Path(self.status["dataDir"]) / "helper.log"
        if path.is_file():
            os.startfile(str(path))
        else:
            self._log("아직 로그 파일이 없어요. 도우미를 한 번 켜면 생겨요.")

    def open_data_dir(self):
        if not self.status:
            self._log("먼저 상태를 확인해 주세요.")
            return
        folder = Path(self.status["dataDir"])
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))

    def copy_diagnostics(self):
        if not self.status:
            self._log("먼저 상태를 확인해 주세요.")
            return
        self.win.clipboard_clear()
        self.win.clipboard_append(diagnostics_text(self.status, app_version(self.root_dir)))
        self._log("진단 정보를 복사했어요. 경로와 상태만 들어 있고 대사나 토큰은 없어요.")

    def uninstall(self):
        if not messagebox.askyesno(APP, "크롬 자동 실행 등록만 지웁니다. 캐시와 설정은 남아요.\n계속할까요?"):
            return
        script = self.root_dir / "tools" / "uninstall_windows.ps1"
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                              capture_output=True, creationflags=CREATE_NO_WINDOW)
        self._log(decode_output(proc.stdout).strip() or "[ok] 등록을 해제했어요.")
        self.refresh()

    def run(self):
        self.win.mainloop()


class ExtensionWizard:
    """Three steps in Chrome. It reads Chrome's own profile settings and marks the job done once the extension is loaded."""

    def __init__(self, parent: "SetupWindow", root_dir: Path):
        self.parent = parent
        self.ext_dir = root_dir / "extension"
        self.user_data = chrome_setup.chrome_user_data()
        self.profiles = chrome_setup.list_profiles(self.user_data) or [("Default", "Default")]
        self.done = False
        self.win = tk.Toplevel(parent.win)
        self.win.title("크롬에 확장 설치")
        self.win.geometry("620x440")
        self.win.transient(parent.win)
        frame = tk.Frame(self.win, padx=18, pady=14)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="크롬에 확장 설치 (3단계)", font=("Malgun Gothic", 13, "bold")).pack(anchor="w")
        tk.Label(frame, text="확장은 크롬 프로필마다 따로 설치돼요. 평소 쓰는 프로필을 고르세요.",
                 wraplength=570, justify="left", fg="#4b5563").pack(anchor="w", pady=(2, 8))
        self.profile_box = ttk.Combobox(frame, state="readonly", width=60,
                                        values=[f"{name} ({folder})" for folder, name in self.profiles])
        self.profile_box.current(0)
        self.profile_box.pack(anchor="w")
        tk.Label(frame, text="1. 크롬 확장 페이지를 여세요. 주소창에 chrome://extensions/ 가 안 열리면 "
                             "복사된 주소를 붙여 넣으세요.", wraplength=570, justify="left").pack(anchor="w", pady=(12, 2))
        tk.Button(frame, text="크롬 확장 페이지 열기", command=self.open_page).pack(anchor="w")
        tk.Label(frame, text="2. 오른쪽 위 '개발자 모드'를 켜고, '압축해제된 확장 프로그램을 로드합니다'를 누른 뒤 "
                             "아래 폴더를 고르세요.", wraplength=570, justify="left").pack(anchor="w", pady=(12, 2))
        path_row = tk.Frame(frame)
        path_row.pack(anchor="w", fill="x")
        tk.Label(path_row, text=str(self.ext_dir), fg="#1f2937", wraplength=400, justify="left").pack(side="left")
        tk.Button(path_row, text="폴더 경로 복사", command=self.copy_folder).pack(side="left", padx=6)
        tk.Button(path_row, text="폴더 열기", command=self.show_folder).pack(side="left")
        self.status = tk.Label(frame, text="", wraplength=570, justify="left", font=("Malgun Gothic", 11, "bold"))
        self.status.pack(anchor="w", pady=(18, 0))
        self.win.protocol("WM_DELETE_WINDOW", self.win.destroy)
        self.check()

    def chosen_folder(self) -> str:
        return self.profiles[max(self.profile_box.current(), 0)][0]

    def open_page(self):
        self.win.clipboard_clear()
        self.win.clipboard_append("chrome://extensions/")
        chrome = find_chrome()
        if not chrome:
            messagebox.showinfo(APP, "크롬을 찾지 못했어요. 주소 chrome://extensions/ 를 복사했으니 "
                                     "크롬 주소창에 붙여 넣으세요.", parent=self.win)
            return
        subprocess.Popen(chrome_setup.extensions_page_args(chrome, self.chosen_folder()))

    def copy_folder(self):
        self.win.clipboard_clear()
        self.win.clipboard_append(str(self.ext_dir))
        self.status.configure(text="폴더 경로를 복사했어요. 2단계에서 붙여 넣거나 이 폴더를 고르세요.", fg=COLORS["warn"])

    def show_folder(self):
        os.startfile(str(self.ext_dir))

    def check(self):
        if self.done or not self.win.winfo_exists():
            return
        chosen = self.chosen_folder()
        found = chrome_setup.find_extension(self.user_data, self.ext_dir)
        if found and found.get("profile") == chosen and found.get("enabled"):
            self.done = True
            self.status.configure(text="설치 완료! 이 프로필에서 확장이 켜졌어요.", fg=COLORS["ok"])
            self.parent.refresh()
            self.win.after(3000, self.win.destroy)
            return
        if found and found.get("profile") and found.get("profile") != chosen:
            text = f"이 확장은 다른 프로필({found['profile']})에 설치돼 있어요. 그 프로필을 고르면 완료로 표시돼요."
        elif found and found.get("profile"):
            text = "설치돼 있지만 꺼져 있어요. 크롬 확장 페이지에서 켜 주세요."
        elif found:
            text = "다른 폴더의 BalloonLens가 로드돼 있어요. 위 폴더를 2단계에서 골랐는지 확인하세요."
        else:
            text = "아직 설치되지 않았어요. 1~2단계를 따라 하면 여기가 자동으로 '설치 완료'로 바뀝니다."
        self.status.configure(text=text, fg=COLORS["warn"])
        self.win.after(2000, self.check)


# ---- entry point ----

def selftest(out_path: str) -> int:
    """No window: checks that the folder is found and the status reads. Writes a JSON result to out_path."""
    root = find_root(app_dir())
    if root is None:
        result = {"ok": False, "error": "setup folder not found"}
    else:
        status = read_status(root)
        result = {"ok": True, "root": str(root), "version": app_version(root),
                  "rows": [name for name, _, _ in status_rows(status)]}
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if result["ok"] else 1


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest(argv[argv.index("--selftest") + 1])
    hold = float(argv[argv.index("--hold") + 1]) if "--hold" in argv else None
    root = find_root(app_dir())
    if root is None:
        hidden = tk.Tk()
        hidden.withdraw()
        messagebox.showerror(APP, "설치 폴더를 찾지 못했어요. 이 파일을 ZIP을 푼 폴더 "
                                  "(tools, helper, extension 폴더가 있는 곳)에 두고 다시 실행하세요.")
        return 1
    window = SetupWindow(root, hold)
    if "--wizard" in argv:  # for screenshots: open the Chrome extension steps right away
        window.open_extension_wizard()
    window.run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
