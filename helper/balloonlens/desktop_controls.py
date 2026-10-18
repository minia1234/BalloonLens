"""Recorder-like controls. Settings and the transcript remain closed while the reader uses another app."""
import tkinter as tk
from tkinter import scrolledtext, ttk

from . import config
from .ui_locale import LANGUAGE_CHOICES, PACKS

SOURCES = {"자동 인식": "auto", "일본어": "ja", "영어": "en", "한국어": "ko",
           "简体中文": "zh-Hans", "繁體中文": "zh-Hant"}
TARGETS = {({"en": "영어", "ja": "일본어"}.get(code, label)): code for code, label in PACKS["languages"].items()}
MODELS = {"Claude 구독": config.CLAUDE_MODEL, "로컬 Qwen3.5": "qwen3.5:9b"}
DIRECTIONS = {"만화 ←": "rtl", "가로 →": "ltr", "웹툰 ↓": "ttb"}
REVIEWS = {"자동": None, "끔": "off", "필요한 대사만 다시 보기": "selective", "모든 대사 다시 보기 (느림)": "all"}


def _popup(app, title):
    window = tk.Toplevel(app.root)
    window.withdraw()
    window.title(title)
    window.attributes("-topmost", True)
    window.protocol("WM_DELETE_WINDOW", window.withdraw)
    return window


