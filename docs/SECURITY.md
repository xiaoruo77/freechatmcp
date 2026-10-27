# Security

This project can operate on local files, browsers, terminals and, when enabled, the Windows desktop. It is not a turnkey secure public remote-access service.

- Bind to 127.0.0.1 until authentication and network access controls are tested.
- Restrict workspaces and avoid granting entire drive access.
- Keep admin mode, desktop control, and arbitrary shell disabled by default.
- Create new per-installation bearer, OAuth, browser-bridge and tunnel secrets.
- Never commit local database files, browser profiles, user-specific configurations, logs, screenshots, certificates, .env or generated browser extension config.js.
- Treat task logs and acceptance test artifacts as potentially private.
- Review code and dependencies before running with elevated privileges.
- Rotate any previously disclosed keys. Removing keys from a Git commit does not revoke them.