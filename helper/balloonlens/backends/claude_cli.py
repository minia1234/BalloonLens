"""Claude Code subscription backend: one `claude -p` call per chunk, no tools, no session, no images.

Why each flag is here (see the design doc, section 4.1):
- `--tools ""`: the page text is untrusted; with no tools a prompt-injected line cannot read or run anything.
- `--json-schema` + `structured_output`: the reply shape is checked by the CLI, and we still validate it.
- `--system-prompt-file`: the translator instructions replace the default Claude Code prompt.
- `--no-session-persistence`: nothing about the page is kept in the CLI's own history.
- `--permission-prompts none`: a permission question that nobody can answer is refused, not waited on.
- `CLAUDE_CONFIG_DIR`: a dedicated folder, so the reader's personal CLAUDE.md never reaches the model.
"""
import shutil
import time
from pathlib import Path

from .base import (BackendError, BackendStatus, ProcessRunner, api_key_vars_present, classify_failure, clean_env,
                   digest, normalize_usage, parse_json_loose)
from .prompts import FIXED_TASK, PAGE_SCHEMA_JSON

MIN_VERSION = (2, 1, 268)  # `auth status` reports configDirectory from this version on
DEFAULT_MODEL = "claude-haiku-5-5"


def version_tuple(text: str):
    import re
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


