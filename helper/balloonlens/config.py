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


def claude_config_dir() -> Path:
    """Reuse OAuth in place. Keep an existing app account; never copy or log credential contents."""
    override = os.environ.get("BALLOONLENS_CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    existing = DATA_DIR / "claude-config"
    user = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    for folder in (existing, user):
        try:
            data = json.loads((folder / ".credentials.json").read_text(encoding="utf-8"))
            oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
            if isinstance(oauth, dict) and (oauth.get("accessToken") or oauth.get("refreshToken")):
                return folder
        except (OSError, ValueError):
            pass
    return user


# Authentication is reused; personal instructions/settings are excluded by the backend's CLI flags.
CLAUDE_CONFIG_DIR = claude_config_dir()
CLAUDE_SANDBOX_DIR = DATA_DIR / "sandbox"
RUNTIME_DIR = DATA_DIR / "runtime"
CLAUDE_EFFORT = os.environ.get("BALLOONLENS_CLAUDE_EFFORT", "low")
CLI_TIMEOUT_S = float(os.environ.get("BALLOONLENS_CLI_TIMEOUT", "150"))
# How many subscription CLI calls run at once (images and chunks of one page share this). 1 = one at a time.
CLI_PARALLEL = max(1, min(4, int(os.environ.get("BALLOONLENS_CLI_PARALLEL", "3"))))
# How many subscription jobs may be in flight at once. A job reads its image first, then waits for CLI calls, so this
# must be larger than CLI_PARALLEL: otherwise a waiting translation holds back the next image's reading.
CLI_JOB_SLOTS = 6
# How many Claude CLI processes wait, already started, for the next translation call. A call that finds one answers in
# about 0.9 s, one that does not starts its own (about 2.3 s); 0 = the old way, one `claude -p` per call (about 3.1 s).
# Each waiting process uses about 180 MB and is stopped after CLI_WARM_TTL_S seconds without being needed.
CLI_WARM = max(0, min(4, int(os.environ.get("BALLOONLENS_CLI_WARM", "2"))))
CLI_WARM_TTL_S = 600.0


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
SUBSCRIPTION_MODELS = [CLAUDE_MODEL]
DEFAULT_MODEL = "qwen3.5:9b"  # helper-side default for API calls that omit a model
SUPPORTED_MODELS = LOCAL_MODELS + SUBSCRIPTION_MODELS

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
    """Claude Code executable. Tests override with BALLOONLENS_CLAUDE_COMMAND (a JSON list).

    The official installer's claude.exe comes first. An npm install only has claude.cmd; login and status work through
    it too, so it is used when nothing else is found. Without any match the official path is returned, so the error
    names the place the program was expected."""
    override = _command_from_env("BALLOONLENS_CLAUDE_COMMAND")
    if override:
        return override
    official = Path.home() / ".local" / "bin" / "claude.exe"
    if official.is_file():
        return [str(official)]
    found = shutil.which("claude.exe") or shutil.which("claude.cmd")
    if found:
        return [found]
    return [str(official)]
