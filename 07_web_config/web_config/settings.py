from __future__ import annotations

import os
import json
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _default_home() -> Path:
    # settings.py -> web_config -> 07_web_config -> project root
    return Path(__file__).resolve().parents[2]


def _csv_env(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


_WINDOWS_MCP_DEFAULT_ARGS = [
    "--python", "3.14", "windows-mcp==0.8.8", "serve", "--transport", "stdio",
    "--tools", "Snapshot,Screenshot,Click,Type,Shortcut,Scroll,App,ControlStatus",
]
_WINDOWS_MCP_KEYS = frozenset({"enabled", "control_enabled", "transport", "url", "token_file", "timeout", "command", "args"})


def _desktop_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Windows-MCP configuration must not repeat option keys")
        result[key] = value
    return result


def _desktop_value(key: str, value: Any) -> Any:
    """Validate only desktop options, with errors containing no supplied value."""

    if key in {"enabled", "control_enabled"}:
        if type(value) is not bool:
            raise ValueError("Windows-MCP enabled/control_enabled must be JSON booleans")
    elif key == "transport":
        if value not in ("stdio", "streamable-http") or not isinstance(value, str):
            raise ValueError("Windows-MCP transport must be stdio or streamable-http")
    elif key == "timeout":
        if type(value) is not int or not 1 <= value <= 120:
            raise ValueError("Windows-MCP timeout must be an integer from 1 to 120")
    elif key == "args":
        if not isinstance(value, list) or any(not isinstance(item, str) or "\x00" in item for item in value):
            raise ValueError("Windows-MCP args must be an array of strings without NUL characters")
        value = list(value)
    elif key == "command":
        if not isinstance(value, str) or not value.strip() or any(char in value for char in "\r\n\x00"):
            raise ValueError("Windows-MCP command must be nonempty text without control characters")
        value = value.strip()
    elif key == "url":
        if not isinstance(value, str):
            raise ValueError("Windows-MCP url must be text")
        if value:
            try:
                parsed = urlsplit(value)
                valid = (
                    parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "::1"}
                    and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                    and parsed.port != 0 and not any(char.isspace() or ord(char) < 32 for char in value)
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Windows-MCP url must be a literal loopback HTTP endpoint without credentials, query or fragment") from None
    elif key == "token_file":
        if value is not None:
            if not isinstance(value, str) or not value or any(char in value for char in "\r\n\x00"):
                raise ValueError("Windows-MCP token_file must be an absolute path or null")
            try:
                path = Path(value)
                if not path.is_absolute():
                    raise ValueError
            except (ValueError, OSError):
                raise ValueError("Windows-MCP token_file must be an absolute path or null") from None
            value = path
    return value


def _load_windows_mcp_options(project_root: Path) -> dict[str, Any]:
    """Read startup options; environment overrides the dedicated local file.

    The file contains a token-file reference, never a token. This loader does
    not read the token, start a process, or create/modify configuration files.
    """

    options: dict[str, Any] = {
        "enabled": False, "control_enabled": False, "transport": "stdio", "url": "",
        "token_file": None, "timeout": 30, "command": "uvx", "args": list(_WINDOWS_MCP_DEFAULT_ARGS),
    }
    path = project_root / "07_web_config" / "windows_mcp.json"
    try:
        exists = path.exists()
        if exists:
            if path.stat().st_size > 65_536:
                raise ValueError("Windows-MCP configuration is too large")
            config = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_desktop_object)
            if not isinstance(config, dict) or set(config) - _WINDOWS_MCP_KEYS:
                raise ValueError("windows_mcp.json accepts only documented Windows-MCP options")
            for key, value in config.items():
                options[key] = _desktop_value(key, value)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Cannot read valid Windows-MCP configuration JSON") from None
    for key in _WINDOWS_MCP_KEYS:
        env_name = "WEB_AGENT_WINDOWS_MCP_" + ("ARGS_JSON" if key == "args" else key.upper())
        if env_name not in os.environ:
            continue
        raw = os.environ[env_name]
        if key in {"enabled", "control_enabled"}:
            normalized = raw.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                value = True
            elif normalized in {"false", "0", "no", "off"}:
                value = False
            else:
                raise ValueError("Windows-MCP boolean environment values must be true or false")
        elif key == "timeout":
            if not raw.isascii() or not raw.strip().isdigit() or len(raw.strip()) > 3:
                raise ValueError("Windows-MCP timeout environment value must be an integer from 1 to 120")
            value = int(raw.strip())
        elif key == "args":
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ValueError("Windows-MCP args environment value must be a JSON array of strings") from None
        elif key == "token_file":
            value = raw if raw else None
        else:
            value = raw
        options[key] = _desktop_value(key, value)
    if options["transport"] == "streamable-http" and (not options["url"] or options["token_file"] is None):
        raise ValueError("Windows-MCP HTTP requires a loopback url and an absolute token_file path")
    return options


