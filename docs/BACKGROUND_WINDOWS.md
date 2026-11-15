# Optional Windows background desktop

The core MCP server and worker can run without desktop automation. Background desktop usage requires an interactive Windows user session, the included Python dependencies and compatible installed applications. It does not require WSL. Background is not a security sandbox and cannot make all applications automatable.

1. Install into the project's `.venv` as in the [README](../README.md). Keep the main server and background service on loopback.
2. Create an independent random token in a file whose Windows ACL only allows the operator and intended service account. Use the same private token file for the background client and service. Do not paste the token into chat or commit or log it.
3. Copy [background_desktop.example.json](../07_web_config/background_desktop.example.json) to the ignored local `07_web_config/background_desktop.json`. Supply absolute local paths for the token and data directory. Add your own `allowed_apps` entries (for example `edge` and `wps` with the installed executable paths); leave absent apps unconfigured. Never edit files to point at someone else's installation.
4. From the logged-in user's session, start the helper using the project environment and verify `background_desktop(action="status")` before using it:

   ```powershell
   .\.venv\Scripts\python.exe -m web_tools.background_desktop_service --config .\07_web_config\background_desktop.json
   ```

5. Keep the default foreground policy (`isolated_only`). State-changing desktop operations require admin mode **and** explicit per-call confirmation with your authorization. Test with an empty browser profile and a disposable document. Only configure a small set of workspaces, not whole drives. Cleanup targets only the private desktop; unsaved-document prompts may require manual handling.

Desktop UI Automation, WPS Writer (with the required native document interface), and browser background actions are application-dependent. A dispatched action needs a separate observed result. Unsupported or uncertain paths are not proof of success. No personal remote receiver installer, account data, or browser binary is provided.
