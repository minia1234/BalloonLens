"""Page translation through a CLI backend, and the Claude Code adapter with fake runners.

The helpers (fake runner, fake backend, reply builders) live in test_backends.py.
"""
import json
from pathlib import Path

import pytest

from balloonlens.backends.base import BackendError
from balloonlens.backends.claude_cli import ClaudeCodeBackend
from balloonlens.backends.page import CliTranslator
from balloonlens.backends.prompts import PAGE_SCHEMA_JSON
from balloonlens.backends.quota import QuotaPolicy
from balloonlens.cache import Cache

from test_backends import (FakeBackend, FakeRunner, auth_json, claude_backend, claude_result, items_reply,
                           page_payload, proc, write_empty_list)


# ---------- page translation through a CLI backend ----------

def lines(n):
    return [(i + 1, f"台詞{i}") for i in range(n)]


def run_page(translator, page, **kwargs):
    got = {}
    meta = translator.translate_page(page, "ja", "ko", "claude-haiku-5-5", "scope",
                                     lambda sid, text, status: got.__setitem__(sid, (text, status)), **kwargs)
    return got, meta


def test_short_page_is_one_call_and_a_repeat_comes_from_the_cache(tmp_path):
    backend = FakeBackend("claude")
    translator = CliTranslator(backend, Cache(tmp_path / "c.db"))
    got, meta = run_page(translator, [(1, "待って"), (2, "こんにちは")])
    assert got == {1: ("번역문", "done"), 2: ("번역문", "done")}
    assert meta["calls"] == 1 and len(backend.calls) == 1
    got2, meta2 = run_page(translator, [(1, "待って"), (2, "こんにちは")])
    assert got2 == got and meta2["cache_hits"] == 2 and len(backend.calls) == 1


def test_long_page_is_split_in_reading_order(tmp_path):
    backend = FakeBackend("claude")
    run_page(CliTranslator(backend, Cache(tmp_path / "c.db")), lines(30))
    sizes = [len(json.loads(c["payload"])["target_ids"]) for c in backend.calls]
    assert sizes == [24, 6]


def test_missing_line_gets_one_targeted_retry(tmp_path):
    state = {"n": 0}

    def handler(payload):
        state["n"] += 1
        body = json.loads(payload)
        ids = body["target_ids"][:-1] if state["n"] == 1 else body["target_ids"]
        return {"data": {"items": [{"id": s, "text": "번역문", "review": False} for s in ids]},
                "usage": {}, "seconds": 0}

    backend = FakeBackend("claude", handler)
    got, meta = run_page(CliTranslator(backend, Cache(tmp_path / "c.db")), [(1, "待って"), (2, "こんにちは")])
    assert got == {1: ("번역문", "done"), 2: ("번역문", "done")}
    assert meta["calls"] == 2


def test_secret_looking_reply_is_never_shown_or_cached(tmp_path):
    secret = "sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE"

    def handler(payload):
        ids = json.loads(payload)["target_ids"]
        items = [{"id": s, "text": secret if s == 1 else "번역문", "review": False} for s in ids]
        return {"data": {"items": items}, "usage": {}, "seconds": 0}

    backend = FakeBackend("claude", handler)
    translator = CliTranslator(backend, Cache(tmp_path / "c.db"))
    got, meta = run_page(translator, [(1, "待って"), (2, "こんにちは")])
    assert got[1] == (None, "failed") and got[2] == ("번역문", "done")
    assert "secret_pattern" in meta["quality_errors"][0]["reasons"]
    # Line 2 is cached; line 1 is asked again on the next run because nothing was stored for it.
    run_page(translator, [(1, "待って"), (2, "こんにちは")])
    assert len(backend.calls) == 2
    assert json.loads(backend.calls[1]["payload"])["target_ids"] == [1]


def test_fatal_error_stops_after_the_failing_chunk(tmp_path):
    def handler(payload):
        raise BackendError("not_logged_in", "login needed")

    backend = FakeBackend("claude", handler)
    got, meta = run_page(CliTranslator(backend, Cache(tmp_path / "c.db")), lines(30))
    assert meta["fatal"] == "not_logged_in"
    assert len(backend.calls) == 1
    assert len(got) == 30 and all(status == "failed" for _, status in got.values())


