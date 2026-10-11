"""Checks for reading Chrome's profile files. The files are fakes in a temporary folder; no real profile is touched."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chrome_setup  # noqa: E402

ID = chrome_setup.EXTENSION_ID


def write_profile(user_data: Path, folder: str, name: str, entry: dict | None = None):
    profile = user_data / folder
    profile.mkdir(parents=True, exist_ok=True)
    settings = {} if entry is None else {ID: entry}
    (profile / "Preferences").write_text(json.dumps({"extensions": {"settings": settings}}), encoding="utf-8")
    return name


def write_local_state(user_data: Path, names: dict[str, str], last_used: str):
    info = {folder: {"name": name} for folder, name in names.items()}
    (user_data / "Local State").write_text(json.dumps({"profile": {"info_cache": info, "last_used": last_used}}),
                                           encoding="utf-8")


def unpacked(path: Path, enabled: bool = True) -> dict:
    return {"location": chrome_setup.LOCATION_UNPACKED, "path": str(path), "state": 1 if enabled else 0}


def test_profiles_list_the_last_used_first_and_skip_folders_without_preferences(tmp_path):
    user_data = tmp_path / "User Data"
    write_profile(user_data, "Default", "work")
    write_profile(user_data, "Profile 2", "home")
    (user_data / "Profile 9").mkdir()  # no Preferences file: not a profile Chrome has used
    write_local_state(user_data, {"Default": "work", "Profile 2": "home", "Profile 9": "old"}, last_used="Profile 2")
    assert chrome_setup.list_profiles(user_data) == [("Profile 2", "home"), ("Default", "work")]


def test_profiles_fall_back_to_folder_names_without_local_state(tmp_path):
    user_data = tmp_path / "User Data"
    write_profile(user_data, "Default", "")
    assert chrome_setup.list_profiles(user_data) == [("Default", "Default")]


def test_an_entry_at_this_folder_counts_as_installed_and_enabled(tmp_path):
    ext = tmp_path / "BalloonLens" / "extension"
    prefs = tmp_path / "Default" / "Preferences"
    prefs.parent.mkdir(parents=True)
    prefs.write_text(json.dumps({"extensions": {"settings": {ID: unpacked(ext)}}}), encoding="utf-8")
    assert chrome_setup.extension_entry(prefs, ext) == {"samePath": True, "enabled": True}


def test_a_disabled_entry_is_reported_as_not_enabled(tmp_path):
    ext = tmp_path / "extension"
    prefs = tmp_path / "Preferences"
    prefs.write_text(json.dumps({"extensions": {"settings": {ID: unpacked(ext, enabled=False)}}}), encoding="utf-8")
    assert chrome_setup.extension_entry(prefs, ext) == {"samePath": True, "enabled": False}


def test_a_copy_loaded_from_another_folder_is_not_ours(tmp_path):
    prefs = tmp_path / "Preferences"
    prefs.write_text(json.dumps({"extensions": {"settings": {ID: unpacked(tmp_path / "old-copy")}}}), encoding="utf-8")
    assert chrome_setup.extension_entry(prefs, tmp_path / "extension") == {"samePath": False, "enabled": True}


def test_find_extension_reports_the_profile_that_has_it(tmp_path):
    user_data = tmp_path / "User Data"
    ext = tmp_path / "extension"
    write_profile(user_data, "Default", "work")
    write_profile(user_data, "Profile 1", "home", unpacked(ext))
    write_local_state(user_data, {"Default": "work", "Profile 1": "home"}, last_used="Default")
    assert chrome_setup.find_extension(user_data, ext) == {"profile": "Profile 1", "name": "home", "enabled": True}


def test_find_extension_says_when_only_another_copy_is_loaded(tmp_path):
    user_data = tmp_path / "User Data"
    write_profile(user_data, "Default", "work", unpacked(tmp_path / "old-copy"))
    assert chrome_setup.find_extension(user_data, tmp_path / "extension") == {"elsewhere": "Default"}


def test_find_extension_is_none_when_no_profile_has_it(tmp_path):
    user_data = tmp_path / "User Data"
    write_profile(user_data, "Default", "work")
    assert chrome_setup.find_extension(user_data, tmp_path / "extension") is None


def test_a_broken_preferences_file_is_treated_as_no_entry(tmp_path):
    prefs = tmp_path / "Preferences"
    prefs.write_text("{not json", encoding="utf-8")
    assert chrome_setup.extension_entry(prefs, tmp_path) is None


def test_the_extensions_page_opens_in_the_chosen_profile():
    args = chrome_setup.extensions_page_args(r"C:\Chrome\chrome.exe", "Profile 4")
    assert args == [r"C:\Chrome\chrome.exe", "--profile-directory=Profile 4", "chrome://extensions/"]
