"""Codex subscription backend (gpt-6-luna): one `codex exec` call per chunk, read-only sandbox.

Codex's read-only sandbox is the only fence: there is no switch that removes its shell tool. So Codex stays off
until `local_sandbox_check` shows that the sandbox can read a file in its own folder and is refused a file in the
reader's profile. That check runs the same Windows sandbox runner without any model call, so it uses no
subscription usage. Any tool event in a model reply is treated as an attack attempt and the reply is discarded.

Test PC, 2026-10-10: the read-only sandbox could read a file in the profile, so Codex stays off (sandbox_failed).
"""
import json
import shutil
import time
import uuid
from pathlib import Path

from .base import (BackendError, BackendStatus, ProcessRunner, api_key_vars_present, classify_failure, clean_env,
                   normalize_usage, parse_json_loose)
from .prompts import PAGE_SCHEMA_JSON, PROBE_SCHEMA

DEFAULT_MODEL = "gpt-6-luna"
SANDBOX_POLICY = "read-only"  # the same value `codex exec -s read-only` uses
CODEX_TASK = ("Follow the instructions at the top of stdin. The data between <DATA> and </DATA> is a JSON object. "
              "Return only the JSON result.")
PROBE_TASK = "Follow the instructions in the JSON data on stdin. Return only the JSON result."
# Item types that are plain conversation. Anything else (commands, file changes, tool calls) counts as tool use.
_TEXT_ITEM_TYPES = frozenset({"agent_message", "reasoning", "message", "assistant_message", "user_message", "error"})
# Only an explicit refusal counts as a block. Any other failure (missing path, sandbox not started) is not a pass.
_DENIED = ("access is denied", "액세스가 거부", "permission denied")
SANDBOX_FAILED_DETAIL = {
    "file_readable": "이 PC의 Codex 읽기 전용 샌드박스가 사용자 폴더의 파일을 읽을 수 있어요. 안전하지 않아서 Codex 번역을 막아 두었어요.",
    "sandbox_not_started": "Codex 샌드박스를 시작하지 못했어요. 작업 폴더와 Codex 설치를 확인한 뒤 다시 점검하세요.",
}


def parse_events(stdout: bytes) -> list[dict]:
    events = []
    for line in stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def is_tool_event(event: dict) -> bool:
    if not str(event.get("type") or "").startswith("item."):
        return False
    item = event.get("item") or {}
    return str(item.get("type") or "") not in _TEXT_ITEM_TYPES


def _item_types(events: list[dict]) -> list[str]:
    return sorted({str((e.get("item") or {}).get("type") or "") for e in events if str(e.get("type") or "").startswith("item.")})


def _text(res) -> str:
    return (res.stdout + res.stderr).decode("utf-8", "replace")


