"""Run the local helper:  python -m balloonlens [--no-warmup] [--background] [--cpu]"""
import argparse
import json
import socket
import sys
import threading
import time
import urllib.request

import uvicorn

from . import config
from .backends import ClaudeCodeBackend, CliTranslator, QuotaPolicy, Router
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
    """The Claude Code backend plus the shared usage ledger. Building this starts no CLI process: page jobs
    prepare waiting processes before OCR; direct translation calls can also start them on demand."""
    quota = QuotaPolicy(cache)
    claude = ClaudeCodeBackend(command=config.claude_command(), config_dir=config.CLAUDE_CONFIG_DIR,
                               sandbox_dir=config.CLAUDE_SANDBOX_DIR, runtime_dir=config.RUNTIME_DIR,
                               model=config.CLAUDE_MODEL, effort=config.CLAUDE_EFFORT, timeout=config.CLI_TIMEOUT_S,
                               warm=config.CLI_WARM, warm_ttl=config.CLI_WARM_TTL_S)
    backends = {"claude": claude}
    cli = {name: CliTranslator(backend, cache, quota) for name, backend in backends.items()}
    return Router(local=local, cli=cli, backends=backends, quota=quota, cache=cache)


def check_backends_in_background(router: Router):
    """Login and sandbox state are read once at start-up so the first job does not wait for it."""
    def run():
        for name in router.backends:
            router.status(name, refresh=True)
    threading.Thread(target=run, name="backend-check", daemon=True).start()


def warm_up(ocr: OcrEngine):
    """Loads both OCR models and runs each once. The time goes to the log: it is what the first page would otherwise wait for."""
    t0 = time.perf_counter()
    ocr.warmup()
    print(f"OCR 모델 준비: {time.perf_counter() - t0:.1f}초")


def main():
    parser = argparse.ArgumentParser(prog="balloonlens")
    parser.add_argument("--warmup", action="store_true", help="load OCR models right after start (the default; kept for old shortcuts)")
    parser.add_argument("--no-warmup", action="store_true", help="load OCR models when the first page needs them")
    parser.add_argument("--background", action="store_true", help="no console: log to file")
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
    if not args.no_warmup:
        # Serve immediately; OCR models load behind the scenes (status.ocr_ready tells the UI). The first page then waits for
        # nothing but the models: a page that arrives while they load queues behind the warm-up pass.
        threading.Thread(target=warm_up, args=(ocr,), name="ocr-warmup", daemon=True).start()

    print(f"BalloonLens helper {config.VERSION}  http://{config.HOST}:{config.PORT}")
    print("확장 프로그램은 자동으로 연결됨")
    uvicorn.run(create_app(manager, token), host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
