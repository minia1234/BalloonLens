"""Shared pieces for the subscription CLI backends: errors, status, a safe process runner, env allowlist.

Everything here is deliberately boring: no model knowledge, no translation logic. The Claude and Codex
adapters build on it, and tests drive it with small fake CLIs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

# Codes that stop every further call to that backend until the reader fixes the cause.
FATAL_CODES = frozenset({
    "not_installed", "not_logged_in", "api_key_detected", "auth_expired", "unsupported",
    "quota_exhausted", "quota_paused", "sandbox_unverified", "sandbox_failed", "consent_required", "backend_error",
    "config_mismatch",
})
# Codes where one more try of the same request is worth a call.
# Format problems only (plan §8). A timeout or a failed process is not retried, because each try costs quota.
RETRYABLE_CODES = frozenset({"bad_output", "schema_violation", "empty_output"})

# Only these variables reach a CLI child. Everything else is dropped on purpose: an API key would
# silently switch billing, and an endpoint override would send the page somewhere else.
_ALLOWED_ENV = (
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "USERNAME", "USERDOMAIN", "COMPUTERNAME",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "LANG", "LC_ALL",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "CLAUDE_CODE_GIT_BASH_PATH",
)
_ALLOWED_UPPER = frozenset(name.upper() for name in _ALLOWED_ENV)
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")

_NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}


def clean_env(source: Mapping[str, str], extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Allowlist copy of the environment plus explicit extras. Never copies API keys or endpoint overrides."""
    env = {k: str(v) for k, v in source.items() if k.upper() in _ALLOWED_UPPER}
    env.update({k: str(v) for k, v in (extra or {}).items()})
    return env


def api_key_vars_present(source: Mapping[str, str]) -> list[str]:
    """Names only, never values. Used to tell the reader what the child process will not see."""
    return [name for name in API_KEY_VARS if source.get(name)]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_once(path, text: str):
    """Creates path with text unless it already exists. The text goes to a temp file first, so a reader never sees
    half a file. Parallel calls can race here: the file a winner created has the same text, so a loser can stop."""
    path = Path(path)
    if path.exists():
        return path
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        if not path.exists():  # the rename failed for another reason
            raise
    finally:
        tmp.unlink(missing_ok=True)
    return path


class BackendError(RuntimeError):
    def __init__(self, code: str, detail: str = "", *, retry_after: float | None = None):
        self.code = code
        self.detail = re.sub(r"\s+", " ", str(detail))[:200]
        self.retry_after = retry_after
        super().__init__(f"{code}: {self.detail}" if self.detail else code)

    @property
    def fatal(self) -> bool:
        return self.code in FATAL_CODES

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


@dataclass
class BackendStatus:
    backend: str
    state: str = "unchecked"  # unchecked | ready | not_installed | not_logged_in | api_key_detected | sandbox_unverified | sandbox_failed | unsupported | backend_error
    detail: str = ""
    version: str = ""
    checked_at: float = 0.0

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProcResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    seconds: float
    timed_out: bool


def kill_tree(proc) -> None:
    """Stop a CLI and whatever it started. Node-based CLIs spawn helpers, so a plain kill is not enough on Windows."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       timeout=30, check=False, **_NO_WINDOW)
    try:
        proc.kill()
    except OSError:
        pass


class ProcessRunner:
    """One CLI call: stdin in, stdout/stderr out, a hard timeout, and a cancel handle."""

    def __init__(self, popen=subprocess.Popen):
        self._popen = popen
        self._lock = threading.Lock()
        # Several subscription CLI calls may run simultaneously.
        # Keep every child so cancellation does not miss older processes.
        self._active = set()

    def run(self, argv: Sequence[str], *, stdin: bytes, cwd, env: Mapping[str, str], timeout: float) -> ProcResult:
        started = time.monotonic()
        proc = self._popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           cwd=str(cwd), env=dict(env), shell=False, **_NO_WINDOW)
        with self._lock:
            self._active.add(proc)
        timed_out = False
        try:
            out, err = proc.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc)
            out, err = proc.communicate()
        finally:
            with self._lock:
                self._active.discard(proc)
        return ProcResult(proc.returncode, out or b"", err or b"", round(time.monotonic() - started, 2), timed_out)

    def cancel(self) -> None:
        with self._lock:
            procs = list(self._active)
        for proc in procs:
            kill_tree(proc)


_AUTH_EXPIRED = re.compile(r"login expired|login has expired|token expired|expired login", re.I)
_NOT_LOGGED_IN = re.compile(r"not logged in|please run /login|run /login|log in to continue|no credentials", re.I)
_QUOTA = re.compile(r"usage limit|rate[ -]?limit|limit reached|too many requests|quota|hit your limit|"
                    r"weekly limit|5-hour limit", re.I)
_RETRY_IN = re.compile(r"(?:try again|retry|resets?)\s+(?:in|after)\s+(\d+)\s*(second|minute|hour|day)s?", re.I)
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def classify_failure(text: str) -> BackendError | None:
    """Map a CLI's failure text to a backend error code. Unknown text returns None (caller decides)."""
    if not text:
        return None
    if _AUTH_EXPIRED.search(text):
        return BackendError("auth_expired", text)
    if _QUOTA.search(text):
        m = _RETRY_IN.search(text)
        retry = int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()] if m else None
        return BackendError("quota_exhausted", text, retry_after=retry)
    if _NOT_LOGGED_IN.search(text):
        return BackendError("not_logged_in", text)
    return None


def parse_json_loose(text) -> object | None:
    """JSON from a model reply: tolerates code fences and a little prose around the object."""
    if not isinstance(text, str):
        return None
    body = text.strip()
    body = re.sub(r"^```(?:json)?\s*", "", body)
    body = re.sub(r"\s*```$", "", body)
    try:
        return json.loads(body)
    except ValueError:
        start, end = body.find("{"), body.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(body[start:end + 1])
            except ValueError:
                return None
        return None


def normalize_usage(usage) -> dict:
    """Input and output token counts across both CLIs' field names. Cached reads are reported separately."""
    if not isinstance(usage, dict):
        return {"input": 0, "output": 0, "cached": 0}

    def num(key):
        try:
            return int(usage.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "input": num("input_tokens") + num("cache_creation_input_tokens"),
        "output": num("output_tokens") + num("reasoning_output_tokens"),
        "cached": num("cache_read_input_tokens") + num("cached_input_tokens"),
    }
