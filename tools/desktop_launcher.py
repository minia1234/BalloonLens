"""Small desktop launcher, also packaged as BalloonLens.exe without bundling the OCR models."""
import ctypes
import subprocess
import sys
from pathlib import Path


def main():
    start = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    root = next((folder for folder in (start, *start.parents)
                 if (folder / "helper" / "balloonlens" / "desktop.py").is_file()), None)
    if root is None:
        ctypes.windll.user32.MessageBoxW(None, "BalloonLens 폴더 안에서 실행해 주세요.\n실행 파일과 helper 폴더가 함께 있어야 해요.", "말풍선 렌즈", 0x10)
        return
    python = root / "helper" / ".venv" / "Scripts" / "pythonw.exe"
    if not python.is_file():
        ctypes.windll.user32.MessageBoxW(None, "번역 환경이 아직 없어요.\n같은 폴더의 Setup.cmd를 먼저 실행해 주세요.", "말풍선 렌즈", 0x10)
        return
    try:
        if getattr(sys, "frozen", False):
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        subprocess.Popen([str(python), "-m", "balloonlens.desktop"], cwd=str(root / "helper"),
                         creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        ctypes.windll.user32.MessageBoxW(None, "프로그램을 켜지 못했어요.\nSetup.cmd로 번역 환경을 준비해 주세요.", "말풍선 렌즈", 0x10)


if __name__ == "__main__":
    main()
