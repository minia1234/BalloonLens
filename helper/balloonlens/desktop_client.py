"""Desktop access to the existing loopback helper. Screen pixels never go to a cloud endpoint."""
import io
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import config

FINISHED = {"done", "partial", "failed", "cancelled"}
MESSAGES = {
    "not_logged_in": "Claude 로그인이 필요해요. 설치 도우미에서 로그인해 주세요.",
    "auth_expired": "Claude 로그인이 만료됐어요. 다시 로그인해 주세요.",
    "not_installed": "Claude Code가 없어요. https://claude.com/claude-code 에서 설치한 뒤 다시 시도해 주세요.",
    "quota_paused": "Claude 사용량 한도로 대기 중이에요. 남은 사용량을 확인해 주세요.",
    "quota_exhausted": "Claude 사용량 한도에 도달했어요.",
    "api_key_detected": "Claude 전용 폴더에서 구독 계정으로 로그인해 주세요.",
    "queue_full": "번역 작업이 많아요. 잠시 뒤 다시 시도해 주세요.",
    "timeout": "번역 응답이 늦어졌어요. 이번 화면 번역으로 다시 시도해 주세요.",
    "unauthorized": "도우미 로그인 폴더와 프로그램의 데이터 폴더가 달라요.",
    "ocr_model_missing": "선택한 원문 언어의 OCR 모델이 없어요. 자동 인식을 사용하거나 해당 모델을 준비해 주세요.",
}


class TranslationCancelled(Exception):
    pass


class HelperError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class DesktopClient:
    def __init__(self):
        self.base = f"http://{config.HOST}:{config.PORT}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request(self, method, path, data=None, *, content_type="application/json", timeout=8):
        token = config.TOKEN_PATH.read_text(encoding="utf-8").strip()
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"X-BalloonLens-Token": token, "Content-Type": content_type})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
                code = body.get("error") or body.get("detail")
            except (ValueError, AttributeError):
                code = None
            raise HelperError(MESSAGES.get(str(code), f"도우미 요청 실패 ({exc.code})")) from None

    def ensure_running(self, cancel):
        try:
            if self.request("GET", "/v1/status", timeout=3).get("ok"):
                return
        except (OSError, ValueError):
            pass
        if cancel.is_set():
            raise TranslationCancelled()
        # Use the same Python environment, data directory and account as the extension helper.
        subprocess.Popen([sys.executable, "-m", "balloonlens", "--background"],
                         cwd=str(Path(__file__).resolve().parents[1]), creationflags=subprocess.CREATE_NO_WINDOW,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if cancel.wait(0.3):
                raise TranslationCancelled()
            try:
                if self.request("GET", "/v1/status", timeout=3).get("ok"):
                    return
            except (OSError, ValueError):
                continue
        raise HelperError("도우미를 켜지 못했어요. Setup.cmd로 번역 환경을 준비해 주세요.")

    def translate(self, image, settings, cancel, progress):
        self.ensure_running(cancel)
        if cancel.is_set():
            raise TranslationCancelled()
        encoded = io.BytesIO()
        image.save(encoded, "PNG")
        query = urllib.parse.urlencode({"src": settings["src"], "tgt": settings["tgt"],
                                        "model": settings["model"], "reading": settings["reading"],
                                        "priority": 1, "session": "desktop-" + uuid.uuid4().hex, "scope": "desktop"})
        options = json.dumps({"review": settings.get("review")}).encode("utf-8")
        frame = len(options).to_bytes(4, "big") + options + encoded.getvalue()
        created = self.request("POST", "/v1/jobs?" + query, frame, content_type="application/x-balloonlens-page", timeout=30)
        job_id = created["id"]
        finished = False
        deadline = time.monotonic() + 600
        try:
            while time.monotonic() < deadline:
                if cancel.is_set():
                    raise TranslationCancelled()
                snapshot = self.request("GET", "/v1/jobs/" + job_id)
                state = snapshot.get("status")
                progress(state)
                if state in FINISHED:
                    finished = True
                    if state == "cancelled":
                        raise TranslationCancelled()
                    if state == "failed":
                        code = snapshot.get("error")
                        raise HelperError(MESSAGES.get(code, f"번역하지 못했어요 ({code or 'unknown'})."))
                    return snapshot
                cancel.wait(0.25)
            raise HelperError(MESSAGES["timeout"])
        finally:
            if not finished:
                try:
                    self.request("POST", f"/v1/jobs/{job_id}/cancel", timeout=3)
                except (OSError, ValueError, HelperError):
                    pass
