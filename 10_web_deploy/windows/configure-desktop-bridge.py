"""Configure the local user-session desktop bridge without exposing credentials.

Use --enable or --disable; with no flag this only inspects effective settings.
The MCP service must reload settings after a change. Existing OAuth is retained.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def secure_token(path: Path):
    import secrets
    import win32api
    import win32security
    import ntsecuritycon
    if not path.exists():
        with path.open("x", encoding="utf-8") as output:
            output.write(secrets.token_urlsafe(48) + "\n")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 20 or any(character.isspace() for character in token):
        raise RuntimeError("Existing desktop credential is invalid; it was preserved")
    user = win32security.GetTokenInformation(win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY), win32security.TokenUser)[0]
    system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    acl = win32security.ACL()
    for sid in (user, system):
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
    win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION, None, None, acl, None)


def shortcut(remove=False):
    import win32com.client
    shell = win32com.client.Dispatch("WScript.Shell")
    path = Path(shell.SpecialFolders("Startup")) / "Xiaoruo1 Desktop Bridge.lnk"
    target = ROOT / ".venv/Scripts/pythonw.exe"
    if path.exists() and Path(shell.CreateShortcut(str(path)).TargetPath).resolve() != target.resolve():
        raise RuntimeError("Unrelated startup shortcut preserved")
    if remove:
        path.unlink(missing_ok=True)
        return str(path)
    item = shell.CreateShortcut(str(path))
    item.TargetPath = str(target)
    item.Arguments = '"' + str(ROOT / "10_web_deploy/windows/run_windows_mcp_bridge.py") + '"'
    item.WorkingDirectory = str(ROOT)
    item.WindowStyle = 7
    item.Description = "Xiaoruo1 authenticated loopback desktop bridge in logged-in user session"
    item.Save()
    return str(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--enable", action="store_true")
    mode.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    config_file = ROOT / "07_web_config/windows_mcp.json"
    launcher = ROOT / "10_web_deploy/windows/run_windows_mcp_bridge.py"
    if not args.enable and not args.disable:
        config = json.loads(config_file.read_text(encoding="utf-8-sig")) if config_file.exists() else {"enabled": False, "control_enabled": False}
        print(json.dumps({"configuration": config, "secrets_disclosed": False}, ensure_ascii=False))
        return
    token_file = ROOT / "08_web_data/windows_mcp.token"
    if args.enable:
        if not (ROOT / "10_web_deploy/windows/windows_mcp_runtime/venv/Scripts/python.exe").is_file():
            raise RuntimeError("Install the isolated Windows-MCP runtime first")
        secure_token(token_file)
    config = {"enabled": args.enable, "control_enabled": args.enable, "transport": "streamable-http",
              "url": "http://127.0.0.1:48888/mcp", "token_file": str(token_file), "timeout": 30}
    if config_file.exists():
        shutil.copyfile(config_file, config_file.with_name(config_file.name + "." + datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak"))
    temporary = config_file.with_suffix(".json.new")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(config_file)
    startup = shortcut(remove=args.disable)
    if args.enable:
        subprocess.Popen([str(ROOT / ".venv/Scripts/pythonw.exe"), str(launcher)], cwd=str(ROOT),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        subprocess.run([sys.executable, str(launcher), "--stop"], check=False)
    print(json.dumps({"enabled": args.enable, "endpoint": config["url"], "startup_shortcut": startup,
                      "mcp_settings_reload_required": True, "credentials_printed": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()