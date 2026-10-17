"""Build a source install ZIP without local models, tokens, caches, browser profiles or subscription logins."""
import json
import re
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ('setup_windows.ps1', 'install_windows.ps1', 'install_windows.cmd', 'uninstall_windows.ps1',
         'test_setup_windows.ps1', 'test_extension.cjs', 'test_reading.cjs', 'test_editor.cjs', 'test_subscription_ui.cjs',
         'e2e_extension.py', 'e2e_convenience.py', 'e2e_regions.py', 'e2e_subscription.py', 'e2e_subscription_ui.py',
         'e2e_subscription_live.py', 'e2e_subscription_auto.py', 'e2e_page.html',
         'review_compare.py', 'blind_eval.py',
         'setup_gui.py', 'test_setup_gui.py', 'build_setup_exe.ps1', 'chrome_setup.py', 'test_chrome_setup.py',
         'desktop_launcher.py', 'build_desktop_exe.ps1')
# The one-file setup window, built by tools/build_setup_exe.ps1. It sits in the ZIP root next to Setup.cmd.
SETUP_EXE = ROOT / 'release' / 'BalloonLens-Setup.exe'
DESKTOP_EXE = ROOT / 'release' / 'BalloonLens.exe'
# Nothing with these names may end up in the ZIP: local secrets, caches, runtime folders and CLI logins.
FORBIDDEN = ('.venv/', '__pycache__/', 'token.txt', 'cache.sqlite', 'bench_out/', 'profile/', 'claude-config/',
             'runtime/', 'metrics.jsonl', 'helper.log', 'auth.json', '.credentials.json')
REQUIRED = {'Setup.cmd', 'BalloonLens.cmd', 'BalloonLens-Setup.exe', 'BalloonLens.exe',
            'tools/setup_windows.ps1', 'tools/setup_gui.py', 'tools/desktop_launcher.py',
            'helper/balloonlens/desktop.py', 'helper/balloonlens/desktop_client.py',
            'helper/balloonlens/desktop_windows.py', 'helper/balloonlens/desktop_view.py',
            'extension/manifest.json', 'extension/reading.js',
            'helper/balloonlens/presentation.py', 'helper/balloonlens/backends/claude_cli.py',
            'helper/balloonlens/backends/page.py', 'helper/balloonlens/backends/warm.py',
            'helper/balloonlens/backends/subscription_usage.py',
            'helper/requirements.txt', 'helper/tests/fixtures/fake_claude.py',
            'tools/e2e_subscription.py', 'tools/blind_eval.py'}


def collect() -> list[Path]:
    files = [ROOT / p for p in ('Setup.cmd', 'BalloonLens.cmd', 'README.md', 'QUALITY_VALIDATION.md', 'VALIDATION.md', 'pytest.ini')]
    for folder, pattern in [('extension', '**/*'), ('helper/balloonlens', '*.py'),
                            ('helper/balloonlens/backends', '*.py'), ('helper/native', '*.bat'),
                            ('helper/tests', '*.py'), ('helper/tests/fixtures', '*.py'), ('samples/synthetic', '*')]:
        files.extend(p for p in (ROOT / folder).glob(pattern) if p.is_file())
    files.extend((ROOT / 'helper').glob('requirements*.txt'))
    files.extend(ROOT / 'tools' / name for name in TOOLS)
    return sorted({f for f in files if f.is_file()})


def check_versions():
    manifest = json.loads((ROOT / 'extension/manifest.json').read_text(encoding='utf-8'))['version']
    config = re.search(r'^VERSION = "([^"]+)"', (ROOT / 'helper/balloonlens/config.py').read_text(encoding='utf-8'),
                       re.M).group(1)
    assert manifest == config, f'manifest {manifest} differs from helper config {config}'
    return manifest


def main():
    version = check_versions()
    out = ROOT / 'release' / f'BalloonLens-{version}-{time.time_ns()}.zip'
    out.parent.mkdir(exist_ok=True)
    files = collect()
    assert SETUP_EXE.is_file(), 'build tools/build_setup_exe.ps1 first: the ZIP needs the setup window'
    assert DESKTOP_EXE.is_file(), 'build tools/build_desktop_exe.ps1 first: the ZIP needs the desktop launcher'
    with zipfile.ZipFile(out, 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(ROOT).as_posix())
        archive.write(SETUP_EXE, 'BalloonLens-Setup.exe')
        archive.write(DESKTOP_EXE, 'BalloonLens.exe')
    with zipfile.ZipFile(out) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        missing = REQUIRED - names
        assert not missing, f'missing from package: {sorted(missing)}'
        leaked = sorted(n for n in names if any(part in n for part in FORBIDDEN))
        assert not leaked, f'forbidden files in package: {leaked}'
    print(f'{out}\nVerified {len(names)} files for version {version}; {out.stat().st_size} bytes')


if __name__ == '__main__':
    main()