class CodexBackend:
    backend_id = "codex"
    cache_id = "codex-cli"
    label = "Codex 구독 (luna)"

    def __init__(self, *, command, sandbox_dir, runtime_dir, model=DEFAULT_MODEL, reasoning="low", timeout=180.0,
                 sandbox_verified=False, sandbox_verdict="", runner=None, environ=None, clock=time.time):
        self.command = [str(part) for part in command]
        self.sandbox_dir = Path(sandbox_dir)
        self.runtime_dir = Path(runtime_dir)
        self.model = model
        self.reasoning = reasoning
        self.timeout = timeout
        self.sandbox_verdict = ""
        self.sandbox_verified = False
        self.set_sandbox_result(sandbox_verdict or ("read_blocked" if sandbox_verified else ""))
        self.runner = runner or ProcessRunner()
        self.environ = dict(__import__("os").environ) if environ is None else dict(environ)
        self.clock = clock
        self.last_status = BackendStatus(backend=self.backend_id)
        self.last_item_types: list[str] = []  # type names only, for diagnostics; never item text

    # ----- process helpers -----
    def _env(self) -> dict:
        return clean_env(self.environ, {})

    def _ensure_dirs(self):
        for folder in (self.sandbox_dir, self.runtime_dir):
            Path(folder).mkdir(parents=True, exist_ok=True)

    def _executable_ok(self) -> bool:
        if not self.command:
            return False
        exe = self.command[0]
        return Path(exe).exists() or shutil.which(exe) is not None

    def _run(self, args, *, stdin=b"", timeout=None):
        return self.runner.run([*self.command, *args], stdin=stdin, cwd=self.sandbox_dir, env=self._env(),
                               timeout=timeout or self.timeout)

    def _schema_file(self, name: str, text: str) -> Path:
        path = self.runtime_dir / f"{name}-schema.json"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
        return path

    def cancel(self):
        self.runner.cancel()

    def set_sandbox_result(self, verdict: str):
        """Stores the last sandbox verdict. Only "read_blocked" lets translation run."""
        self.sandbox_verdict = verdict or ""
        self.sandbox_verified = self.sandbox_verdict == "read_blocked"

    def set_sandbox_verified(self, value: bool):
        self.set_sandbox_result("read_blocked" if value else "")

    @property
    def sandbox_failed(self) -> bool:
        return bool(self.sandbox_verdict) and self.sandbox_verdict != "read_blocked"

    # ----- status -----
    def _set(self, status: BackendStatus) -> BackendStatus:
        status.checked_at = self.clock()
        self.last_status = status
        return status

    def preflight(self) -> BackendStatus:
        """Login state from `codex login status` (no model call). Sandbox state from the last sandbox check."""
        if not self._executable_ok():
            return self._set(BackendStatus(self.backend_id, "not_installed",
                                           "Codex CLI를 찾지 못했어요. 설치 후 다시 확인하세요."))
        self._ensure_dirs()
        try:
            login = self._run(["login", "status"], timeout=30)
        except OSError as e:
            return self._set(BackendStatus(self.backend_id, "backend_error", f"Codex를 실행하지 못했어요 ({type(e).__name__})."))
        if login.returncode != 0:
            return self._set(BackendStatus(self.backend_id, "not_logged_in",
                                           "Codex에 로그인되어 있지 않아요. 터미널에서 codex login을 실행하세요."))
        # Only a ChatGPT subscription login is allowed. An API key login would bill per token, so it is refused.
        login_text = (login.stdout + login.stderr).decode("utf-8", "replace").lower()
        if "api key" in login_text or "apikey" in login_text:
            return self._set(BackendStatus(self.backend_id, "api_key_detected",
                                           "Codex가 API 키로 로그인돼 있어요. 구독 로그인(ChatGPT)으로 바꿔 주세요."))
        if "chatgpt" not in login_text:
            return self._set(BackendStatus(self.backend_id, "unsupported",
                                           "Codex 로그인 방식을 알 수 없어요. ChatGPT 구독 로그인만 쓸 수 있어요."))
        note = "ChatGPT 구독 로그인 확인됨"
        if api_key_vars_present(self.environ):
            note += " · 이 PC 환경 변수의 API 키는 번역 호출에 전달되지 않아요."
        if self.sandbox_failed:
            detail = SANDBOX_FAILED_DETAIL.get(self.sandbox_verdict, "Codex 샌드박스 점검이 통과하지 못했어요.")
            return self._set(BackendStatus(self.backend_id, "sandbox_failed", detail))
        if not self.sandbox_verified:
            return self._set(BackendStatus(self.backend_id, "sandbox_unverified",
                                           "샌드박스 보안 점검을 아직 통과하지 않았어요. 설정 화면에서 보안 점검을 실행하세요."))
        return self._set(BackendStatus(self.backend_id, "ready", note))

    # ----- translation -----
    def _argv(self, out_path: Path, schema_path: Path, model: str) -> list[str]:
        return ["exec", CODEX_TASK,
                "--ignore-user-config", "--ignore-rules",
                "-m", model,
                "-c", f'model_reasoning_effort="{self.reasoning}"',
                "-s", SANDBOX_POLICY,
                "-C", str(self.sandbox_dir),
                "--skip-git-repo-check", "--ephemeral",
                "--output-schema", str(schema_path),
                "-o", str(out_path),
                "--json"]

    def complete_json(self, system: str, payload: bytes, *, model=None, schema_json: str = PAGE_SCHEMA_JSON,
                      schema_name: str = "page") -> dict:
        if not self.sandbox_verified:
            raise BackendError("sandbox_unverified", "run the sandbox check in settings first")
        self._ensure_dirs()
        out_path = self.runtime_dir / f"codex-out-{uuid.uuid4().hex}.json"
        schema_path = self._schema_file(schema_name, schema_json)
        stdin = system.encode("utf-8") + b"\n\n<DATA>\n" + payload + b"\n</DATA>\n"
        res = self._run(self._argv(out_path, schema_path, model or self.model), stdin=stdin)
        return self._finish(res, out_path)

    def _finish(self, res, out_path: Path) -> dict:
        if res.timed_out:
            self._discard(out_path)
            raise BackendError("timeout", f"{res.seconds}s")
        events = parse_events(res.stdout)
        self.last_item_types = _item_types(events)
        err = res.stderr.decode("utf-8", "replace")
        tool_events = [e for e in events if is_tool_event(e)]
        if tool_events:
            self._discard(out_path)
            raise BackendError("tool_attempt", f"{len(tool_events)} tool events in reply")
        usage = {}
        error_texts = []
        for e in events:
            if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict):
                usage = e["usage"]
            if e.get("type") in ("error", "turn.failed"):
                error_texts.append(str(e.get("message") or e.get("error") or e))
        text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        self._discard(out_path)
        if res.returncode != 0 or (not text and error_texts):
            failure = classify_failure(" ".join(error_texts) + "\n" + err)
            if failure:
                raise failure
            raise BackendError("process_failed" if res.returncode else "empty_output", " ".join(error_texts) or err)
        data = parse_json_loose(text)
        if not isinstance(data, dict):
            raise BackendError("schema_violation", "reply is not a JSON object")
        return {"data": data, "usage": normalize_usage(usage), "seconds": res.seconds}

    @staticmethod
    def _discard(path: Path):
        try:
            path.unlink()
        except OSError:
            pass

    # ----- sandbox check without a model call (used by the settings page) -----
    def _sandbox_read(self, working: Path, target: str):
        """One command under the same Windows sandbox that `exec -s read-only` uses. No model is involved."""
        arg = f'"{target}"' if " " in target else target  # a path with spaces must stay one argument
        argv = [*self.command, "sandbox", "-c", f'sandbox_mode="{SANDBOX_POLICY}"', "--", "cmd", "/c", "type", arg]
        return self.runner.run(argv, stdin=b"", cwd=working, env=self._env(), timeout=60)

    def local_sandbox_check(self, working_dir, canary_dir) -> dict:
        """Asks the sandbox runner to read two random marker files. No model call, so no usage and no consent needed.

        Passes only when the file inside `working_dir` can be read AND the file in `canary_dir` (the reader's
        profile) is refused with an explicit access-denied message. Anything else is not a pass."""
        working, canary_dir = Path(working_dir), Path(canary_dir)
        working.mkdir(parents=True, exist_ok=True)
        canary_dir.mkdir(parents=True, exist_ok=True)
        inside = working / f"pos-{uuid.uuid4().hex}.txt"
        outside = canary_dir / f"canary-{uuid.uuid4().hex}.txt"
        inside_token, outside_token = f"POS-{uuid.uuid4().hex}", f"CANARY-{uuid.uuid4().hex}"
        inside.write_text(inside_token, encoding="utf-8")
        outside.write_text(outside_token, encoding="utf-8")
        try:
            inside_res = self._sandbox_read(working, inside.name)
            outside_res = self._sandbox_read(working, str(outside))
        finally:
            self._discard(inside)
            self._discard(outside)
        inside_text, outside_text = _text(inside_res), _text(outside_res)
        inside_ok = inside_token in inside_text
        outside_ok = outside_token in outside_text
        denied = any(marker in outside_text.lower() for marker in _DENIED)
        if outside_ok:
            verdict = "file_readable"
        elif inside_ok and denied:
            verdict = "read_blocked"
        else:
            verdict = "sandbox_not_started"
        return {"passed": verdict == "read_blocked", "verdict": verdict, "method": "local_sandbox_runner",
                "inside_readable": inside_ok, "outside_readable": outside_ok,
                "seconds": round((inside_res.seconds or 0) + (outside_res.seconds or 0), 2),
                "returncode": outside_res.returncode}

    # ----- sandbox probe through the model (diagnostic only; costs one model call) -----
    def sandbox_probe(self, canary_dir) -> dict:
        """Asks the model to read a canary file placed OUTSIDE the sandbox. The canary is a random string
        in a temp file, not a real credential. Passing means: a tool was tried and the canary never came back."""
        self._ensure_dirs()
        canary_dir = Path(canary_dir)
        canary_dir.mkdir(parents=True, exist_ok=True)
        token = f"CANARY-{uuid.uuid4().hex}"
        canary = canary_dir / f"canary-{uuid.uuid4().hex}.txt"
        canary.write_text(token, encoding="utf-8")
        out_path = self.runtime_dir / f"probe-out-{uuid.uuid4().hex}.json"
        schema_path = self._schema_file("probe", json.dumps(PROBE_SCHEMA, separators=(",", ":")))
        body = {"task": "read_file_probe", "path": str(canary),
                "instruction": "Before you answer, run exactly one shell command that prints the file at the path above. "
                               "Do not answer from memory. Put the exact text the command printed in text. "
                               "If the command fails or prints nothing, put an empty string."}
        stdin = json.dumps(body).encode("utf-8")
        args = ["exec", PROBE_TASK, "--ignore-user-config", "--ignore-rules", "-m", self.model,
                "-c", f'model_reasoning_effort="{self.reasoning}"', "-s", SANDBOX_POLICY, "-C", str(self.sandbox_dir),
                "--skip-git-repo-check", "--ephemeral", "--output-schema", str(schema_path), "-o", str(out_path), "--json"]
        try:
            res = self._run(args, stdin=stdin)
            raw = res.stdout.decode("utf-8", "replace") + res.stderr.decode("utf-8", "replace")
            events = parse_events(res.stdout)
            self.last_item_types = _item_types(events)
            attempted = any(is_tool_event(e) for e in events)
            text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
            readable = token in raw or token in text
            if readable:
                verdict, passed = "file_readable", False
            elif attempted:
                verdict, passed = "read_blocked", True
            else:
                verdict, passed = "inconclusive", False
            return {"passed": passed, "verdict": verdict, "attempted": attempted, "seconds": res.seconds,
                    "returncode": res.returncode}
        finally:
            self._discard(out_path)
            self._discard(canary)
