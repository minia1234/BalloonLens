import json

import pytest

from balloonlens.cache import Cache
from balloonlens.translate import Translator, TranslateError, clean_output, looks_like_target, parse_qwen


def test_ollama_host_bind_values_become_client_urls():
    from balloonlens.config import ollama_base_url

    assert ollama_base_url(None) == "http://127.0.0.1:11434"
    assert ollama_base_url("0.0.0.0") == "http://127.0.0.1:11434"
    assert ollama_base_url("0.0.0.0:12345") == "http://127.0.0.1:12345"
    assert ollama_base_url("https://gpu-box:11434/") == "https://gpu-box:11434"


def test_clean_output_strips_think_and_quotes():
    assert clean_output('<think>hmm</think>\n"안녕"') == "안녕"


def test_target_language_check():
    assert looks_like_target("안녕하세요!", "ko")
    assert looks_like_target("...!?", "ko")
    assert not looks_like_target("Hello there", "ko")


def test_parse_qwen_ignores_unknown_and_empty_ids():
    content = json.dumps({"items": [{"id": 1, "text": "가"}, {"id": 9, "text": "x"}, {"id": 2, "text": " "}]})
    assert parse_qwen(content, [1, 2]) == {1: "가"}
    assert parse_qwen("not json", [1]) == {}


@pytest.mark.parametrize("items", [None, 42, True, {}, "bad", [{"id": [], "text": "가"}],
                                   [{"id": True, "text": "가"}], [{"id": 1.0, "text": "가"}]])
def test_parse_qwen_rejects_malformed_items(items):
    assert parse_qwen(json.dumps({"items": items}), [1]) == {}


def _results(tr, lines, model):
    out = {}
    tr.translate(lines, "ja", "ko", model, "test", lambda sid, t, st: out.__setitem__(sid, (t, st)))
    return out


def test_qwen_missing_line_is_retried_once(tmp_path):
    calls = []

    def chat(model, messages, fmt=None, think=None):
        items = json.loads(messages[-1]["content"])
        calls.append([i["id"] for i in items])
        # batch call drops id 1; single retry answers it
        answer = [{"id": i["id"], "text": f"번역{i['id']}"} for i in items if len(items) == 1 or i["id"] != 1]
        return json.dumps({"items": answer}), {"load_ms": 0}

    out = _results(Translator(Cache(tmp_path / "c.db"), chat=chat), [(0, "a"), (1, "b")], "qwen3:8b")
    assert out == {0: ("번역0", "done"), 1: ("번역1", "done")}
    assert calls == [[0, 1], [1]]


def test_gemma_wrong_language_marked_review_and_not_cached(tmp_path):
    cache = Cache(tmp_path / "c.db")
    tr = Translator(cache, chat=lambda *a, **k: ("Hello", {"load_ms": 0}))
    assert _results(tr, [(0, "こんにちは")], "translategemma:4b") == {0: ("Hello", "review")}
    tr_ok = Translator(cache, chat=lambda *a, **k: ("안녕", {"load_ms": 0}))
    assert _results(tr_ok, [(0, "こんにちは")], "translategemma:4b") == {0: ("안녕", "done")}


def test_ollama_down_marks_failed(tmp_path):
    def chat(*a, **k):
        raise TranslateError("ollama_unreachable")

    tr = Translator(Cache(tmp_path / "c.db"), chat=chat)
    assert _results(tr, [(0, "x")], "translategemma:4b") == {0: (None, "failed")}
