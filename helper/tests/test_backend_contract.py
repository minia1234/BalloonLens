"""Contract checks for the subscription backends (design §15.2): what the child process may see, which lines may
be reused together, and what context a later chunk of a long page carries."""
import json

from balloonlens.backends.base import clean_env
from balloonlens.backends.page import CliTranslator
from balloonlens.cache import Cache


class Replying:
    """Answers each target id with the same reply and keeps every payload."""

    def __init__(self, backend_id, cache_id, reply="번역문"):
        self.backend_id = backend_id
        self.cache_id = cache_id
        self.reply = reply
        self.payloads = []

    @property
    def calls(self):
        return len(self.payloads)

    def complete_json(self, system, payload, *, model=None, schema_json=None, schema_name="page"):
        self.payloads.append(payload)
        ids = json.loads(payload)["target_ids"]
        return {"data": {"items": [{"id": sid, "text": self.reply, "review": False} for sid in ids]},
                "usage": {}, "seconds": 0}


def noop(*args):
    return None


def test_endpoint_cloud_and_key_variables_never_reach_the_cli():
    host_env = {
        "PATH": "C:\\bin", "SystemRoot": "C:\\Windows", "HTTPS_PROXY": "http://proxy:8080",
        "ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "https://evil.example",
        "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "AWS_ACCESS_KEY_ID": "AKIA",
        "AWS_PROFILE": "p", "GOOGLE_APPLICATION_CREDENTIALS": "c.json", "OPENAI_API_KEY": "sk",
        "OPENAI_BASE_URL": "https://evil.example", "CODEX_API_KEY": "k",
    }
    env = clean_env(host_env, {"CLAUDE_CONFIG_DIR": "D:\\cfg"})
    assert set(env) == {"PATH", "SystemRoot", "HTTPS_PROXY", "CLAUDE_CONFIG_DIR"}


def test_two_backends_never_share_cached_lines(tmp_path):
    cache = Cache(tmp_path / "c.db")
    claude = Replying("claude", "claude-cli")
    codex = Replying("codex", "codex-cli")
    lines = [(1, "待って")]
    CliTranslator(claude, cache).translate_page(lines, "ja", "ko", "claude-haiku-5-5", "scope", noop)
    other = CliTranslator(codex, cache).translate_page(lines, "ja", "ko", "gpt-6-luna", "scope", noop)
    assert other["cache_hits"] == 0 and codex.calls == 1
    again = CliTranslator(claude, cache).translate_page(lines, "ja", "ko", "claude-haiku-5-5", "scope", noop)
    assert again["cache_hits"] == 1 and claude.calls == 1


def test_a_later_chunk_carries_the_last_two_lines_as_context(tmp_path):
    backend = Replying("claude", "claude-cli")
    digit_free = [(i + 1, "台詞" + "あ" * (i % 5 + 1)) for i in range(26)]  # 24 lines, then 2 more
    CliTranslator(backend, Cache(tmp_path / "c.db")).translate_page(digit_free, "ja", "ko", "claude-haiku-5-5",
                                                                    "scope", noop)
    first, second = (json.loads(p) for p in backend.payloads)
    assert first["context"] == []
    assert [item["source"] for item in second["context"]] == [digit_free[22][1], digit_free[23][1]]
    assert all(item["translation"] == "번역문" for item in second["context"])
