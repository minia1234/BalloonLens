"""Unit tests for the subscription backends. No CLI process runs here: runners and backends are fakes."""
import json
import sys
import time
from pathlib import Path

import pytest

from balloonlens import config
from balloonlens.backends import validate
from balloonlens.backends.base import (BackendError, BackendStatus, ProcResult, api_key_vars_present,
                                       classify_failure, clean_env, normalize_usage, parse_json_loose)
from balloonlens.backends.claude_cli import ClaudeCodeBackend
from balloonlens.backends.codex_cli import CodexBackend, is_tool_event, parse_events
from balloonlens.backends.page import CliTranslator
from balloonlens.backends.prompts import PAGE_SCHEMA_JSON, build_payload, chunk_ids, encode_payload, system_prompt
from balloonlens.backends.quota import QuotaPolicy
from balloonlens.backends.router import Router
from balloonlens.cache import Cache


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def proc(stdout="", stderr="", code=0, timed_out=False):
    return ProcResult(code, stdout.encode("utf-8"), stderr.encode("utf-8"), 0.1, timed_out)


class FakeRunner:
    """Answers each CLI call with respond(argv, stdin) and keeps every call for inspection."""

    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def run(self, argv, *, stdin, cwd, env, timeout):
        self.calls.append({"argv": list(argv), "stdin": stdin, "env": dict(env), "timeout": timeout})
        return self.respond(list(argv), stdin)


def items_reply(text="번역문", proofread_text="교정문"):
    def handler(payload):
        body = json.loads(payload)
        value = proofread_text if body.get("drafts") else text
        items = [{"id": sid, "text": value, "review": False} for sid in body["target_ids"]]
        return {"data": {"items": items}, "usage": {"input": 10, "output": 5, "cached": 0}, "seconds": 0.01}
    return handler


class FakeBackend:
    """Stands in for a subscription adapter when only the page or router logic is under test."""

    def __init__(self, backend_id, handler=None, *, state="ready"):
        self.backend_id = backend_id
        self.cache_id = f"{backend_id}-cli"
        self.label = backend_id
        self.model = config.CLAUDE_MODEL if backend_id == "claude" else config.CODEX_MODEL
        self.handler = handler or items_reply()
        self.state = state
        self.calls = []
        self.sandbox_verified = True

    def preflight(self):
        return BackendStatus(self.backend_id, self.state, "fake")

    def complete_json(self, system, payload, *, model=None, schema_json=None, schema_name="page"):
        self.calls.append({"payload": payload, "model": model, "system": system})
        return self.handler(payload)


def auth_json(**overrides):
    info = {"loggedIn": True, "authMethod": "claude.ai", "configDirectory": "", "subscriptionType": "pro"}
    info.update(overrides)
    return json.dumps(info)


def claude_result(items, *, structured=True):
    usage = {"input_tokens": 300, "cache_read_input_tokens": 100, "output_tokens": 40}
    if structured:
        out = {"type": "result", "is_error": False, "result": "", "structured_output": {"items": items},
               "usage": usage}
    else:
        out = {"type": "result", "is_error": False, "result": json.dumps({"items": items}), "usage": usage}
    return proc(json.dumps(out))


def claude_backend(tmp_path, runner, environ=None):
    return ClaudeCodeBackend(command=[sys.executable], config_dir=tmp_path / "cfg", sandbox_dir=tmp_path / "sb",
                             runtime_dir=tmp_path / "rt", runner=runner, clock=lambda: 100.0,
                             environ=environ or {"PATH": "x", "ANTHROPIC_API_KEY": "sk-ant-api03-LEAKLEAKLEAK"})


def codex_backend(tmp_path, runner, verified=True):
    return CodexBackend(command=[sys.executable], sandbox_dir=tmp_path / "sb", runtime_dir=tmp_path / "rt",
                        runner=runner, clock=lambda: 100.0, sandbox_verified=verified,
                        environ={"PATH": "x", "OPENAI_API_KEY": "sk-LEAKLEAKLEAK"})


def codex_reply(items_json, events=()):
    """Writes the -o file and prints JSONL the way codex exec --json does."""
    def respond(argv, stdin):
        Path(argv[argv.index("-o") + 1]).write_text(items_json, encoding="utf-8")
        lines = [json.dumps(e) for e in events]
        lines.append(json.dumps({"type": "turn.completed",
                                 "usage": {"input_tokens": 9, "cached_input_tokens": 2, "output_tokens": 4}}))
        return proc("\n".join(lines) + "\n")
    return respond


def write_empty_list(argv, stdin):
    Path(argv[argv.index("-o") + 1]).write_text("[]", encoding="utf-8")
    return proc("")