def build(app):
    root = app.root
    root.title("BalloonLens")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#17252a")
    bar = tk.Frame(root, bg="#17252a", padx=8, pady=7)
    bar.pack(fill="both", expand=True)
    handle = tk.Label(bar, text="⠿", font=("Segoe UI", 15), bg="#17252a", fg="#829a9f", cursor="fleur")
    handle.pack(side="left", padx=(0, 6))
    app.bar_status = tk.StringVar(value="준비됨")
    label = tk.Label(bar, textvariable=app.bar_status, bg="#17252a", fg="#72dfc8", font=("맑은 고딕", 10), width=13)
    label.pack(side="left", padx=(0, 8))
    for widget in (handle, label):
        widget.bind("<ButtonPress-1>", app._drag_start)
        widget.bind("<B1-Motion>", app._drag_move)
        widget.bind("<ButtonRelease-1>", lambda _event: app._save())

    def button(text, command, primary=False):
        item = tk.Button(bar, text=text, command=command, relief="flat", bd=0, cursor="hand2",
                         bg="#167e78" if primary else "#17252a", fg="white", activebackground="#245054",
                         activeforeground="white", padx=9, pady=5, font=("맑은 고딕", 10))
        item.pack(side="left", padx=2)
        return item

    app.region_button = button("범위 선택", app.select_region)
    app.toggle_button = button("번역 시작", app.toggle, True)
    app.stop_button = button("■", app.stop)
    app.peek_button = button("원문 보기", app.peek)
    app.settings_button = button("⚙", app.show_settings)

    app.src = tk.StringVar(value="자동 인식")
    app.tgt = tk.StringVar(value="한국어")
    app.model = tk.StringVar(value="Claude 구독")
    app.reading = tk.StringVar(value="만화 ←")
    app.review = tk.StringVar(value="자동")
    app.ui_language = tk.StringVar(value="한국어 (ko)")
    app.automatic = tk.BooleanVar(value=True)
    app.font_scale = tk.DoubleVar(value=1.0)
    app.opacity = tk.DoubleVar(value=1.0)
    app.settle_time = tk.DoubleVar(value=0.8)
    app.show_corners = tk.BooleanVar(value=True)
    app.status = tk.StringVar(value="범위와 목표 언어를 선택한 뒤 번역을 시작하세요.")
    app.region_text = tk.StringVar(value="아직 범위를 선택하지 않았어요.")
    app.usage = tk.StringVar(value="Claude 남은 사용량 확인 중…")
    app.language_status = tk.StringVar(value="")
    app.ui_choices = []

    window = app.settings_window = _popup(app, "번역 설정")
    window.resizable(False, False)
    shell = ttk.Frame(window)
    shell.pack(fill="both", expand=True)
    canvas = app.settings_canvas = tk.Canvas(shell, highlightthickness=0, width=450)
    scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    frame = app.settings_frame = ttk.Frame(canvas, padding=16)
    canvas.create_window((0, 0), window=frame, anchor="nw")
    frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
    def scroll(event):
        if event.widget.winfo_class() not in {"TCombobox", "TSpinbox"}:
            canvas.yview_scroll(-int(event.delta / 120), "units")
    window.bind("<MouseWheel>", scroll)
    ttk.Label(frame, text="번역 설정", font=("맑은 고딕", 14, "bold")).pack(anchor="w", pady=(0, 12))

    def combo(parent, title, variable, values, *, editable=False):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=title, width=13).pack(side="left")
        box = ttk.Combobox(row, textvariable=variable, values=list(values), width=27,
                           state="normal" if editable else "readonly")
        box.pack(side="left", fill="x", expand=True)
        box._ui_values = {value: value for value in values}
        box.bind("<<ComboboxSelected>>", app._settings_changed)
        box.bind("<Return>", app._settings_changed)
        box.bind("<FocusOut>", app._settings_changed)
        app.ui_choices.append((box, variable, values))
        return box

    # Keep the same tuple order for persisted source/target/model/reading settings.
    target_row = ttk.Frame(frame)
    target_row.pack(fill="x")
    combo(target_row, "목표 언어", app.tgt, TARGETS, editable=True)
    ttk.Label(frame, text="목록에 없는 언어는 이름이나 코드를 직접 입력하세요.", wraplength=420).pack(anchor="w", pady=(2, 0))
    ttk.Checkbutton(frame, text="화면이 바뀌면 자동 번역", variable=app.automatic,
                    command=app._settings_changed).pack(anchor="w", pady=8)
    language_row = ttk.Frame(frame)
    language_row.pack(fill="x", pady=4)
    ttk.Label(language_row, text="화면 언어", width=13).pack(side="left")
    language = ttk.Combobox(language_row, textvariable=app.ui_language, values=LANGUAGE_CHOICES, width=20)
    language.pack(side="left", fill="x", expand=True)
    language.bind("<<ComboboxSelected>>", lambda _event: app.change_ui_language())
    language.bind("<Return>", lambda _event: app.change_ui_language())
    ttk.Button(language_row, text="적용", command=app.change_ui_language).pack(side="left", padx=(6, 0))
    ttk.Label(frame, textvariable=app.language_status, wraplength=420).pack(anchor="w")
    ttk.Separator(frame).pack(fill="x", pady=10)
    ttk.Label(frame, textvariable=app.usage, wraplength=420).pack(anchor="w")
    ttk.Button(frame, text="Claude 구독 로그인", command=app.login).pack(anchor="w", pady=(8, 4))
    ttk.Label(frame, textvariable=app.status, wraplength=420, foreground="#167e78").pack(anchor="w", pady=6)
    ttk.Label(frame, textvariable=app.region_text, wraplength=420).pack(anchor="w")
    ttk.Button(frame, text="범위 선택", command=app.select_region).pack(anchor="w", pady=4)

    advanced = ttk.Frame(frame)
    def expand():
        if advanced.winfo_manager():
            advanced.pack_forget()
        else:
            advanced.pack(fill="x", pady=(8, 0), before=bottom)
        app._place_popup(window)
    ttk.Button(frame, text="고급 설정", command=expand).pack(anchor="w", pady=(8, 0))
    combo(advanced, "원문 언어", app.src, SOURCES)
    combo(advanced, "번역 방식", app.model, MODELS)
    combo(advanced, "읽는 방향", app.reading, DIRECTIONS)
    combo(advanced, "대사 검수", app.review, REVIEWS)
    by_variable = {str(row[1]): row for row in app.ui_choices}
    app.ui_choices = [by_variable[str(variable)] for variable in (app.src, app.tgt, app.model, app.reading, app.review)]
    for title, variable, lower, upper, step in (("글자 크기", app.font_scale, 0.7, 1.5, 0.1),
                                                ("번역 표시 불투명도", app.opacity, 0.5, 1.0, 0.1),
                                                ("화면 안정 대기(초)", app.settle_time, 0.4, 2.0, 0.1)):
        row = ttk.Frame(advanced)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=title).pack(side="left")
        spin = ttk.Spinbox(row, textvariable=variable, from_=lower, to=upper, increment=step, width=8,
                           command=app._settings_changed)
        spin.pack(side="right")
        spin.bind("<FocusOut>", app._settings_changed)
        spin.bind("<Return>", app._settings_changed)
    ttk.Checkbutton(advanced, text="번역 범위 모서리 표시", variable=app.show_corners,
                    command=app._settings_changed).pack(anchor="w", pady=4)
    ttk.Label(advanced, text="자동 인식은 설치된 OCR 모델을 사용해요. 추가 모델은 자동 다운로드하지 않아요.",
              wraplength=420).pack(anchor="w", pady=4)
    ttk.Label(advanced, text="Ctrl+Alt+R 범위 · Ctrl+Alt+T 번역 · Ctrl+Alt+O 원문 · Ctrl+Alt+S 일시정지",
              wraplength=420).pack(anchor="w", pady=4)
    bottom = ttk.Frame(frame)
    bottom.pack(fill="x", pady=(10, 0))
    ttk.Button(bottom, text="대사 목록", command=app.show_transcript).pack(side="left")
    ttk.Button(bottom, text="프로그램 종료", command=app.close).pack(side="right")
    ttk.Button(bottom, text="닫기", command=window.withdraw).pack(side="right", padx=6)

    transcript_window = app.transcript_window = _popup(app, "대사 목록")
    transcript_window.geometry("560x420")
    app.transcript = scrolledtext.ScrolledText(transcript_window, font=("맑은 고딕", 11), wrap="word", state="disabled")
    app.transcript.pack(fill="both", expand=True, padx=12, pady=12)
    app.capture_windows = [(root, False), (window, app.native.exclude_capture(window)),
                           (transcript_window, app.native.exclude_capture(transcript_window))]
