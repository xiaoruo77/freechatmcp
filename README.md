# FreeChatMCP

Open-source bridge that lets a supported web-chat AI client control a local Windows computer through MCP, without purchasing a separate LLM API. ChatGPT orchestrates calls in the conversation, while local workers perform explicit tool operations, without needing an extra model API subscription. The web-chat account may still require a paid plan, remain subject to usage limits, and must support MCP/app tool connections.

**Web chat is the brain; your computer is the worker. No extra LLM API required.**

This is not an API bypass, a way to obtain unlimited AI calls, or a zero-cost guarantee. It uses supported client-side integration and user-authorized tool calls.

## v0.3.0 — Windows background workspace and verifiable actions

- **Optional background desktop** for supported Windows applications. Separate Edge instances and supported UI Automation controls reduce interference with the user's foreground. WPS Writer uses a private window and verifies edits and Save As; no promise is made for all applications, GPU surfaces, Office variants or Windows installations.
- **Better browser operation receipts.** Navigation, downloads, forms and uploads distinguish dispatch, pending/uncertain state and observed results. Look up the receipt before retrying an operation.
- **Python and Notebook isolation.** Choose a configured interpreter for each project and run notebooks in a separate kernel. The input notebook is never overwritten by the notebook executor. User code can still have side effects.
- **Evidence-based host/worker coordination.** Persist the goal, checkpoints and explicit acceptance; reread actual worker output; do not turn an uncertain effect into success or reissue it automatically.

[v0.2.0](https://github.com/xiaoruo77/freechatmcp/releases/tag/v0.2.0) introduced unified browser control, native IPC and uploads. [v0.1.0](https://github.com/xiaoruo77/freechatmcp/releases/tag/v0.1.0) is the original connection/deployment snapshot.

## Components

- 02_web_mcp: FastAPI/MCP endpoints, OAuth and authorization
- 03_web_worker: deterministic task worker and status reporting
- 06_web_tools: file, browser, shell, Git and optional desktop control tools
- 01_web_frontend: React control panel
- 04_web_canvas and 05_web_memory: local canvas, history and memory
- 07_web_config: shared Python configuration code
- 10_web_deploy: optional Windows services, Edge extension and local deployment scripts
- tests: automated tests

## Development setup

Requires Windows, an appropriate Python installation, Node.js/npm for the frontend, and optional third-party runtimes for desktop features.

1. Clone the GitHub repository xiaoruo77/freechatmcp.
2. Run python -m venv .venv in the project directory.
3. Install packages using .venv/Scripts/python.exe -m pip install -e ".[dev]".
4. In 01_web_frontend, run npm install and npm run build.
5. Start run_server.py and run_worker.py using the virtual environment's Python executable.
6. Local UI and MCP endpoint default to 127.0.0.1:48271. The default file workspace is the separate `workspace/` directory.
7. See [optional Windows background setup](docs/BACKGROUND_WINDOWS.md).

This repository provides source code only. Python/Node environments, third-party Windows-MCP components, cloudflared configuration, browser sessions and installed credentials are not bundled.

## Security and configuration

Read .env.example and docs/SECURITY.md. Supply actual values in your process environment; .env.example is not auto-loaded. Default to loopback host, least-privilege workspaces, disabled admin/desktop and shell permissions. Configure and verify any OAuth, public tunnel and browser bridge secrets independently; never publish them. Edge extension config.js is generated locally and ignored by Git.

This is a powerful local automation tool; do not expose it on the internet without correctly configured authentication and access controls.

## License

MIT; see LICENSE. Third-party dependencies remain under their own licenses.