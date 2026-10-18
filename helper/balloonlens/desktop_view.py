"""Tk screen selection and a transparent, click-through translation overlay."""
import base64
import binascii
import io
import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageTk

TRANSPARENT = "#ff00ff"


class RegionSelector:
    def __init__(self, root, native, done, *, ui=None):
        self.native, self.done = native, done
        self.bounds = native.bounds()
        self.start = None
        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.35)
        self.canvas = tk.Canvas(self.window, bg="#101827", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        native.place(self.window, self.bounds)
        width = self.bounds[2] - self.bounds[0]
        hint = "번역할 범위를 드래그하세요 · Esc 취소"
        self.canvas.create_text(width // 2, 40, text=ui.text(hint) if ui else hint,
                                fill="white", font=("맑은 고딕", 18, "bold"))
        self.box = self.canvas.create_rectangle(0, 0, 0, 0, outline="#5eead4", width=4)
        self.canvas.bind("<ButtonPress-1>", self._start)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._finish)
        self.window.bind("<Escape>", lambda _event: self.close(None))
        self.window.grab_set()
        self.window.focus_force()

    def _point(self, event):
        left, top, right, bottom = self.bounds
        return max(left, min(right, event.x_root)), max(top, min(bottom, event.y_root))

    def _start(self, event):
        self.start = self._point(event)

    def _move(self, event):
        if self.start is None:
            return
        x, y = self._point(event)
        ox, oy = self.bounds[:2]
        self.canvas.coords(self.box, self.start[0] - ox, self.start[1] - oy, x - ox, y - oy)

    def _finish(self, event):
        if self.start is None:
            return
        x, y = self._point(event)
        left, right = sorted((self.start[0], x))
        top, bottom = sorted((self.start[1], y))
        if right - left >= 32 and bottom - top >= 32:
            self.close((left, top, right, bottom))

    def close(self, region):
        self.window.grab_release()
        self.window.destroy()
        self.done(region)


def _wrap(text, font, width):
    lines = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if font.measure(candidate) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ""
            for char in word:
                if line and font.measure(line + char) > width:
                    lines.append(line)
                    line = ""
                line += char
        lines.append(line)
    return lines


def _fitted(text, canvas, width, height, scale=1.0):
    width, height = max(1, width), max(1, height)
    for size in range(round(24 * scale), round(11 * scale) - 1, -1):
        font = tkfont.Font(root=canvas, family="맑은 고딕", size=-size)
        lines = _wrap(text, font, width)
        if len(lines) * font.metrics("linespace") <= height:
            return "\n".join(lines), font
    count = max(1, height // font.metrics("linespace"))
    shown = lines[:count]
    if len(lines) > count:
        last = shown[-1]
        while last and font.measure(last + "…") > width:
            last = last[:-1]
        shown[-1] = last + "…"
    return "\n".join(shown), font


class TranslationOverlay:
    def __init__(self, root, native):
        self.native = native
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-transparentcolor", TRANSPARENT)
        self.window.configure(bg=TRANSPARENT)
        self.canvas = tk.Canvas(self.window, bg=TRANSPARENT, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.images, self.fonts = [], []
        self.rect = None
        self.has_text = False
        self.font_scale = 1.0
        self.show_corners = True
        self.capture_excluded = native.exclude_capture(self.window)
        native.click_through(self.window)

    def configure_display(self, scale, opacity, corners):
        self.font_scale, self.show_corners = scale, corners
        self.window.attributes("-alpha", opacity)

    def hide(self):
        self.window.withdraw()

    def show(self):
        if self.rect is not None and self.has_text:
            self.window.deiconify()
            self.native.click_through(self.window)
            self.native.place(self.window, self.rect, passive=True)

    def clear(self):
        self.hide()
        self.canvas.delete("all")
        self.images.clear()
        self.fonts.clear()
        self.has_text = False

    def draw(self, rect, snapshot):
        self.clear()
        self.rect = rect
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        self.native.place(self.window, rect, passive=True)
        sx, sy = width / max(1, snapshot.get("width", width)), height / max(1, snapshot.get("height", height))
        shown = [s for s in snapshot.get("segments", []) if s.get("tgt") and s.get("status") in ("done", "review")]
        for seg in shown:
            presentation = seg.get("presentation") or {}
            if presentation.get("cleanup") and presentation.get("cleanup_bbox"):
                try:
                    raw = base64.b64decode(presentation["cleanup"].split(",", 1)[-1], validate=True)
                    with Image.open(io.BytesIO(raw)) as source:
                        px, py, pw, ph = presentation["cleanup_bbox"]
                        patch = source.convert("RGBA").resize((max(1, round(pw * sx)), max(1, round(ph * sy))))
                    photo = ImageTk.PhotoImage(patch, master=self.canvas)
                    self.images.append(photo)
                    self.canvas.create_image(round(px * sx), round(py * sy), image=photo, anchor="nw")
                except (binascii.Error, OSError, ValueError):
                    pass
        for seg in shown:
            frame = (seg.get("presentation") or {}).get("frame") or seg["bbox"]
            x, y, w, h = frame
            left, top = max(0, round(x * sx)), max(0, round(y * sy))
            right, bottom = min(width, round((x + w) * sx)), min(height, round((y + h) * sy))
            if right - left < 8 or bottom - top < 8:
                continue
            self.canvas.create_rectangle(left, top, right, bottom, fill="#fffefa", outline="")
            text, font = _fitted(seg["tgt"], self.canvas, right - left - 6, bottom - top - 4, self.font_scale)
            self.fonts.append(font)
            self.canvas.create_text((left + right) / 2, (top + bottom) / 2, text=text, font=font,
                                    fill="#92400e" if seg["status"] == "review" else "#111827", justify="center")
        self.has_text = bool(shown)
        if self.show_corners:
            for x, y, dx, dy in ((2, 2, 1, 1), (width - 2, 2, -1, 1),
                                  (2, height - 2, 1, -1), (width - 2, height - 2, -1, -1)):
                self.canvas.create_line(x, y + dy * 10, x, y, x + dx * 10, y, fill="#27b7a8", width=2)
            self.has_text = True

    def close(self):
        self.window.destroy()