def test_timeout_is_not_retried(tmp_path):
    """A timeout costs a full wait and quota, so only format problems get the one retry (plan §8)."""
    state = {"n": 0}

    def handler(payload):
        state["n"] += 1
        raise BackendError("timeout", "150s")

    backend = FakeBackend("claude", handler)
    _, meta = run_page(CliTranslator(backend, Cache(tmp_path / "c.db")), [(1, "待って")])
    assert state["n"] == 1 and meta["network_errors"] == ["timeout"]


def test_no_image_bytes_reach_a_subscription_call(tmp_path):
    backend = FakeBackend("claude")
    _, meta = run_page(CliTranslator(backend, Cache(tmp_path / "c.db")), [(1, "待って")], image=b"\x89PNG-bytes")
    assert meta["image_ignored"] is True
    body = json.loads(backend.calls[0]["payload"])
    assert "image" not in body and b"base64" not in backend.calls[0]["payload"]


def test_review_all_adds_one_proofread_pass_and_off_adds_none(tmp_path):
    backend = FakeBackend("claude", items_reply())
    got, meta = run_page(CliTranslator(backend, Cache(tmp_path / "c.db")), [(1, "待って")], review="all")
    assert got == {1: ("교정문", "done")} and meta["proofread_calls"] == 1 and len(backend.calls) == 2
    backend_off = FakeBackend("claude", items_reply())
    run_page(CliTranslator(backend_off, Cache(tmp_path / "c2.db")), [(1, "待って")])
    assert len(backend_off.calls) == 1


def test_selective_review_proofreads_only_the_risky_line(tmp_path):
    backend = FakeBackend("claude", items_reply())
    run_page(CliTranslator(backend, Cache(tmp_path / "c.db")), [(1, "待って"), (2, "3人いる")], review="selective")
    assert len(backend.calls) == 2
    assert json.loads(backend.calls[1]["payload"])["target_ids"] == [2]


def test_fixed_lines_are_not_sent_for_translation(tmp_path):
    backend = FakeBackend("claude")
    got, _ = run_page(CliTranslator(backend, Cache(tmp_path / "c.db")), [(1, "待って"), (2, "こんにちは")],
                      fixed={1: "고정문"})
    assert got[1] == ("고정문", "done")
    assert json.loads(backend.calls[0]["payload"])["target_ids"] == [2]


def test_cancelled_page_makes_no_calls(tmp_path):
    backend = FakeBackend("claude")
    run_page(CliTranslator(backend, Cache(tmp_path / "c.db")), lines(5), should_cancel=lambda: True)
    assert backend.calls == []


# ---------- Claude Code adapter (fake runner) ----------

def claude_preflight_runner(auth_stdout, *, version="2.1.285 (Claude Code)", auth_code=0):
    def respond(argv, stdin):
        if argv[1:] == ["--version"]:
            return proc(version + "\n")
        return proc(auth_stdout, code=auth_code)
    return FakeRunner(respond)


def test_claude_preflight_ready_with_subscription(tmp_path):
    runner = claude_preflight_runner(auth_json(configDirectory=str(tmp_path / "cfg")))
    st = claude_backend(tmp_path, runner).preflight()
    assert st.state == "ready" and st.version == "2.1.285 (Claude Code)" and "pro" in st.detail


@pytest.mark.parametrize("auth_stdout, auth_code, version, state", [
    (auth_json(loggedIn=False, authMethod="none"), 1, "2.1.285 (Claude Code)", "not_logged_in"),
    (auth_json(authMethod="api_key"), 0, "2.1.285 (Claude Code)", "api_key_detected"),
    (auth_json(authMethod="third_party"), 0, "2.1.285 (Claude Code)", "unsupported"),
    (auth_json(configDirectory="C:/elsewhere"), 0, "2.1.285 (Claude Code)", "backend_error"),
    (auth_json(), 0, "2.0.1 (Claude Code)", "backend_error"),
    ("status: ok", 0, "2.1.285 (Claude Code)", "not_logged_in"),
])
def test_claude_preflight_refuses_unsafe_or_unknown_states(tmp_path, auth_stdout, auth_code, version, state):
    runner = claude_preflight_runner(auth_stdout, version=version, auth_code=auth_code)
    assert claude_backend(tmp_path, runner).preflight().state == state


