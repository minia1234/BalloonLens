"""Translation backends.

- local: Ollama (translate.Translator). Offline, default for installs that already used it.
- claude: Claude Code subscription via `claude -p` (ClaudeCodeBackend + CliTranslator).
- codex: Codex subscription via `codex exec` (CodexBackend + CliTranslator), gated by the sandbox probe.
"""
from .base import BackendError, BackendStatus, ProcessRunner, classify_failure, clean_env
from .claude_cli import ClaudeCodeBackend
from .codex_cli import CodexBackend
from .page import CliTranslator
from .quota import QuotaPolicy
from .router import Router

__all__ = [
    "BackendError", "BackendStatus", "ProcessRunner", "classify_failure", "clean_env",
    "ClaudeCodeBackend", "CodexBackend", "CliTranslator", "QuotaPolicy", "Router",
]