@dataclass(slots=True)
class Settings:
    project_root: Path
    display_name: str
    host: str
    port: int
    workspace_root: Path
    allowed_workspaces: list[Path]
    token: str
    token_file: Path
    admin_mode: bool
    embedded_worker: bool
    log_level: str
    cors_origins: list[str]
    mcp_allowed_hosts: list[str]
    mcp_allowed_origins: list[str]
    oauth_enabled: bool
    oauth_issuer_url: str
    oauth_resource_url: str
    oauth_scopes: list[str]
    oauth_allowed_redirect_hosts: list[str]
    wsl_distribution: str | None
    allow_custom_wsl_workflow: bool
    allow_custom_shell_workflow: bool
    task_poll_interval: float
    default_task_timeout: int
    database_path: Path
    data_root: Path
    log_root: Path
    task_output_root: Path
    generated_script_root: Path
    browser_bridge_url: str
    browser_bridge_token_file: Path
    browser_profile_dir: Path
    frontend_dist: Path
    extra: dict[str, Any] = field(default_factory=dict)
    # Keep the deployed behavior until the opt-in direct path is accepted.
    orchestration_mode: str = "legacy_sampling"
    windows_mcp_enabled: bool = False
    windows_mcp_control_enabled: bool = False
    windows_mcp_transport: str = "stdio"
    windows_mcp_command: str = "uvx"
    windows_mcp_args: list[str] = field(default_factory=lambda: list(_WINDOWS_MCP_DEFAULT_ARGS))
    windows_mcp_url: str = ""
    windows_mcp_token_file: Path | None = None
    windows_mcp_timeout: int = 30

    @classmethod
    def load(cls) -> "Settings":
        project_root = Path(os.getenv("WEB_AGENT_HOME", str(_default_home()))).expanduser().resolve()
        mode_file = project_root / "07_web_config" / "orchestration.json"
        mode_default = "legacy_sampling"
        if mode_file.is_file():
            config = json.loads(mode_file.read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict) or set(config) - {"orchestration_mode"}:
                raise ValueError("orchestration.json accepts only orchestration_mode")
            mode_default = config.get("orchestration_mode", mode_default)
        mode = os.getenv("WEB_AGENT_ORCHESTRATION_MODE", mode_default).strip().lower()
        if mode not in {"legacy_sampling", "chatgpt_direct"}:
            raise ValueError("WEB_AGENT_ORCHESTRATION_MODE must be chatgpt_direct or legacy_sampling")
        desktop = _load_windows_mcp_options(project_root)
        data_root = Path(os.getenv("WEB_AGENT_DATA", str(project_root / "08_web_data"))).expanduser().resolve()
        log_root = Path(os.getenv("WEB_AGENT_LOGS", str(project_root / "09_web_logs"))).expanduser().resolve()
        workspace_root = Path(os.getenv("WEB_AGENT_WORKSPACE", str(project_root))).expanduser().resolve()
        configured_workspaces = os.getenv("WEB_AGENT_WORKSPACES", "")
        allowed = [workspace_root]
        if configured_workspaces.strip():
            allowed = [Path(item.strip()).expanduser().resolve() for item in configured_workspaces.split(";") if item.strip()]
            if workspace_root not in allowed:
                allowed.insert(0, workspace_root)

        data_root.mkdir(parents=True, exist_ok=True)
        (data_root / "task_outputs").mkdir(parents=True, exist_ok=True)
        (data_root / "generated_scripts").mkdir(parents=True, exist_ok=True)
        log_root.mkdir(parents=True, exist_ok=True)

        browser_bridge_host = os.getenv("WEB_AGENT_BROWSER_BRIDGE_HOST", "127.0.0.1").strip() or "127.0.0.1"
        browser_bridge_port = _env_int("WEB_AGENT_BROWSER_BRIDGE_PORT", 48272)
        browser_bridge_token_file = data_root / "browser_bridge.token"
        browser_profile_dir = Path(
            os.getenv("WEB_AGENT_BROWSER_PROFILE", str(data_root / "browser_profile"))
        ).expanduser().resolve()
        if not browser_bridge_token_file.exists() or not browser_bridge_token_file.read_text(encoding="utf-8").strip():
            browser_bridge_token_file.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")

        token_file = data_root / "web_agent.token"
        token = os.getenv("WEB_AGENT_TOKEN", "").strip()
        if not token:
            if token_file.exists():
                token = token_file.read_text(encoding="utf-8").strip()
            if not token:
                token = secrets.token_urlsafe(32)
                token_file.write_text(token + "\n", encoding="utf-8")
        try:
            if token_file.exists() and sys.platform != "win32":
                token_file.chmod(0o600)
        except OSError:
            pass

        origins = [x.strip() for x in os.getenv("WEB_AGENT_CORS_ORIGINS", "http://127.0.0.1:48271,http://localhost:5173").split(",") if x.strip()]
        public_hostnames = _csv_env(
            "WEB_AGENT_PUBLIC_HOSTNAMES",
            "mcp.example.com,mcp.example.com,mcp.example.com",
        )
        mcp_allowed_hosts = _csv_env(
            "WEB_AGENT_MCP_ALLOWED_HOSTS",
            ",".join(
                [
                    "127.0.0.1:*",
                    "localhost:*",
                    "[::1]:*",
                    *public_hostnames,
                    *(f"{hostname}:*" for hostname in public_hostnames),
                ]
            ),
        )
        mcp_allowed_origins = _csv_env(
            "WEB_AGENT_MCP_ALLOWED_ORIGINS",
            ",".join(
                [
                    "http://127.0.0.1:*",
                    "http://localhost:*",
                    "http://[::1]:*",
                    *(f"https://{hostname}" for hostname in public_hostnames),
                    *(f"https://{hostname}:*" for hostname in public_hostnames),
                ]
            ),
        )
        public_url = os.getenv("WEB_AGENT_PUBLIC_URL", "https://mcp.example.com").strip().rstrip("/")
        oauth_issuer_url = os.getenv("WEB_AGENT_OAUTH_ISSUER_URL", public_url).strip().rstrip("/")
        oauth_resource_url = os.getenv(
            "WEB_AGENT_MCP_RESOURCE_URL",
            f"{public_url}/mcp",
        ).strip().rstrip("/")
        wsl_distribution = os.getenv("WEB_AGENT_WSL_DISTRIBUTION", "").strip() or None
        settings = cls(
            project_root=project_root,
            display_name=os.getenv("WEB_AGENT_DISPLAY_NAME", "Xiaoruo1").strip() or "Xiaoruo1",
            host=os.getenv("WEB_AGENT_HOST", "127.0.0.1"),
            port=_env_int("WEB_AGENT_PORT", 48271),
            workspace_root=workspace_root,
            allowed_workspaces=allowed,
            token=token,
            token_file=token_file,
            admin_mode=_env_bool("WEB_AGENT_ADMIN_MODE", True),
            embedded_worker=_env_bool("WEB_AGENT_EMBEDDED_WORKER", False),
            log_level=os.getenv("WEB_AGENT_LOG_LEVEL", "INFO").upper(),
            cors_origins=origins,
            mcp_allowed_hosts=mcp_allowed_hosts,
            mcp_allowed_origins=mcp_allowed_origins,
            oauth_enabled=_env_bool("WEB_AGENT_OAUTH_ENABLED", False),
            oauth_issuer_url=oauth_issuer_url,
            oauth_resource_url=oauth_resource_url,
            oauth_scopes=_csv_env("WEB_AGENT_OAUTH_SCOPES", "web_agent"),
            oauth_allowed_redirect_hosts=_csv_env(
                "WEB_AGENT_OAUTH_ALLOWED_REDIRECT_HOSTS",
                "chatgpt.com,localhost,127.0.0.1",
            ),
            wsl_distribution=wsl_distribution,
            allow_custom_wsl_workflow=_env_bool("WEB_AGENT_ALLOW_CUSTOM_WSL_WORKFLOW", True),
            allow_custom_shell_workflow=_env_bool("WEB_AGENT_ALLOW_CUSTOM_SHELL_WORKFLOW", True),
            task_poll_interval=max(0.2, float(os.getenv("WEB_AGENT_TASK_POLL_INTERVAL", "1"))),
            default_task_timeout=max(1, _env_int("WEB_AGENT_TASK_TIMEOUT", 3600)),
            database_path=data_root / "web_agent.sqlite3",
            data_root=data_root,
            log_root=log_root,
            task_output_root=data_root / "task_outputs",
            generated_script_root=data_root / "generated_scripts",
            browser_bridge_url=f"http://{browser_bridge_host}:{browser_bridge_port}",
            browser_bridge_token_file=browser_bridge_token_file,
            browser_profile_dir=browser_profile_dir,
            frontend_dist=project_root / "01_web_frontend" / "dist",
            orchestration_mode=mode,
            windows_mcp_enabled=desktop["enabled"],
            windows_mcp_control_enabled=desktop["control_enabled"],
            windows_mcp_transport=desktop["transport"],
            windows_mcp_command=desktop["command"],
            windows_mcp_args=desktop["args"],
            windows_mcp_url=desktop["url"],
            windows_mcp_token_file=desktop["token_file"],
            windows_mcp_timeout=desktop["timeout"],
        )
        return settings