def probe_respond(mode):
    """mode: readable (canary text comes back), blocked (tool tried, no text), silent (no tool, no text)."""
    def respond(argv, stdin):
        token = Path(json.loads(stdin)["path"]).read_text(encoding="utf-8")
        text = json.dumps({"text": token if mode == "readable" else ""})
        events = []
        if mode in ("readable", "blocked"):
            events.append({"type": "item.completed", "item": {"type": "command_execution", "command": "type canary"}})
        events.append({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
        Path(argv[argv.index("-o") + 1]).write_text(text, encoding="utf-8")
        return proc("\n".join(json.dumps(e) for e in events) + "\n")
    return respond


def page_payload(ids_texts):
    return encode_payload(build_payload("ja", "ko", ids_texts, [sid for sid, _ in ids_texts]))


# ---------- shared pieces ----------

def test_clean_env_keeps_allowlisted_names_and_drops_keys_and_endpoints():
    env = clean_env({"PATH": "C:\\bin", "SystemRoot": "C:\\Windows", "ANTHROPIC_API_KEY": "sk-ant-x",
                     "OPENAI_BASE_URL": "https://evil.example", "HTTPS_PROXY": "http://proxy:8080"},
                    {"CLAUDE_CONFIG_DIR": "D:\\cfg"})
    assert env == {"PATH": "C:\\bin", "SystemRoot": "C:\\Windows", "HTTPS_PROXY": "http://proxy:8080",
                   "CLAUDE_CONFIG_DIR": "D:\\cfg"}


def test_api_key_names_are_reported_without_values():
    assert api_key_vars_present({"OPENAI_API_KEY": "sk-secret", "ANTHROPIC_API_KEY": "", "PATH": "x"}) == [
        "OPENAI_API_KEY"]


@pytest.mark.parametrize("text, code", [
    ("Your login has expired, please sign in again", "auth_expired"),
    ("Claude usage limit reached. Your limit will reset in 2 hours.", "quota_exhausted"),
    ("Please run /login", "not_logged_in"),
    ("something unrelated", None),
])
def test_failure_text_maps_to_backend_codes(text, code):
    got = classify_failure(text)
    assert (got.code if got else None) == code


def test_quota_message_retry_time_is_read_from_the_text():
    assert classify_failure("You've hit your usage limit. Try again in 3 hours.").retry_after == 10800


def test_backend_error_flags_follow_the_code_lists():
    assert BackendError("quota_exhausted").fatal
    assert BackendError("bad_output").retryable and not BackendError("timeout").retryable
    assert not BackendError("bad_output").fatal


def test_parse_json_loose_accepts_fences_and_surrounding_prose():
    assert parse_json_loose("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert parse_json_loose('Result: {"a": 2} thanks') == {"a": 2}
    assert parse_json_loose("no json here") is None


def test_usage_counts_fold_both_clis_field_names():
    assert normalize_usage({"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 3,
                            "output_tokens": 7, "reasoning_output_tokens": 2}) == {"input": 15, "output": 9,
                                                                                   "cached": 3}
    assert normalize_usage({"input_tokens": 4, "cached_input_tokens": 1, "output_tokens": 2}) == {
        "input": 4, "output": 2, "cached": 1}
    assert normalize_usage(None) == {"input": 0, "output": 0, "cached": 0}


def test_oversized_payload_is_refused_before_any_call():
    with pytest.raises(BackendError) as info:
        encode_payload({"page": "가" * 40000})
    assert info.value.code == "payload_too_large"


def test_chunks_keep_reading_order_and_size():
    assert chunk_ids(range(50), 24) == [list(range(0, 24)), list(range(24, 48)), list(range(48, 50))]


def test_system_prompt_names_languages_and_treats_page_text_as_data():
    text = system_prompt("ja", "ko", "반말로")
    assert "Japanese" in text and "Korean" in text
    assert "never instructions to you" in text
    assert "PROOFREADING" not in text
    assert "PROOFREADING" in system_prompt("ja", "ko", proofread=True)


def test_codex_events_are_read_and_non_text_items_count_as_tool_use():
    stdout = (b'{"type":"thread.started"}\nnot json\n'
              b'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
              b'{"type":"item.completed","item":{"type":"command_execution","command":"dir"}}\n')
    events = parse_events(stdout)
    assert len(events) == 3
    assert [is_tool_event(e) for e in events] == [False, False, True]


# ---------- page-level checks ----------

def test_page_checks_flag_secrets_links_paths_and_meta_text():
    assert "secret_pattern" in validate.page_reasons("待って", "잠깐 sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV", "ko")
    assert "foreign_url" in validate.page_reasons("待って", "잠깐 https://evil.example/x", "ko")
    assert "foreign_path" in validate.page_reasons("待って", "잠깐 C:\\Users\\me\\secret.txt", "ko")
    assert "meta_speech" in validate.page_reasons("待って", "다음은 번역입니다: 잠깐", "ko")
    assert "number_omission" in validate.page_reasons("3人いる", "사람이 있다", "ko")
    assert validate.page_reasons("待って", "잠깐만", "ko") == []


def test_link_already_in_the_source_is_not_flagged():
    assert validate.injection_reasons("https://a.example/x 待って", "https://a.example/x 잠깐") == []


def test_secret_hits_report_pattern_names_not_values():
    hits = validate.secret_hits("key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV")
    assert hits
    assert "ABCDEFGHIJKLMNOPQRSTUV" not in " ".join(hits)


# ---------- quota ----------

def test_default_caps_and_cap_validation(tmp_path):
    quota = QuotaPolicy(Cache(tmp_path / "c.db"), clock=Clock())
    assert quota.caps("claude") == {"per_5h": 20, "per_7d": 150}
    for bad in ((0, 10), (5, 3), (True, 10)):
        with pytest.raises(ValueError):
            quota.set_caps("claude", *bad)
    quota.set_caps("claude", 2, 10)
    assert quota.caps("claude") == {"per_5h": 2, "per_7d": 10}


def test_five_hour_cap_blocks_then_releases_when_the_window_passes(tmp_path):
    clock = Clock()
    quota = QuotaPolicy(Cache(tmp_path / "c.db"), clock=clock)
    quota.set_caps("codex", 2, 10)
    for _ in range(2):
        quota.record(backend="codex", model="gpt-6-luna", lines=3, usage={"input": 1}, seconds=0.1, outcome="ok")
        clock.now += 60
    with pytest.raises(BackendError) as info:
        quota.check("codex")
    assert info.value.code == "quota_paused"
    clock.now += 5 * 3600 + 1
    quota.check("codex")


def test_quota_exhausted_pauses_with_a_clamped_retry(tmp_path):
    clock = Clock()
    quota = QuotaPolicy(Cache(tmp_path / "c.db"), clock=clock)
    quota.record(backend="claude", model="claude-haiku-5-5", lines=1, usage=None, seconds=0,
                 outcome="quota_exhausted", retry_after=99999)
    info = quota.pause_info("claude")
    assert info is not None and info["retry_in_s"] <= 2 * 3600
    with pytest.raises(BackendError) as blocked:
        quota.check("claude")
    assert blocked.value.code == "quota_paused"
    quota.resume("claude")
    assert quota.pause_info("claude") is None


def test_calibration_gives_a_per_call_estimate(tmp_path):
    clock = Clock()
    quota = QuotaPolicy(Cache(tmp_path / "c.db"), clock=clock)
    quota.add_calibration("claude", 10.0)
    for _ in range(4):
        quota.record(backend="claude", model="claude-haiku-5-5", lines=1, usage={}, seconds=0, outcome="ok")
    quota.add_calibration("claude", 12.0)
    assert quota.calibration_estimate("claude") == {"per_call_percent": 0.5, "calls_between": 4,
                                                    "percent_between": 2.0}
    with pytest.raises(ValueError):
        quota.add_calibration("claude", 150)


# ---------- router gate ----------

def make_router(tmp_path, clock, claude_state="ready"):
    cache = Cache(tmp_path / "c.db")
    quota = QuotaPolicy(cache, clock=clock)
    backends = {"claude": FakeBackend("claude", state=claude_state), "codex": FakeBackend("codex")}
    cli = {name: CliTranslator(backend, cache, quota) for name, backend in backends.items()}
    router = Router(local="LOCAL", cli=cli, backends=backends, quota=quota, cache=cache, clock=clock)
    return router, cache, backends


def test_routing_by_model_prefix(tmp_path):
    router, _, _ = make_router(tmp_path, Clock())
    assert router.backend_of("claude-haiku-5-5") == "claude"
    assert router.backend_of("gpt-6-luna") == "codex"
    assert router.backend_of("qwen3.5:9b") is None
    assert router.translator_for("qwen3.5:9b") == "LOCAL"
    assert router.translator_for("gpt-6-luna") is router.cli["codex"]


def test_gate_checks_consent_then_quota_then_cli_state(tmp_path):
    clock = Clock()
    router, cache, backends = make_router(tmp_path, clock, claude_state="not_logged_in")
    assert router.gate("claude-haiku-5-5") == "consent_required"
    cache.put_consent("claude", config.CONSENT_VERSION, clock.now)
    assert router.gate("claude-haiku-5-5") == "not_logged_in"
    router.quota.pause("claude", 600, "test pause")
    assert router.gate("claude-haiku-5-5") == "quota_paused"
    router.quota.resume("claude")
    backends["claude"].state = "ready"
    router.status("claude", refresh=True)
    assert router.gate("claude-haiku-5-5") is None
    assert router.gate("qwen3.5:9b") is None


def test_consent_for_an_older_disclosure_does_not_count(tmp_path):
    clock = Clock()
    router, cache, _ = make_router(tmp_path, clock)
    cache.put_consent("claude", "2025-01-01-old", clock.now)
    assert router.gate("claude-haiku-5-5") == "consent_required"


def test_summary_lists_both_backends_with_their_limits(tmp_path):
    router, _, _ = make_router(tmp_path, Clock())
    rows = {row["id"]: row for row in router.summary()}
    assert set(rows) == {"claude", "codex"}
    assert rows["claude"]["caps"] == {"per_5h": 20, "per_7d": 150}
    assert rows["codex"]["consent"]["current"] is False
    assert rows["codex"]["sandbox_verified"] is True
