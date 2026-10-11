"""Reads Chrome's own profile files (read-only) to tell whether the BalloonLens extension is loaded.

Chrome does not let another program add an unpacked extension silently, and the command-line switch that once did
(--load-extension) is ignored by current Chrome builds. So the setup window guides the user through the steps in
Chrome and uses this module to notice when they are done.
"""
import json
import os
from pathlib import Path

EXTENSION_ID = "pnccemlpjodglelglkjjdnnmdknejgop"  # fixed by the "key" field in extension/manifest.json
LOCATION_UNPACKED = 4  # Chrome's location code for "Load unpacked"
STATE_ENABLED = 1


def chrome_user_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def list_profiles(user_data: Path) -> list[tuple[str, str]]:
    """(folder, display name) for each profile that has a Preferences file, the last used one first."""
    info, last_used = {}, None
    try:
        local = _load_json(user_data / "Local State")
        info = local.get("profile", {}).get("info_cache", {}) or {}
        last_used = local.get("profile", {}).get("last_used")
    except (OSError, ValueError, AttributeError):
        pass
    if not user_data.is_dir():
        return []
    folders = [p.name for p in user_data.iterdir()
               if (p.name == "Default" or p.name.startswith("Profile ")) and (p / "Preferences").is_file()]
    names = {folder: (info.get(folder) or {}).get("name") or folder for folder in folders}
    ordered = sorted(folders, key=lambda folder: (folder != last_used, folder))
    return [(folder, names[folder]) for folder in ordered]


def _same_folder(path_text: str, folder: Path) -> bool:
    if not path_text:
        return False
    return os.path.normcase(os.path.abspath(path_text)) == os.path.normcase(os.path.abspath(str(folder)))


def extension_entry(prefs: Path, ext_dir: Path) -> dict | None:
    """The BalloonLens entry in one profile: {'samePath', 'enabled'}, or None when the profile has no such entry."""
    try:
        settings = _load_json(prefs).get("extensions", {}).get("settings", {})
    except (OSError, ValueError, AttributeError):
        return None
    entry = settings.get(EXTENSION_ID)
    if not isinstance(entry, dict) or entry.get("location") != LOCATION_UNPACKED:
        return None
    return {"samePath": _same_folder(entry.get("path", ""), ext_dir), "enabled": entry.get("state") == STATE_ENABLED}


def find_extension(user_data: Path, ext_dir: Path) -> dict | None:
    """Where this extension folder is loaded: {'profile', 'name', 'enabled'}; or {'elsewhere': folder} when another
    copy is loaded; or None when no profile has it."""
    elsewhere = None
    for folder, name in list_profiles(user_data):
        entry = extension_entry(user_data / folder / "Preferences", ext_dir)
        if entry is None:
            continue
        if entry["samePath"]:
            return {"profile": folder, "name": name, "enabled": entry["enabled"]}
        elsewhere = elsewhere or folder
    return {"elsewhere": elsewhere} if elsewhere else None


def extensions_page_args(chrome_exe: str, profile_dir: str) -> list[str]:
    """Opens the extensions page in the chosen profile. Chrome may ignore the address when it starts cold,
    so the window also copies the address to the clipboard."""
    return [chrome_exe, f"--profile-directory={profile_dir}", "chrome://extensions/"]
