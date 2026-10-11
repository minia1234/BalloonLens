"""Run the local helper:  python -m balloonlens [--warmup] [--background] [--cpu]"""
import argparse
import json
import socket
import sys
import threading
import urllib.request

import uvicorn

from . import config
from .backends import ClaudeCodeBackend, CliTranslator, CodexBackend, QuotaPolicy, Router
from .cache import Cache
from .jobs import JobManager
from .ocr import OcrEngine, decode_image
from .server import create_app
from .translate import Translator, ollama_tags


def port_state(port: int) -> str:
    """'free', 'balloonlens' (our helper answers the pairing call) or 'other' (another program holds the port)."""
    with socket.socket() as s:
        s.settimeout(0.3)
        if s.connect_ex((config.HOST, port)) != 0:
            return "free"
    origin = f"chrome-extension://{next(iter(sorted(config.EXTENSION_IDS)), '')}"
    try:
        request = urllib.request.Request(f"http://{config.HOST}:{port}/v1/pair", method="POST",
                                         headers={"Origin": origin})
        with urllib.request.urlopen(request, timeout=2) as resp:
            return "balloonlens" if "token" in json.loads(resp.read() or b"{}") else "other"
    except (OSError, ValueError):
        return "other"


def build_router(cache, local) -> Router:
    """Claude Code and Codex backends plus the shared usage ledger. Building this starts no CLI process."""
    quota = QuotaPolicy(cache)
    claude = ClaudeCodeBackend(command=config.claude_command(), config_dir=config.CLAUDE_CONFIG_DIR,
                               sandbox_dir=config.CLAUDE_SANDBOX_DIR, runtime_dir=config.RUNTIME_DIR,
                               model=config.CLAUDE_MODEL, effort=config.CLAUDE_EFFORT, timeout=config.CLI_TIMEOUT_S)
    codex = CodexBackend(command=config.codex_command(), sandbox_dir=config.CODEX_SANDBOX_DIR,
                         runtime_dir=config.RUNTIME_DIR, model=config.CODEX_MODEL, reasoning=config.CODEX_REASONING,
                         timeout=config.CLI_TIMEOUT_S,
                         sandbox_verdict=cache.get_flags("codex").get("verdict", ""))
    backends = {"claude": claude, "codex": codex}
    cli = {name: CliTranslator(backend, cache, quota) for name, backend in backends.items()}
    return Router(local=local, cli=cli, backends=backends, quota=quota, cache=cache)


def check_backends_in_background(router: Router):
    """Login and sandbox state are read once at start-up so the first job does not wait for it."""
    def run():
        for name in router.backends:
            router.status(name, refresh=True)
    threading.Thread(target=run, name="backend-check", daemon=True).start()


def main():
    parser = argparse.ArgumentParser(prog="balloonlens")
    parser.add_argument("--warmup", action="store_true", help="load OCR models right after start")
    parser.add_argument("--background", action="store_true", help="no console: log to file and warm up OCR")
    parser.add_argument("--cpu", action="store_true", help="run OCR on CPU")
    args = parser.parse_args()

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if args.background or sys.stdout is None:  # pythonw has no stdout
        log = open(config.LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log

    state = port_state(config.PORT)
    if state == "balloonlens":
        print(f"이미 실행 중: http://{config.HOST}:{config.PORT}")
        return
    if state == "other":
        print(f"포트 {config.PORT}을 다른 프로그램이 쓰고 있어요. BalloonLens 도우미를 켤 수 없어요. "
              "그 프로그램을 끄거나 재시작한 뒤 다시 실행하세요.")
        sys.exit(1)

    token = config.ensure_token()
    cache = Cache(config.DB_PATH)
    local = Translator(cache)
    router = build_router(cache, local)
    ocr = OcrEngine(gpu=not args.cpu)
    manager = JobManager(ocr, local, cache, decode_image, metrics_path=config.METRICS_PATH, router=router)

    # Ollama is optional since v0.5.0: the subscription backends work without it.
    installed = ollama_tags()
    if installed is None:
        print("[i] Ollama 꺼짐: 로컬 모델은 못 쓰지만 구독 번역은 그대로 쓸 수 있어요")
    else:
        missing = [m for m in config.LOCAL_MODELS if m not in installed]
        if missing:
            print("[i] 로컬 모델 미설치(선택 사항): " + ", ".join(missing))

    manager.start()
    check_backends_in_background(router)
    if args.warmup or args.background:
        # Serve immediately; OCR models load behind the scenes (status.ocr_ready tells the UI).
        threading.Thread(target=ocr.warmup, name="ocr-warmup", daemon=True).start()

    print(f"BalloonLens helper {config.VERSION}  http://{config.HOST}:{config.PORT}")
    print("확장 프로그램은 자동으로 연결됨")
    uvicorn.run(create_app(manager, token), host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
