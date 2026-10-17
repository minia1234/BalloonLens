"""Stream-json Claude processes, some started ahead of time: a waiting one answers if there is one, otherwise one is
started for the call, and no process outlives its use. The fake CLI in tests/fixtures speaks both the one-shot and the
stream-json form."""
import json
import sys
import time
from pathlib import Path

import pytest

from balloonlens.backends.base import BackendError, cancel_scope
from balloonlens.backends.claude_cli import ClaudeCodeBackend
from balloonlens.backends.page import CliTranslator
from balloonlens.backends.prompts import PAGE_SCHEMA_JSON, build_payload, encode_payload
from balloonlens.cache import Cache

FAKE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"
MODEL = "claude-haiku-5-5"
SYSTEM = "system words"


def backend(tmp_path, mode="ok", log=None, warm=1, ttl=600.0, timeout=20):
    command = [sys.executable, str(FAKE_CLAUDE), "--mode", mode] + (["--log", str(log)] if log else [])
    return ClaudeCodeBackend(command=command, config_dir=tmp_path / "cfg", sandbox_dir=tmp_path / "sb",
                             runtime_dir=tmp_path / "rt", timeout=timeout, warm=warm, warm_ttl=ttl)


def payload():
    return encode_payload(build_payload("ja", "ko", [(1, "待って")], [1]))


def key_of(b, system=SYSTEM):
    b._ensure_dirs()  # the system prompt file lives in the runtime folder
    return (MODEL, str(b._system_file(system)))