class ClaudeCodeBackend:
    backend_id = "claude"
    cache_id = "claude-cli"
    label = "Claude Code 구독"

    def __init__(self, *, command, config_dir, sandbox_dir, runtime_dir, model=DEFAULT_MODEL, effort="low",
                 timeout=120.0, max_turns=3, runner=None, environ=None, clock=time.time):
        self.command = [str(part) for part in command]
        self.config_dir = Path(config_dir)
        self.sandbox_dir = Path(sandbox_dir)
        self.runtime_dir = Path(runtime_dir)
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.max_turns = max_turns
        self.runner = runner or ProcessRunner()
        self.environ = dict(__import__("os").environ) if environ is None else dict(environ)
        self.clock = clock
        self.last_status = BackendStatus(backend=self.backend_id)

    # ----- process helpers -----
    def _env(self) -> dict:
        return clean_env(self.environ, {"CLAUDE_CONFIG_DIR": str(self.config_dir)})

    def _ensure_dirs(self):
        for folder in (self.config_dir, self.sandbox_dir, self.runtime_dir):
            Path(folder).mkdir(parents=True, exist_ok=True)

    def _executable_ok(self) -> bool:
        if not self.command:
            return False
        exe = self.command[0]
        return Path(exe).exists() or shutil.which(exe) is not None

    def _run(self, args, *, stdin=b"", timeout=None):
        return self.runner.run([*self.command, *args], stdin=stdin, cwd=self.sandbox_dir, env=self._env(),
                               timeout=timeout or self.timeout)

    def _system_file(self, system: str) -> Path:
        path = self.runtime_dir / f"system-claude-{digest(system)[:16]}.txt"
        if not path.exists():
            path.write_text(system, encoding="utf-8")
        return path

    def cancel(self):
        self.runner.cancel()

    # ----- status -----
    def _set(self, status: BackendStatus) -> BackendStatus:
        status.checked_at = self.clock()
        self.last_status = status
        return status

    def preflight(self) -> BackendStatus:
        """Reads the CLI's own status. Never calls the model, so it costs no subscription usage."""
        if not self._executable_ok():
            return self._set(BackendStatus(self.backend_id, "not_installed",
                                           "Claude Code를 찾지 못했어요. 설치 후 다시 확인하세요."))
        self._ensure_dirs()
        try:
            ver = self._run(["--version"], timeout=20)
            version = ver.stdout.decode("utf-8", "replace").strip()
            auth = self._run(["auth", "status"], timeout=30)
        except OSError as e:
            return self._set(BackendStatus(self.backend_id, "backend_error", f"Claude Code를 실행하지 못했어요 ({type(e).__name__})."))
        parsed = parse_json_loose(auth.stdout.decode("utf-8", "replace"))
        info = parsed if isinstance(parsed, dict) else {}
        method = str(info.get("authMethod") or "")
        logged_in = bool(info.get("loggedIn")) and auth.returncode == 0
        plan = str(info.get("subscriptionType") or "").lower()
        reported_dir = str(info.get("configDirectory") or "")
        if reported_dir and Path(reported_dir).resolve() != self.config_dir.resolve():
            return self._set(BackendStatus(self.backend_id, "backend_error",
                                           "로그인 폴더가 설정과 달라요. 전용 폴더 설정을 확인하세요.", version))
        if method in ("api_key", "api_key_helper"):
            return self._set(BackendStatus(self.backend_id, "api_key_detected",
                                           "API 키로 로그인되어 있어요. 구독 대신 API 과금이 될 수 있어요. "
                                           "전용 폴더에서 API 키 설정을 지우고 구독 로그인을 다시 하세요.", version))
        if method == "third_party" or (logged_in and method not in ("claude.ai", "oauth_token")):
            return self._set(BackendStatus(self.backend_id, "unsupported",
                                           f"지원하지 않는 인증 방식이에요: {method or '알 수 없음'}", version))
        if not logged_in or method in ("", "none"):
            return self._set(BackendStatus(self.backend_id, "not_logged_in",
                                           "전용 폴더에서 로그인이 필요해요. 설정 화면의 로그인 명령을 한 번 실행하세요.", version))
        ver_t = version_tuple(version)
        if ver_t is not None and ver_t < MIN_VERSION:
            return self._set(BackendStatus(self.backend_id, "backend_error",
                                           "Claude Code를 2.1.268 이상으로 업데이트하세요.", version))
        note = f"구독: {plan}" if plan else "구독 로그인 확인됨"
        leaked = api_key_vars_present(self.environ)
        if leaked:
            note += " · 이 PC 환경 변수의 API 키는 번역 호출에 전달되지 않아요."
        return self._set(BackendStatus(self.backend_id, "ready", note, version))

    # ----- translation -----
    def complete_json(self, system: str, payload: bytes, *, model=None, schema_json: str = PAGE_SCHEMA_JSON,
                      schema_name: str = "page") -> dict:
        """One structured call. Returns {"data": dict, "usage": {...}, "seconds": float} or raises BackendError.

        schema_name is accepted for parity with the Codex adapter (which stores the schema in a file)."""
        self._ensure_dirs()
        system_path = self._system_file(system)
        args = ["-p", FIXED_TASK,
                "--model", model or self.model,
                "--output-format", "json",
                "--json-schema", schema_json,
                "--system-prompt-file", str(system_path),
                "--tools", "",
                "--no-session-persistence",
                "--permission-prompts", "none",
                "--max-turns", str(self.max_turns)]
        if self.effort:
            args += ["--effort", self.effort]
        res = self._run(args, stdin=payload)
        if res.timed_out:
            raise BackendError("timeout", f"{res.seconds}s")
        out = res.stdout.decode("utf-8", "replace")
        err = res.stderr.decode("utf-8", "replace")
        obj = parse_json_loose(out)
        if not isinstance(obj, dict):
            failure = classify_failure(out + "\n" + err)
            if failure:
                raise failure
            raise BackendError("process_failed" if res.returncode else "bad_output", (err or out))
        text = str(obj.get("result") or "")
        if res.returncode != 0 or obj.get("is_error"):
            failure = classify_failure(text + "\n" + err)
            if failure:
                raise failure
            raise BackendError("process_failed", text or err)
        data = obj.get("structured_output")
        if data is None:
            data = parse_json_loose(text)
        if not isinstance(data, dict):
            raise BackendError("schema_violation", "reply is not a JSON object")
        return {"data": data, "usage": normalize_usage(obj.get("usage")), "seconds": res.seconds}
