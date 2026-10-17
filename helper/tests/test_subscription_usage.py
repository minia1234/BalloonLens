"""Real account usage metadata, never estimates derived from the local call ledger."""
import json
import threading
import time

import httpx
import pytest

from balloonlens.backends.subscription_usage import ClaudeUsage, USAGE_URL, parse_windows


def body(five=11, week=92):
    return {"five_hour": {"utilization": five, "resets_at": "2099-01-01T12:00:00Z"},
            "seven_day": {"utilization": week, "resets_at": "2099-01-07T12:00:00Z"}}


def wait_usage(reader):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = reader.snapshot()
        if result["state"] != "loading":
            return result
        time.sleep(0.005)
    raise AssertionError("usage fetch did not finish")


def test_official_percentages_are_converted_to_remaining_without_losing_zero():
    windows = parse_windows(body(five=0, week=100))
    assert [(w["id"], w["remaining_percent"]) for w in windows] == [("five_hour", 100), ("seven_day", 0)]
    assert all(isinstance(w["resets_at"], (int, float)) for w in windows)


@pytest.mark.parametrize("value", [None, True, "12", float("nan"), float("inf"), {}, []])
def test_unknown_or_invalid_values_are_not_reported_as_unused(value):
    assert parse_windows(body(five=value, week=value)) == []


def test_new_meter_shape_uses_session_and_weekly_all_not_model_scoped_limits():
    data = {"limits": [{"kind": "session", "percent": 25, "resets_at": None},
                       {"kind": "weekly_all", "percent": 90, "resets_at": None},
                       {"kind": "weekly_scoped", "percent": 99, "scope": {"model": {"display_name": "Sonnet"}}}]}
    assert [(w["id"], w["remaining_percent"]) for w in parse_windows(data)] == [("five_hour", 75), ("seven_day", 10)]


def test_only_the_translation_accounts_credentials_reach_the_fixed_usage_endpoint(tmp_path, monkeypatch):
    config_dir = tmp_path / "translation-account"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "test-secret"}}))
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json=body(), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    reader = ClaudeUsage(config_dir)
    result = wait_usage(reader)
    assert result["state"] == "ready" and result["stale"] is False
    assert [w["remaining_percent"] for w in result["windows"]] == [89, 8]
    url, options = calls[0]
    assert url == USAGE_URL == "https://api.anthropic.com/api/oauth/usage"
    assert options["headers"]["Authorization"] == "Bearer test-secret"
    assert options["follow_redirects"] is False
    assert len(calls) == 1 and "test-secret" not in json.dumps(result)


def test_missing_dedicated_credentials_never_fall_back_to_another_account(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("no request without translation credentials"))
    result = wait_usage(ClaudeUsage(tmp_path))
    assert result["state"] == "unavailable" and result["windows"] == []


def test_polling_is_nonblocking_and_shares_one_fetch_for_five_minutes(tmp_path):
    now, calls = [1000.0], []
    release = threading.Event()

    def fetch():
        calls.append(True)
        release.wait(2)
        return body()

    reader = ClaudeUsage(tmp_path, fetch=fetch, clock=lambda: now[0])
    try:
        assert reader.snapshot()["state"] == "loading"
        for _ in range(20):
            assert reader.snapshot()["state"] == "loading"
        release.set()
        assert wait_usage(reader)["state"] == "ready"
        now[0] += 299
        assert reader.snapshot()["state"] == "ready" and len(calls) == 1
        now[0] += 2
        reader.snapshot()
        assert wait_usage(reader)["checked_at"] == now[0] and len(calls) == 2
    finally:
        release.set()


def test_failed_refresh_keeps_last_known_values_marked_stale_and_backs_off(tmp_path, monkeypatch):
    now, fail, requests = [1000.0], [False], []
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "test-secret"}}))

    def get(url, **kwargs):
        requests.append(True)
        return httpx.Response(429 if fail[0] else 200, json={} if fail[0] else body(),
                              headers={"retry-after": "600"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    reader = ClaudeUsage(tmp_path, clock=lambda: now[0])
    first = wait_usage(reader)
    fail[0] = True
    now[0] += 301
    reader.snapshot()
    deadline = time.monotonic() + 2
    while reader.snapshot().get("error") is None and time.monotonic() < deadline:
        time.sleep(0.005)
    result = reader.snapshot()
    assert result["stale"] is True and result["windows"] == first["windows"]
    assert result["checked_at"] == first["checked_at"] and result["error"] == "rate_limited"
    now[0] += 599
    reader.snapshot()
    assert len(requests) == 2


def test_expired_window_is_unknown_until_refetched(tmp_path):
    now = [1_000.0]
    reader = ClaudeUsage(tmp_path, fetch=lambda: {"five_hour": {"utilization": 80, "resets_at": 1010}},
                         clock=lambda: now[0])
    assert wait_usage(reader)["windows"][0]["remaining_percent"] == 20
    now[0] = 1011
    result = reader.snapshot()
    assert result["stale"] is True and result["windows"][0]["remaining_percent"] is None