def wait_ready(b, key, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        procs = b.warm._by_key.get(key, [])
        if procs and all(p.is_ready() for p in procs):
            return
        time.sleep(0.05)
    raise AssertionError("no process became ready")


def wait_dead(proc, timeout=10):
    end = time.time() + timeout
    while proc.alive() and time.time() < end:
        time.sleep(0.05)
    return not proc.alive()


def read_log(path):
    return [json.loads(row) for row in Path(path).read_text(encoding="utf-8").splitlines()]


def test_a_waiting_process_answers_and_without_one_the_call_starts_its_own(tmp_path):
    log = tmp_path / "log.jsonl"
    b = backend(tmp_path, "ok", log)
    try:
        first = b.complete_json(SYSTEM, payload(), model=MODEL)
        assert first["warm"] is False  # nothing was waiting yet: a process was started for this call
        wait_ready(b, key_of(b))
        second = b.complete_json(SYSTEM, payload(), model=MODEL)
        assert second["warm"] is True and second["data"] == first["data"]
        assert second["usage"] == {"input": 300, "output": 60, "cached": 120}
        for row in read_log(log):
            assert "--input-format" in row["argv"] and "--json-schema" not in row["argv"]
            assert row["stdin"] == payload().decode("utf-8")  # the same data reached the CLI, nothing else
            assert "--tools" in row["argv"]
    finally:
        b.warm.shutdown()


def test_connectors_are_kept_out_of_every_form_of_call(tmp_path):
    """Without this flag the account's MCP tools (Figma, Jira...) are loaded into the call: about 60k tokens of
    definitions on every request, and tools that page text could ask for."""
    log = tmp_path / "log.jsonl"
    b = backend(tmp_path, "ok", log)
    try:
        b.complete_json(SYSTEM, payload(), model=MODEL)  # stream-json
        b.complete_json(SYSTEM, payload(), model=MODEL, schema_json=PAGE_SCHEMA_JSON)  # one-shot
    finally:
        b.warm.shutdown()
    streamed, one_shot = read_log(log)
    assert "--input-format" in streamed["argv"] and "--input-format" not in one_shot["argv"]
    for row in (streamed, one_shot):
        assert "--strict-mcp-config" in row["argv"] and "--mcp-config" not in row["argv"]


def test_each_process_answers_once_and_a_replacement_is_started(tmp_path):
    b = backend(tmp_path, "ok")
    try:
        b.complete_json(SYSTEM, payload(), model=MODEL)
        key = key_of(b)
        wait_ready(b, key)
        used = list(b.warm._by_key[key])
        assert b.complete_json(SYSTEM, payload(), model=MODEL)["warm"] is True
        wait_ready(b, key)
        waiting = b.warm._by_key[key]
        assert len(waiting) == 1 and waiting[0] is not used[0]
        assert wait_dead(used[0])  # closed after its one answer
    finally:
        b.warm.shutdown()


def test_a_process_that_has_not_finished_starting_is_not_handed_out(tmp_path):
    b = backend(tmp_path, "ok")
    try:
        key = key_of(b)
        b.warm.refill(key)
        assert b.warm.take(key) is None  # the fake needs a moment to answer the start-up request
        wait_ready(b, key)
        assert b.warm.take(key) is not None
    finally:
        b.warm.shutdown()


def test_a_call_uses_a_starting_process_instead_of_starting_over(tmp_path, monkeypatch):
    b = backend(tmp_path, "ok")
    try:
        key = key_of(b)
        b.warm.refill(key)
        starting = b.warm._by_key[key][0]
        monkeypatch.setattr(starting, "is_ready", lambda: False)
        used = []
        ask = starting.ask

        def record_ask(*args, **kwargs):
            used.append(True)
            return ask(*args, **kwargs)

        monkeypatch.setattr(starting, "ask", record_ask)
        result = b.complete_json(SYSTEM, payload(), model=MODEL)
        assert used == [True]
        assert result["warm"] is False  # still starting when borrowed, not a fully warmed call
        assert result["data"]["items"][0]["text"] == "번역문"
        assert wait_dead(starting)
    finally:
        b.warm.shutdown()


def test_preparing_alone_makes_no_model_call(tmp_path):
    log = tmp_path / "log.jsonl"
    b = backend(tmp_path, log=log)
    try:
        b.prepare(SYSTEM, model=MODEL)
        wait_ready(b, key_of(b))
        assert not log.exists()  # the fake logs only actual user requests
        result = b.complete_json(SYSTEM, payload(), model=MODEL)
        assert result["warm"] is True and len(read_log(log)) == 1
    finally:
        b.warm.shutdown()


def test_calls_with_a_schema_do_not_use_the_waiting_processes(tmp_path):
    log = tmp_path / "log.jsonl"
    b = backend(tmp_path, "ok", log)
    try:
        b.complete_json(SYSTEM, payload(), model=MODEL)
        wait_ready(b, key_of(b))
        out = b.complete_json(SYSTEM, payload(), model=MODEL, schema_json=PAGE_SCHEMA_JSON)
        assert out["warm"] is False
        assert "--json-schema" in read_log(log)[-1]["argv"]
        assert b.warm.count(key_of(b)) == 1  # the waiting process was left alone
    finally:
        b.warm.shutdown()


def test_only_the_two_latest_prompts_keep_processes(tmp_path):
    b = backend(tmp_path, "ok")
    try:
        first = key_of(b, "one")
        b.complete_json("one", payload(), model=MODEL)
        wait_ready(b, first)
        old = list(b.warm._by_key[first])
        b.complete_json("two", payload(), model=MODEL)
        b.complete_json("three", payload(), model=MODEL)
        assert first not in b.warm._by_key and len(b.warm._by_key) == 2
        assert wait_dead(old[0])
    finally:
        b.warm.shutdown()


def test_a_process_that_died_while_waiting_is_skipped(tmp_path):
    b = backend(tmp_path, "die_idle")  # a process nobody asks within a moment ends by itself
    try:
        first = b.complete_json(SYSTEM, payload(), model=MODEL)
        assert first["data"]["items"][0]["text"] == "번역문"
        time.sleep(1.5)  # long enough for the waiting process to end
        again = b.complete_json(SYSTEM, payload(), model=MODEL)
        assert again["warm"] is False and again["data"]["items"][0]["text"] == "번역문"
    finally:
        b.warm.shutdown()


def test_an_old_waiting_process_is_stopped(tmp_path):
    b = backend(tmp_path, "ok", ttl=0.5)
    try:
        key = key_of(b)
        b.warm.refill(key)
        old = list(b.warm._by_key[key])
        time.sleep(0.8)
        assert b.warm.take(key) is None  # too old: stopped, not handed out
        assert wait_dead(old[0])
    finally:
        b.warm.shutdown()


def test_a_login_failure_is_reported_and_clears_the_pool(tmp_path):
    b = backend(tmp_path, "die_mid")
    try:
        with pytest.raises(BackendError) as caught:
            b.complete_json(SYSTEM, payload(), model=MODEL)
        assert caught.value.code == "not_logged_in"
        assert not b.warm._by_key.get(key_of(b))
    finally:
        b.warm.shutdown()


def test_error_replies_use_the_same_codes_as_one_shot_calls(tmp_path):
    b = backend(tmp_path, "quota")
    try:
        with pytest.raises(BackendError) as caught:
            b.complete_json(SYSTEM, payload(), model=MODEL)
        assert caught.value.code == "quota_exhausted" and caught.value.retry_after == 7200
    finally:
        b.warm.shutdown()


def test_cancelling_a_page_stops_its_process(tmp_path):
    b = backend(tmp_path, "slow")
    try:
        key = key_of(b)
        b.warm.refill(key)
        wait_ready(b, key)
        started = time.monotonic()
        with cancel_scope(lambda: time.monotonic() - started > 0.5):
            with pytest.raises(BackendError) as caught:
                b.complete_json(SYSTEM, payload(), model=MODEL)
        assert caught.value.code == "cancelled"
        assert time.monotonic() - started < 10
    finally:
        b.warm.shutdown()


def test_a_call_that_takes_too_long_times_out(tmp_path):
    b = backend(tmp_path, "slow", timeout=1.5)
    try:
        key = key_of(b)
        b.warm.refill(key)
        wait_ready(b, key)
        with pytest.raises(BackendError) as caught:
            b.complete_json(SYSTEM, payload(), model=MODEL)
        assert caught.value.code == "timeout"
    finally:
        b.warm.shutdown()


def test_shutdown_stops_every_waiting_process(tmp_path):
    b = backend(tmp_path, "ok", warm=2)
    key = key_of(b)
    b.warm.refill(key)
    waiting = list(b.warm._by_key[key])
    assert len(waiting) == 2
    b.warm.shutdown()
    assert all(wait_dead(p) for p in waiting)


def test_without_waiting_processes_every_call_is_one_shot(tmp_path):
    log = tmp_path / "log.jsonl"
    b = backend(tmp_path, "ok", log, warm=0)
    assert b.warm is None
    assert b.complete_json(SYSTEM, payload(), model=MODEL)["warm"] is False
    assert "--input-format" not in read_log(log)[0]["argv"]


class WarmFake:
    backend_id = "claude"
    cache_id = "claude-cli"
    label = "Claude"
    model = MODEL

    def complete_json(self, system, payload, *, model=None, schema_json=None, schema_name="page"):
        body = json.loads(payload)
        items = [{"id": sid, "text": "번역문", "review": False} for sid in body["target_ids"]]
        return {"data": {"items": items}, "usage": {"input": 1, "output": 1, "cached": 0}, "seconds": 0.01,
                "warm": True}


def test_the_page_counts_the_calls_that_a_waiting_process_answered(tmp_path):
    got = {}
    meta = CliTranslator(WarmFake(), Cache(tmp_path / "c.db")).translate_page(
        [(1, "台詞あ"), (2, "台詞い")], "ja", "ko", MODEL, "scope", lambda sid, text, status: got.__setitem__(sid, status))
    assert meta["calls"] == 1 and meta["warm_calls"] == 1
    assert set(got.values()) == {"done"}
