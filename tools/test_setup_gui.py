"""Checks for the setup window's pure helpers (no window is opened). Run: python -m pytest tools/test_setup_gui.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import setup_gui  # noqa: E402


def make_tree(base: Path, helper: bool = True) -> Path:
    (base / "tools").mkdir(parents=True)
    (base / "tools" / "setup_windows.ps1").write_text("# stub", encoding="utf-8")
    if helper:
        (base / "helper").mkdir()
    return base


def test_find_root_walks_up_from_a_nested_folder(tmp_path):
    root = make_tree(tmp_path / "BalloonLens")
    nested = root / "extension" / "icons"
    nested.mkdir(parents=True)
    assert setup_gui.find_root(nested) == root


def test_find_root_needs_the_helper_folder(tmp_path):
    root = make_tree(tmp_path / "Lonely", helper=False)
    assert setup_gui.find_root(root) is None


def test_parse_status_ignores_text_printed_before_the_json():
    text = "[ok] Native messaging host registered\n{\n  \"deps\": true,\n  \"claude\": {\"installed\": false}\n}\n"
    status = setup_gui.parse_status(text)
    assert status == {"deps": True, "claude": {"installed": False}}


def test_parse_status_refuses_output_without_json():
    try:
        setup_gui.parse_status("no object here")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_status_rows_mark_a_logged_out_claude_as_a_warning_with_the_login_hint():
    status = {"deps": True, "nativeHost": True, "helper": False, "ollama": False,
              "claude": {"installed": True, "version": "2.1.285 (Claude Code)", "versionOk": True, "loggedIn": False}}
    rows = {name: (text, state) for name, text, state in setup_gui.status_rows(status)}
    text, state = rows["Claude Code"]
    assert state == "warn" and "로그인 필요" in text
    assert "Codex CLI" not in rows
    assert rows["도우미"][1] == "off"
    assert rows["크롬 확장"][1] == "warn"


def test_a_port_taken_by_another_program_is_a_warning_on_the_helper_row():
    rows = {name: (text, state) for name, text, state in setup_gui.status_rows({"helper": False, "portOwner": "other:node"})}
    text, state = rows["도우미"]
    assert state == "warn" and "다른 프로그램(node)" in text


def test_status_rows_are_all_ok_when_everything_is_ready():
    status = {"deps": True, "nativeHost": True, "helper": True, "ollama": True,
              "claude": {"installed": True, "version": "2.1.285 (Claude Code)", "versionOk": True, "loggedIn": True}}
    states = {name: state for name, _, state in setup_gui.status_rows(status)}
    assert states["번역 환경 (Python 패키지)"] == "ok"
    assert states["Claude Code"] == "ok"
    assert states["Ollama (선택 사항)"] == "ok"
    assert states["크롬 확장"] == "warn"  # no Chrome profile entry in this status


def test_claude_login_quotes_the_config_folder_for_powershell():
    args = setup_gui.claude_login_args(r"%USERPROFILE%\.local\bin\claude.exe", Path("C:/Users/O'Brien/BalloonLens/claude-config"))
    assert args[:2] == ["powershell.exe", "-NoExit"]
    script = args[-1]
    assert "O''Brien" in script  # a single quote inside the path is doubled
    assert script.endswith("auth login")
    assert "claude.exe" in script


def test_diagnostics_hold_states_and_paths_but_no_tokens_or_dialogue():
    status = {"root": "C:/BL", "dataDir": "C:/BL-data", "deps": True, "nativeHost": False, "helper": True,
              "ollama": False, "claude": {"installed": True, "version": "2.1.285", "loggedIn": True}}
    text = setup_gui.diagnostics_text(status, "0.5.0")
    assert "0.5.0" in text and "C:/BL-data" in text
    assert "Claude Code: 2.1.285 · 로그인 됨" in text
    assert "token" not in text.lower()


def test_decode_output_reads_utf8_and_falls_back_to_cp949():
    assert setup_gui.decode_output("로그인 됨".encode("utf-8")) == "로그인 됨"
    assert setup_gui.decode_output("로그인 됨".encode("cp949")) == "로그인 됨"


def test_app_version_reads_the_extension_manifest(tmp_path):
    root = make_tree(tmp_path / "BL")
    (root / "extension").mkdir()
    (root / "extension" / "manifest.json").write_text(json.dumps({"version": "0.5.0"}), encoding="utf-8")
    assert setup_gui.app_version(root) == "0.5.0"
    assert setup_gui.app_version(tmp_path / "missing") == "?"


def test_step_lines_from_the_setup_script_are_parsed():
    assert setup_gui.parse_step("[step 2/6] 번역 패키지 확인") == (2, 6, "번역 패키지 확인")
    assert setup_gui.parse_step("[ok] Setup complete.") is None


def test_the_extension_line_says_installed_and_enabled_per_profile():
    assert setup_gui.extension_row({"profile": "Default", "name": "work", "enabled": True}) == (
        "설치됨 · 프로필 work (켜짐)", "ok")
    assert setup_gui.extension_row({"profile": "Default", "name": "work", "enabled": False})[1] == "warn"
    assert setup_gui.extension_row({"elsewhere": "Default"})[1] == "warn"
    assert setup_gui.extension_row(None)[1] == "warn"
    assert "아직 설치되지 않았어요" in setup_gui.extension_row(None)[0]
