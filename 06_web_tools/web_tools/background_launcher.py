"""Start the idle-expiring backend in the current user session, without a UI."""
from pathlib import Path
import subprocess
import sys


def main():
    root=Path(__file__).resolve().parents[2]
    pythonw=root/'.venv'/'Scripts'/'pythonw.exe'
    config=root/'07_web_config'/'background_desktop.json'
    if not pythonw.is_file() or not config.is_file():raise SystemExit(2)
    subprocess.Popen([str(pythonw),'-m','web_tools.background_desktop_service','--config',str(config)],
        cwd=str(root),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0),close_fds=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