def test_claude_preflight_reports_missing_executable(tmp_path):
    backend = ClaudeCodeBackend(command=[str(tmp_path / "no-such-claude.exe")], config_dir=tmp_path / "cfg",
                                sandbox_dir=tmp_path / "sb", runtime_dir=tmp_path / "rt", clock=lambda: 1.0)
    assert backend.preflight().state == "not_installed"


def test_claude_argv_has_fixed_safety_flags_and_no_dialogue(tmp_path):
    payload = page_payload([(1, "待って")])
    runner = FakeRunner(lambda argv, stdin: claude_result([{"id": 1, "text": "잠깐만", "review": False}]))
    out = claude_backend(tmp_path, runner).complete_json("system words", payload, model="claude-haiku-5-5",
                                                         schema_json=PAGE_SCHEMA_JSON)
    call = runner.calls[0]
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert argv[argv.index("--max-turns") + 1] == "3"
    for flag in ("--no-session-persistence", "--json-schema", "--system-prompt-file", "--output-format", "--effort"):
        assert flag in argv
    assert "待って" not in " ".join(argv)
    assert Path(argv[argv.index("--system-prompt-file") + 1]).read_text(encoding="utf-8") == "system words"
    assert call["stdin"] == payload
    assert call["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert out["data"] == {"items": [{"id": 1, "text": "잠깐만", "review": False}]}
    assert out["usage"] == {"input": 300, "output": 40, "cached": 100}


def test_translation_sends_no_schema_flag_and_suggestions_still_do(tmp_path):
    """A schema makes the CLI spend a second model turn on every translation, so translation leaves it out."""
    payload = page_payload([(1, "待って")])
    runner = FakeRunner(lambda argv, stdin: claude_result([{"id": 1, "text": "잠깐만", "review": False}]))
    backend = claude_backend(tmp_path, runner)
    backend.complete_json("system words", payload, model="claude-haiku-5-5")
    assert "--json-schema" not in runner.calls[0]["argv"]
    backend.complete_json("system words", payload, model="claude-haiku-5-5", schema_json=PAGE_SCHEMA_JSON)
    assert "--json-schema" in runner.calls[1]["argv"]


def test_claude_reads_result_text_when_structured_output_is_absent(tmp_path):
    runner = FakeRunner(lambda argv, stdin: claude_result([{"id": 1, "text": "잠깐", "review": False}],
                                                          structured=False))
    out = claude_backend(tmp_path, runner).complete_json("s", b"{}")
    assert out["data"]["items"][0]["text"] == "잠깐"


@pytest.mark.parametrize("res, code, retry", [
    (proc(stderr="Please run /login", code=1), "not_logged_in", None),
    (proc(json.dumps({"is_error": True, "result": "Claude usage limit reached. Your limit will reset in 2 hours."}),
          code=1), "quota_exhausted", 7200),
    (proc(timed_out=True), "timeout", None),
    (proc("garbage"), "bad_output", None),
    (proc(json.dumps({"is_error": False, "structured_output": []})), "schema_violation", None),
])
def test_claude_failures_map_to_codes(tmp_path, res, code, retry):
    backend = claude_backend(tmp_path, FakeRunner(lambda argv, stdin: res))
    with pytest.raises(BackendError) as info:
        backend.complete_json("s", b"{}")
    assert info.value.code == code and info.value.retry_after == retry


def test_cap_reached_mid_page_stops_the_next_chunk_and_the_blocked_call_is_not_counted(tmp_path):
    cache = Cache(tmp_path / "c.db")
    quota = QuotaPolicy(cache)
    quota.set_caps("claude", 1, 10)
    backend = FakeBackend("claude")
    digit_free = [(i + 1, "台詞" + "あ" * (i % 5 + 1)) for i in range(30)]  # digits would trigger a number check
    got, meta = run_page(CliTranslator(backend, cache, quota), digit_free)
    assert len(backend.calls) == 1
    assert meta["fatal"] == "quota_paused"
    statuses = [status for _, status in got.values()]
    assert statuses.count("done") == 24 and statuses.count("failed") == 6
    assert quota.usage("claude")["calls_5h"] == 1
