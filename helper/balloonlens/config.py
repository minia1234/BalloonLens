"""Paths, token, and tunables for the local helper."""
import json
import os
import secrets
import shutil
from pathlib import Path

VERSION = "0.5.0"
HOST = "127.0.0.1"
# Only this extension may auto-pair (fixed by the "key" in extension/manifest.json).
EXTENSION_IDS = {i for i in os.environ.get("BALLOONLENS_EXTENSION_IDS", "pnccemlpjodglelglkjjdnnmdknejgop").split(",") if i}
PORT = int(os.environ.get("BALLOONLENS_PORT", "8787"))

DATA_DIR = Path(os.environ.get("BALLOONLENS_DATA", Path(os.environ.get("LOCALAPPDATA", Path.home())) / "BalloonLens"))
DB_PATH = DATA_DIR / "cache.sqlite3"
TOKEN_PATH = DATA_DIR / "token.txt"
METRICS_PATH = DATA_DIR / "metrics.jsonl"
LOG_PATH = DATA_DIR / "helper.log"

# Subscription CLI backends (v0.5.0). The Claude login lives in its own folder, so the reader's personal
# CLAUDE.md and settings are never read by the translation calls.
CLAUDE_CONFIG_DIR = Path(os.environ.get("BALLOONLENS_CLAUDE_CONFIG_DIR", DATA_DIR / "claude-config"))
CLAUDE_SANDBOX_DIR = DATA_DIR / "sandbox"
CODEX_SANDBOX_DIR = DATA_DIR / "codex-sandbox"
RUNTIME_DIR = DATA_DIR / "runtime"
CLAUDE_EFFORT = os.environ.get("BALLOONLENS_CLAUDE_EFFORT", "low")
CODEX_REASONING = os.environ.get("BALLOONLENS_CODEX_REASONING", "low")
CLI_TIMEOUT_S = float(os.environ.get("BALLOONLENS_CLI_TIMEOUT", "150"))
# How many subscription CLI calls run at once (images and chunks of one page share this). 1 = one at a time.
CLI_PARALLEL = max(1, min(4, int(os.environ.get("BALLOONLENS_CLI_PARALLEL", "3"))))
# How many subscription jobs may be in flight at once. A job reads its image first, then waits for CLI calls, so this
# must be larger than CLI_PARALLEL: otherwise a waiting translation holds back the next image's reading.
CLI_JOB_SLOTS = 6


def ollama_base_url(value: str | None) -> str:
    """OLLAMA_HOST is often a server bind value like '0.0.0.0' or ':11434'; turn it into a client URL."""
    value = (value or "").strip().rstrip("/")
    scheme = "http"
    if "://" in value:
        scheme, value = value.split("://", 1)
    host, _, port = value.rpartition(":") if ":" in value else (value, "", "")
    if host in ("", "0.0.0.0", "[::]", "::"):
        host = "127.0.0.1"
    return f"{scheme}://{host}:{port or '11434'}"


OLLAMA_URL = ollama_base_url(os.environ.get("OLLAMA_HOST"))
OLLAMA_KEEP_ALIVE = os.environ.get("BALLOONLENS_KEEP_ALIVE", "15m")

LOCAL_MODELS = ["translategemma:4b", "qwen3:8b", "qwen3.5:9b"]
CLAUDE_MODEL = "claude-haiku-5-5"
CODEX_MODEL = "gpt-6-luna"
SUBSCRIPTION_MODELS = [CLAUDE_MODEL, CODEX_MODEL]
DEFAULT_MODEL = "qwen3.5:9b"  # helper-side default for API calls that omit a model
SUPPORTED_MODELS = LOCAL_MODELS + SUBSCRIPTION_MODELS

# Reader consent is tied to this text. Change it when what leaves the PC changes; old consent then stops working.
CONSENT_VERSION = "2026-10-10-text-only-v2"

# Request limits (section 14.1)
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 60_000_000
MAX_QUEUED_JOBS = 64
MAX_TRANSLATE_RETRIES = 1

# Bump when detection/grouping/recognition behavior changes -> invalidates OCR cache.
OCR_VERSION = "p0-easyocr-craft+mangaocr-v3-retry"
# Bump when prompts change -> invalidates translation cache.
PROMPT_VERSION = "reading-v2"
CLI_PROMPT_VERSION = "subs-v1"


def ensure_token() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    return token


def _command_from_env(name: str) -> list[str] | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return [raw]
    if isinstance(value, list) and value:
        return [str(v) for v in value]
    return [raw]


def claude_command() -> list[str]:
    """Claude Code executable. Tests override with BALLOONLENS_CLAUDE_COMMAND (a JSON list)."""
    override = _command_from_env("BALLOONLENS_CLAUDE_COMMAND")
    if override:
        return override
    found = shutil.which("claude")
    if found and found.lower().endswith(".exe"):
        return [found]
    return [str(Path.home() / ".local" / "bin" / "claude.exe")]


def codex_command() -> list[str]:
    """Codex native executable. The npm shims (.ps1/.cmd) are never used: they re-parse arguments through a shell."""
    override = _command_from_env("BALLOONLENS_CODEX_COMMAND")
    if override:
        return override
    appdata = os.environ.get("APPDATA")
    if appdata:
        native = (Path(appdata) / "npm" / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
                  / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe")
        if native.exists():
            return [str(native)]
    found = shutil.which("codex")
    if found and found.lower().endswith(".exe"):
        return [found]
    return [str(native)] if appdata else ["codex"]
