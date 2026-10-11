"""Chrome native messaging host: lets the extension start the helper on demand.

Registered per user by tools/install_windows.ps1; Chrome runs helper/native/balloonlens_host.bat for each message.
Messages: {"cmd": "ping"} -> {"ok", "running"}   {"cmd": "start"} -> {"ok", "state": "running"|"started"}
"""
import json
import socket
import struct
import subprocess
import sys
from pathlib import Path

from . import config

HELPER_DIR = Path(__file__).resolve().parents[1]
PROTOCOL = 1


def read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (length,) = struct.unpack("<I", raw)
    return json.loads(sys.stdin.buffer.read(length) or b"{}")


def write_message(obj):
    data = json.dumps(obj).encode()
    sys.stdout.buffer.write(struct.pack("<I", len(data)) + data)
    sys.stdout.buffer.flush()


def helper_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((config.HOST, config.PORT)) == 0


def launch():
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    args = [str(pythonw if pythonw.exists() else exe), "-m", "balloonlens", "--background"]
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        # Chrome kills the host's job object when it exits; break away so the helper keeps running.
        subprocess.Popen(args, cwd=HELPER_DIR, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        return
    except OSError:
        pass
    # Breakaway not permitted: WMI creates the process outside Chrome's job.
    cmdline = subprocess.list2cmdline(args).replace("'", "''")
    cwd = str(HELPER_DIR).replace("'", "''")
    script = (f"Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
              f"-Arguments @{{CommandLine='{cmdline}'; CurrentDirectory='{cwd}'}} | Out-Null")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True,
                   capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)


def main():
    msg = read_message() or {}
    try:
        if msg.get("cmd") == "ping":
            write_message({"ok": True, "protocol": PROTOCOL, "running": helper_running()})
        elif msg.get("cmd") == "start":
            if helper_running():
                write_message({"ok": True, "state": "running"})
            else:
                launch()
                write_message({"ok": True, "state": "started"})
        else:
            write_message({"ok": False, "error": "unknown_command"})
    except Exception as e:  # always answer, or Chrome reports a generic host error
        write_message({"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})


if __name__ == "__main__":
    main()
