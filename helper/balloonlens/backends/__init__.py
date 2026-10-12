"""Translation backends.

- local: Ollama (translate.Translator). Offline, default for installs that already used it.
- claude: Claude Code subscription via `claude -p` (ClaudeCodeBackend + CliTranslator).
"""
from .base import BackendError, BackendStatus, ProcessRunner, classify_failure, clean_env
from .claude_cli import ClaudeCodeBackend
from .page import CliTranslator
from .quota import QuotaPolicy
from .router import Router

__all__ = [
    "BackendError", "BackendStatus", "ProcessRunner", "classify_failure", "clean_env",
    "ClaudeCodeBackend", "CliTranslator", "QuotaPolicy", "Router",
]
