"""Subprocess tests: the real adapters start the fake claude and codex scripts in tests/fixtures.

Nothing here reaches the network or a real subscription. The fakes read their behaviour from --mode and
append each call to --log, so the tests can check exactly what the CLI received.
"""
import json
import os
import sys
from pathlib import Path

import pytest

from balloonlens.backends.base import BackendError
from balloonlens.backends.claude_cli import ClaudeCodeBackend
from balloonlens.backends.codex_cli import CodexBackend
from balloonlens.backends.page import CliTranslator
from balloonlens.backends.prompts import PAGE_SCHEMA_JSON, build_payload, encode_payload
from balloonlens.cache import Cache

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"
FAKE_CODEX = FIXTURES / "fake_codex.py"
SECRET = "sk-ant-api03-SHOULDNOTLEAK"


def base_env():
    """What the helper's own environment looks like: the API keys are present and must not reach the CLI."""
    keep = {k: v for k, v in os.environ.items()
            if k.upper() in ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "USERPROFILE",
                             "PATHEXT", "COMSPEC")}
    return {**keep, "ANTHROPIC_API_KEY": SECRET, "OPENAI_API_KEY": SECRET}


def claude(tmp_path, mode, log=None, timeout=30):
    command = [sys.executable, str(FAKE_CLAUDE), "--mode", mode] + (["--log", str(log)] if log else [])
    return ClaudeCodeBackend(command=command, config_dir=tmp_path / "cfg", sandbox_dir=tmp_path / "sb",
                             runtime_dir=tmp_path / "rt", timeout=timeout, environ=base_env())


def codex(tmp_path, mode, log=None, timeout=30, verified=True):
    command = [sys.executable, str(FAKE_CODEX), "--mode", mode] + (["--log", str(log)] if log else [])
    return CodexBackend(command=command, sandbox_dir=tmp_path / "sb", runtime_dir=tmp_path / "rt",
                        timeout=timeout, sandbox_verified=verified, environ=base_env())


def payload_for(lines):
    return encode_payload(build_payload("ja", "ko", lines, [sid for sid, _ in lines]))


def read_log(path):
    return [json.loads(row) for row in Path(path).read_text(encoding="utf-8").splitlines()]


# ---------- Claude Code ----------

def test_claude_round_trip_keeps_dialogue_out_of_argv_and_keys_out_of_env(tmp_path):
    log = tmp_path / "claude.jsonl"
    payload = payload_for([(1, "待って"), (2, "こんにちは")])
    out = claude(tmp_path, "ok", log).complete_json("system words", payload, model="claude-haiku-5-5",
                                                    schema_json=PAGE_SCHEMA_JSON)
    assert [item["text"] for item in out["data"]["items"]] == ["번역문", "번역문"]
    (call,) = read_log(log)
    assert "待って" not in " ".join(call["argv"])
    assert "ANTHROPIC_API_KEY" not in call["env"] and "OPENAI_API_KEY" not in call["env"]
    assert call["stdin"] == payload.decode("utf-8")
    assert out["usage"] == {"input": 300, "output": 60, "cached": 120}


def test_claude_preflight_through_the_fake_cli_is_ready(tmp_path):
    st = claude(tmp_path, "ok").preflight()
    assert st.state == "ready" and st.version == "2.1.285 (Claude Code)"


@pytest.mark.parametrize("mode, code", [
    ("auth", "not_logged_in"),
    ("not_logged_in_json", "not_logged_in"),
    ("quota", "quota_exhausted"),
    ("bad", "bad_output"),
    ("schema_violation", "schema_violation"),
    ("error_json", "process_failed"),
])
def test_claude_failure_modes_from_the_cli_are_classified(tmp_path, mode, code):
    with pytest.raises(BackendError) as info:
        claude(tmp_path, mode).complete_json("s", payload_for([(1, "待って")]))
    assert info.value.code == code
    if mode == "quota":
        assert info.value.retry_after == 7200


def test_claude_timeout_is_reported_and_the_child_is_stopped(tmp_path):
    with pytest.raises(BackendError) as info:
        claude(tmp_path, "timeout", timeout=1).complete_json("s", payload_for([(1, "待って")]))
    assert info.value.code == "timeout"


def claude_page(tmp_path, mode, log=None):
    return CliTranslator(claude(tmp_path, mode, log), Cache(tmp_path / "c.db"))


def run(translator, page):
    got = {}
    meta = translator.translate_page(page, "ja", "ko", "claude-haiku-5-5", "scope",
                                     lambda sid, text, status: got.__setitem__(sid, (text, status)))
    return got, meta


def test_page_with_a_dropped_reply_fails_only_that_line(tmp_path):
    got, meta = run(claude_page(tmp_path, "missing"), [(1, "待って"), (2, "こんにちは"), (3, "さようなら")])
    assert got[1][1] == "done" and got[2][1] == "done"
    assert got[3] == (None, "failed")
    assert meta["calls"] == 2


def test_secret_in_a_reply_is_dropped_and_other_lines_stand(tmp_path):
    got, meta = run(claude_page(tmp_path, "secret"), [(1, "待って"), (2, "こんにちは")])
    assert got[1] == (None, "failed") and got[2][1] == "done"
    assert all("sk-ant" not in str(text) for text, _ in got.values())
    assert any("secret_pattern" in q["reasons"] for q in meta["quality_errors"])


def test_link_from_the_model_is_marked_for_review(tmp_path):
    got, meta = run(claude_page(tmp_path, "foreign_url"), [(1, "待って"), (2, "こんにちは")])
    assert got[1][1] == "review" and got[2][1] == "done"
    assert any("foreign_url" in q["reasons"] for q in meta["quality_errors"])


# ---------- Codex ----------

def test_codex_round_trip_is_read_only_and_keeps_dialogue_off_argv(tmp_path):
    log = tmp_path / "codex.jsonl"
    out = codex(tmp_path, "ok", log).complete_json("system words", payload_for([(1, "待って")]), model="gpt-6-luna")
    assert out["data"]["items"][0]["text"] == "번역문"
    (call,) = read_log(log)
    argv = call["argv"]
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-m") + 1] == "gpt-6-luna"
    assert "待って" not in " ".join(argv)
    assert "<DATA>" in call["stdin"]
    assert "OPENAI_API_KEY" not in call["env"]
    assert out["usage"] == {"input": 500, "output": 110, "cached": 200}


def test_codex_preflight_through_the_fake_cli(tmp_path):
    assert codex(tmp_path, "ok").preflight().state == "ready"
    assert codex(tmp_path, "not_logged_in").preflight().state == "not_logged_in"


@pytest.mark.parametrize("mode, code, retry", [
    ("tool", "tool_attempt", None),
    ("quota", "quota_exhausted", 10800),
    ("auth_expired", "auth_expired", None),
    ("timeout", "timeout", None),
])
def test_codex_failure_modes_from_the_cli_are_classified(tmp_path, mode, code, retry):
    with pytest.raises(BackendError) as info:
        codex(tmp_path, mode, timeout=1).complete_json("s", payload_for([(1, "待って")]))
    assert info.value.code == code and info.value.retry_after == retry


@pytest.mark.parametrize("mode, verdict", [
    ("probe_readable", "file_readable"),
    ("probe_tool", "read_blocked"),
    ("ok", "inconclusive"),
])
def test_sandbox_probe_through_the_fake_codex(tmp_path, mode, verdict):
    canary_dir = tmp_path / "outside"
    result = codex(tmp_path, mode, verified=False).sandbox_probe(canary_dir)
    assert result["verdict"] == verdict
    assert result["passed"] is (verdict == "read_blocked")
    assert list(canary_dir.glob("canary-*.txt")) == []


# ---------- Codex: login mode and the local sandbox check (no model call) ----------

@pytest.mark.parametrize("mode, state", [
    ("api_key_login", "api_key_detected"),
    ("unknown_login", "unsupported"),
])
def test_codex_login_modes_other_than_chatgpt_are_refused(tmp_path, mode, state):
    assert codex(tmp_path, mode).preflight().state == state


@pytest.mark.parametrize("mode, verdict, passed", [
    ("sandbox_readable", "file_readable", False),
    ("sandbox_blocked", "read_blocked", True),
    ("sandbox_missing", "sandbox_not_started", False),
    ("sandbox_fails", "sandbox_not_started", False),
])
def test_local_sandbox_check_through_the_fake_cli(tmp_path, mode, verdict, passed):
    working, profile = tmp_path / "work", tmp_path / "profile"
    result = codex(tmp_path, mode, verified=False).local_sandbox_check(working, profile)
    assert (result["verdict"], result["passed"]) == (verdict, passed)
    assert result["method"] == "local_sandbox_runner"
    assert list(working.glob("pos-*.txt")) == [] and list(profile.glob("canary-*.txt")) == []


def test_a_failed_sandbox_check_keeps_codex_closed_even_with_a_good_login(tmp_path):
    backend = codex(tmp_path, "ok", verified=False)
    backend.set_sandbox_result("file_readable")
    st = backend.preflight()
    assert st.state == "sandbox_failed" and "읽을 수 있어요" in st.detail
    with pytest.raises(BackendError) as info:
        backend.complete_json("s", payload_for([(1, "待って")]))
    assert info.value.code == "sandbox_unverified"
