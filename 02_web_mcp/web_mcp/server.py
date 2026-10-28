from __future__ import annotations

import asyncio
import html
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.provider import ProviderTokenVerifier, RegistrationError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.authentication import AuthenticationMiddleware

from web_canvas.service import CanvasService
from web_config.settings import Settings
from web_logs.logger import audit, configure_logging
from web_memory.repository import ConversationRepository
from web_memory.store import Database
from web_worker.service import WorkerService

from .auth import extract_token, require_request_token, valid_token
from .mcp_server import build_mcp, configure_registry, release_registry
from .oauth import LocalOAuthProvider
from .tool_registry import ToolContext, ToolRegistry


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=100_000)
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None


class ToolInvocation(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class TaskRequest(BaseModel):
    profile: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CanvasCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=5_000_000)
    kind: str = Field(default="markdown", max_length=50)
    status: str = Field(default="draft", max_length=50)


class CanvasUpdate(BaseModel):
    content: str = Field(max_length=5_000_000)
    title: str | None = Field(default=None, max_length=200)
    kind: str | None = Field(default=None, max_length=50)
    status: str | None = Field(default=None, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AppState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = configure_logging(settings.log_root, settings.log_level)
        self.db = Database(settings.database_path, settings.project_root / "08_web_data" / "schema.sql")
        self.canvas: CanvasService | None = None
        self.registry: ToolRegistry | None = None
        self.conversations: ConversationRepository | None = None
        self.worker: WorkerService | None = None
        self.worker_task: asyncio.Task | None = None
        self.oauth_provider: LocalOAuthProvider | None = None


def get_state(request: Request) -> AppState:
    return request.app.state.web_agent


async def require_auth(request: Request) -> dict[str, Any]:
    state = get_state(request)
    if valid_token(request, state.settings.token):
        return {"mode": "admin"}
    supplied = extract_token(request)
    if state.oauth_provider and supplied:
        access_token = await state.oauth_provider.load_access_token(supplied)
        if access_token:
            return {
                "mode": "oauth",
                "client_id": access_token.client_id,
                "scopes": access_token.scopes,
            }
    return require_request_token(request, state.settings.token)


def get_registry(request: Request) -> ToolRegistry:
    state = get_state(request)
    if state.registry is None:
        raise HTTPException(status_code=503, detail="Application is still starting")
    return state.registry


def _parse_chat_tool(message: str) -> tuple[str | None, dict[str, Any] | None]:
    # A lightweight local command format keeps the UI useful without requiring an LLM provider.
    if not message.startswith("/tool "):
        return None, None
    remainder = message[6:].strip()
    if not remainder:
        return None, None
    pieces = remainder.split(" ", 1)
    name = pieces[0]
    if len(pieces) == 1:
        return name, {}
    try:
        value = json.loads(pieces[1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"/tool JSON arguments invalid: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("/tool arguments must be a JSON object")
    return name, value


def _mcp_transport_security(settings: Settings) -> TransportSecuritySettings:
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.mcp_allowed_hosts,
        allowed_origins=settings.mcp_allowed_origins,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or Settings.load()
    oauth_provider: LocalOAuthProvider | None = None
    oauth_settings: AuthSettings | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state: AppState = app.state.web_agent
        await state.db.init()
        state.canvas = CanvasService(state.db)
        state.conversations = ConversationRepository(state.db)
        state.registry = ToolRegistry(ToolContext(runtime_settings, state.db, state.canvas, state.logger))
        previous_registry = configure_registry(state.registry)
        if runtime_settings.embedded_worker:
            state.worker = WorkerService(runtime_settings, state.db, state.logger)
            state.worker_task = asyncio.create_task(state.worker.run_forever(), name="web-agent-embedded-worker")
        audit(state.logger, "web_mcp_started", host=runtime_settings.host, port=runtime_settings.port, workspace=str(runtime_settings.workspace_root))
        # FastMCP's Streamable HTTP app owns a session manager even in stateless mode.
        try:
            async with mcp_instance.session_manager.run():
                yield
        finally:
            if state.worker_task:
                state.worker.stop() if state.worker else None
                state.worker_task.cancel()
                await asyncio.gather(state.worker_task, return_exceptions=True)
            if state.registry is not None:
                await state.registry.direct_tools.desktop.close()
                release_registry(state.registry, previous_registry)
            audit(state.logger, "web_mcp_stopped")
            await state.db.close()


    app = FastAPI(title=runtime_settings.display_name, version="0.2.0", lifespan=lifespan)
    app.state.web_agent = AppState(runtime_settings)
    if runtime_settings.oauth_enabled:
        oauth_provider = LocalOAuthProvider(runtime_settings, app.state.web_agent.db)
        oauth_settings = AuthSettings(
            issuer_url=runtime_settings.oauth_issuer_url,
            resource_server_url=runtime_settings.oauth_resource_url,
            required_scopes=runtime_settings.oauth_scopes,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                default_scopes=runtime_settings.oauth_scopes,
                valid_scopes=runtime_settings.oauth_scopes,
            ),
        )
        app.state.web_agent.oauth_provider = oauth_provider
        # FastMCP's auth middleware normally lives inside its Starlette app. This
        # application mounts the SDK routes directly, so install the same
        # middleware on the parent FastAPI app to preserve authenticated user
        # context for the mounted Streamable HTTP routes.
        app.add_middleware(AuthContextMiddleware)
        app.add_middleware(
            AuthenticationMiddleware,
            backend=BearerAuthBackend(ProviderTokenVerifier(oauth_provider)),
        )
    mcp_instance = build_mcp(
        _mcp_transport_security(runtime_settings),
        auth=oauth_settings,
        auth_server_provider=oauth_provider,
        orchestration_mode=runtime_settings.orchestration_mode,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Web-Agent-Token"],
    )

    @app.middleware("http")
    async def protect_mcp(request: Request, call_next):
        if any(request.url.path == path or request.url.path.startswith(path + "/") for path in ("/mcp", "/sse", "/messages")):
            state: AppState = request.app.state.web_agent
            if not state.settings.oauth_enabled and not valid_token(request, state.settings.token):
                return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": f"Valid {runtime_settings.display_name} token required"})
        return await call_next(request)

    def oauth_metadata() -> dict[str, Any]:
        if not oauth_provider:
            raise HTTPException(status_code=404, detail="OAuth is disabled")
        issuer = runtime_settings.oauth_issuer_url
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "registration_endpoint": f"{issuer}/register",
            "scopes_supported": runtime_settings.oauth_scopes,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
        }

    def protected_resource_metadata() -> dict[str, Any]:
        if not oauth_provider:
            raise HTTPException(status_code=404, detail="OAuth is disabled")
        return {
            "resource": runtime_settings.oauth_resource_url,
            "authorization_servers": [runtime_settings.oauth_issuer_url],
            "scopes_supported": runtime_settings.oauth_scopes,
            "bearer_methods_supported": ["header"],
            "resource_name": runtime_settings.display_name,
        }

    @app.post("/register", include_in_schema=False)
    async def oauth_register(request: Request) -> JSONResponse:
        """Register ChatGPT's public OAuth client.

        The MCP SDK's built-in handler requires both ``authorization_code`` and
        ``refresh_token`` to be present in the request metadata. ChatGPT may send
        only the authorization-code grant because refresh support is negotiated
        separately. This compatibility endpoint normalizes that valid public
        client metadata before handing it to the same provider and database.
        """

        if not oauth_provider:
            raise HTTPException(status_code=404, detail="OAuth is disabled")

        try:
            metadata = OAuthClientMetadata.model_validate(await request.json())
        except Exception as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_client_metadata",
                    "error_description": str(exc),
                },
            )

        auth_method = metadata.token_endpoint_auth_method or "none"
        if auth_method != "none":
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_client_metadata",
                    "error_description": "Xiaoruo1 OAuth clients must use token_endpoint_auth_method=none",
                },
            )

        grant_types = list(dict.fromkeys(metadata.grant_types or ["authorization_code"]))
        if "authorization_code" not in grant_types:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_client_metadata",
                    "error_description": "grant_types must include authorization_code",
                },
            )
        if "refresh_token" not in grant_types:
            grant_types.append("refresh_token")

        response_types = list(dict.fromkeys(metadata.response_types or ["code"]))
        if "code" not in response_types:
            response_types.append("code")

        scope = metadata.scope or " ".join(runtime_settings.oauth_scopes)
        if runtime_settings.oauth_scopes:
            requested_scopes = set(scope.split())
            valid_scopes = set(runtime_settings.oauth_scopes)
            if not requested_scopes.issubset(valid_scopes):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_client_metadata",
                        "error_description": "Requested scopes are not valid",
                    },
                )

        client_payload = metadata.model_dump(mode="python")
        client_payload.update(
            {
                "client_id": str(uuid4()),
                "client_id_issued_at": int(time.time()),
                "client_secret": None,
                "token_endpoint_auth_method": auth_method,
                "grant_types": grant_types,
                "response_types": response_types,
                "scope": scope,
            }
        )
        client_info = OAuthClientInformationFull.model_validate(client_payload)
        try:
            await oauth_provider.register_client(client_info)
        except RegistrationError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": exc.error,
                    "error_description": exc.error_description,
                },
            )

        return JSONResponse(
            status_code=201,
            content=client_info.model_dump(mode="json", exclude_none=True),
        )

    @app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    async def oauth_authorization_server_metadata() -> dict[str, Any]:
        return oauth_metadata()

    @app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    @app.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
    async def oauth_protected_resource_metadata() -> dict[str, Any]:
        return protected_resource_metadata()

    def render_consent_page(pending: dict[str, Any], error: str | None = None) -> str:
        title = html.escape(f"{runtime_settings.display_name} 授权")
        scopes = html.escape(" ".join(pending.get("scopes") or runtime_settings.oauth_scopes))
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:560px;margin:48px auto;padding:0 20px;color:#202124}}
main{{border:1px solid #ddd;border-radius:14px;padding:28px;box-shadow:0 4px 18px #00000012}}
input{{width:100%;box-sizing:border-box;padding:12px;margin:8px 0 18px;border:1px solid #bbb;border-radius:8px;font-size:16px}}
button{{padding:12px 18px;border:0;border-radius:8px;background:#111;color:white;font-size:16px;cursor:pointer}}
.muted{{color:#666;line-height:1.6}}.error{{color:#b3261e;background:#fdecea;padding:10px;border-radius:8px}}
</style></head><body><main>
<h1>{title}</h1>
<p>ChatGPT 正在请求连接你的个人电脑 Agent。</p>
<p class="muted">授权范围：<code>{scopes}</code></p>
<p class="muted">请输入本机文件 <code>08_web_data/web_agent.token</code> 中的 token。它只会提交到你的 {html.escape(runtime_settings.display_name)}，不会提交给 ChatGPT。</p>
{error_html}
<form method="post" action="/oauth/consent">
<input type="hidden" name="request_id" value="{html.escape(str(pending.get("request_id", "")), quote=True)}">
<label for="web_agent_token">{html.escape(runtime_settings.display_name)} token</label>
<input id="web_agent_token" name="web_agent_token" type="password" autocomplete="current-password" required autofocus>
<button type="submit">批准连接</button>
</form></main></body></html>"""

    @app.get("/oauth/consent", include_in_schema=False)
    async def oauth_consent(request_id: str) -> HTMLResponse:
        if not oauth_provider:
            raise HTTPException(status_code=404, detail="OAuth is disabled")
        pending = await oauth_provider.get_pending_authorization(request_id)
        if not pending:
            raise HTTPException(status_code=400, detail="OAuth authorization request expired")
        pending["request_id"] = request_id
        return HTMLResponse(render_consent_page(pending))

    @app.post("/oauth/consent", include_in_schema=False, response_model=None)
    async def oauth_consent_submit(request: Request):
        if not oauth_provider:
            raise HTTPException(status_code=404, detail="OAuth is disabled")
        form = await request.form()
        request_id = str(form.get("request_id") or "")
        local_token = str(form.get("web_agent_token") or "")
        redirect_url, error = await oauth_provider.approve_authorization(request_id, local_token)
        if redirect_url:
            from starlette.responses import RedirectResponse

            return RedirectResponse(redirect_url, status_code=303)
        pending = await oauth_provider.get_pending_authorization(request_id)
        if not pending:
            raise HTTPException(status_code=400, detail=error or "OAuth authorization request expired")
        pending["request_id"] = request_id
        return HTMLResponse(render_consent_page(pending, error), status_code=400)

    @app.get("/upload-test/fixture.html", include_in_schema=False)
    async def fixed_upload_fixture():
        # Fixed non-sensitive test HTML only. Never expose a filesystem route,
        # selected local file, browser state or credentials through this URL.
        fixture = runtime_settings.project_root / "10_web_deploy" / "validation_runs" / "bilibili-web-upload-20261027" / "fixture.html"
        if not fixture.is_file():
            raise HTTPException(status_code=404, detail="Upload fixture is not installed")
        return FileResponse(fixture, media_type="text/html", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/upload-test/{unavailable:path}", include_in_schema=False)
    async def reject_fixture_paths(unavailable: str):
        raise HTTPException(status_code=404, detail="Only the fixed test fixture is available")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "web_agent", "display_name": runtime_settings.display_name, "mcp_path": "/mcp", "host": runtime_settings.host, "port": runtime_settings.port, "orchestration_mode": runtime_settings.orchestration_mode}

    @app.get("/api/config")
    async def api_config(_: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return {
            "service": "web_agent",
            "display_name": runtime_settings.display_name,
            "version": "0.2.0",
            "auth": "oauth2+bearer-token" if runtime_settings.oauth_enabled else "bearer-token",
            "admin_mode": runtime_settings.admin_mode,
            "workspace_root": str(runtime_settings.workspace_root),
            "allowed_workspaces": [str(path) for path in runtime_settings.allowed_workspaces],
            "mcp_path": "/mcp",
            "features": {"canvas": True, "worker": True, "wsl": True, "python": True},
        }

    @app.get("/api/tools")
    async def api_tools(registry: ToolRegistry = Depends(get_registry), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return {"tools": registry.list_specs()}

    @app.post("/api/tools/invoke")
    async def api_invoke_tool(body: ToolInvocation, registry: ToolRegistry = Depends(get_registry), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        try:
            result = await registry.invoke(body.name, body.arguments)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"name": body.name, "result": result}

    @app.get("/api/system")
    async def api_system(registry: ToolRegistry = Depends(get_registry), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        return await registry.invoke("get_system_info")

    @app.get("/api/conversations")
    async def api_conversations(state: AppState = Depends(get_state), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        assert state.conversations is not None
        return {"conversations": await state.conversations.list()}

    @app.post("/api/conversations")
    async def api_create_conversation(state: AppState = Depends(get_state), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        assert state.conversations is not None
        return await state.conversations.create()

    @app.get("/api/conversations/{conversation_id}")
    async def api_get_conversation(conversation_id: str, state: AppState = Depends(get_state), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        assert state.conversations is not None
        conversation = await state.conversations.get(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return conversation

    @app.post("/api/chat")
    async def api_chat(body: ChatRequest, state: AppState = Depends(get_state), registry: ToolRegistry = Depends(get_registry), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        assert state.conversations is not None
        conversation_id = body.conversation_id
        if conversation_id:
            conversation = await state.conversations.get(conversation_id)
            if not conversation:
                raise HTTPException(status_code=404, detail="Conversation not found")
        else:
            conversation = await state.conversations.create(body.message[:60])
            conversation_id = conversation["id"]
        await state.conversations.append_message(conversation_id, "user", body.message)
        tool_name = body.tool_name
        tool_input = body.tool_input
        if not tool_name:
            tool_name, parsed_input = _parse_chat_tool(body.message)
            if tool_name:
                tool_input = parsed_input
        if tool_name:
            try:
                result = await registry.invoke(tool_name, tool_input or {})
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (ValueError, PermissionError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            content = f"工具 `{tool_name}` 已执行。\n\n```json\n{json.dumps(result, ensure_ascii=False, indent=2, default=str)}\n```"
            await state.conversations.append_message(conversation_id, "tool", content, tool_name, tool_input or {}, result)
            return {"conversation_id": conversation_id, "message": content, "tool_name": tool_name, "tool_result": result}
        content = "消息已保存。当前本地 Chat UI 不绑定外部大模型；请选择右侧工具，或使用 `/tool 工具名 {JSON参数}` 调用 web_mcp。"
        await state.conversations.append_message(conversation_id, "assistant", content)
        return {"conversation_id": conversation_id, "message": content, "tool_name": None}

    @app.get("/api/tasks")
    async def api_tasks(state: AppState = Depends(get_state), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        from web_memory.task_repository import TaskRepository

        return {"tasks": await TaskRepository(state.db).list()}

    @app.post("/api/tasks")
    async def api_create_task(body: TaskRequest, registry: ToolRegistry = Depends(get_registry), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        try:
            return await registry.invoke("create_task", {"profile": body.profile, "payload": body.payload})
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_id}")
    async def api_task(task_id: str, registry: ToolRegistry = Depends(get_registry), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        # This endpoint is a non-blocking status view.  The MCP tool
        # ``monitor_task`` intentionally waits for a terminal result and may
        # run the autonomous repair loop; using it here made a queued task
        # detail request hang for the full MCP wait window when no Worker was
        # attached (and made the local API look frozen).  Long-task callers
        # should use the MCP supervisor; the REST status endpoint must return
        # the current durable record immediately.
        task = await registry.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

    @app.post("/api/tasks/{task_id}/cancel")
    async def api_cancel_task(task_id: str, registry: ToolRegistry = Depends(get_registry), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        task = await registry.invoke("cancel_task", {"task_id": task_id})
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

    @app.get("/api/canvas")
    async def api_canvases(state: AppState = Depends(get_state), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        assert state.canvas is not None
        return {"canvases": await state.canvas.list()}

    @app.post("/api/canvas")
    async def api_create_canvas(body: CanvasCreate, state: AppState = Depends(get_state), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        assert state.canvas is not None
        return await state.canvas.create(body.title, body.content, body.kind, body.status)

    @app.get("/api/canvas/{canvas_id}")
    async def api_get_canvas(canvas_id: str, revision: int | None = None, state: AppState = Depends(get_state), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        assert state.canvas is not None
        canvas = await state.canvas.get(canvas_id, revision)
        if not canvas:
            raise HTTPException(status_code=404, detail="Canvas not found")
        return canvas

    @app.put("/api/canvas/{canvas_id}")
    async def api_update_canvas(canvas_id: str, body: CanvasUpdate, state: AppState = Depends(get_state), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        assert state.canvas is not None
        canvas = await state.canvas.update(canvas_id, body.content, body.title, body.kind, body.status, body.metadata)
        if not canvas:
            raise HTTPException(status_code=404, detail="Canvas not found")
        return canvas

    @app.get("/api/canvas/{canvas_id}/history")
    async def api_canvas_history(canvas_id: str, state: AppState = Depends(get_state), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        assert state.canvas is not None
        if not await state.db.fetchone("SELECT id FROM canvases WHERE id=?", (canvas_id,)):
            raise HTTPException(status_code=404, detail="Canvas not found")
        return {"history": await state.canvas.history(canvas_id)}

    @app.post("/api/canvas/{canvas_id}/restore/{revision_number}")
    async def api_restore_canvas(canvas_id: str, revision_number: int, state: AppState = Depends(get_state), _: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        assert state.canvas is not None
        canvas = await state.canvas.restore(canvas_id, revision_number)
        if not canvas:
            raise HTTPException(status_code=404, detail="Canvas or revision not found")
        return canvas

    # FastMCP's route is registered directly so the public endpoint remains exactly /mcp.
    mcp_http_app = mcp_instance.streamable_http_app()
    app.router.routes.extend(mcp_http_app.routes)

    # ChatGPT has used both MCP transports over time.  The installed Xiaoruo1
    # app can still perform its initial capability probe against the legacy
    # SSE endpoint (``/sse``); without these routes the SPA fallback returns
    # ``index.html`` and the client reports "SSE probe returned text/html"
    # before it ever sees a tool.  Keep the Streamable HTTP endpoint above,
    # and expose only the SSE transport/message routes here.  The parent app
    # already owns the OAuth endpoints and authentication middleware, so do
    # not duplicate the SSE app's auth/metadata routes.
    sse_http_app = mcp_instance.sse_app()
    sse_paths = {
        str(getattr(mcp_instance.settings, "sse_path", "/sse")).rstrip("/") or "/",
        str(getattr(mcp_instance.settings, "message_path", "/messages/")).rstrip("/") or "/",
    }
    app.router.routes.extend(
        route for route in sse_http_app.routes if getattr(route, "path", None) in sse_paths
    )

    if runtime_settings.frontend_dist.exists():
        assets_dir = runtime_settings.frontend_dist / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/", include_in_schema=False)
        async def frontend_index() -> FileResponse:
            return FileResponse(runtime_settings.frontend_dist / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def frontend_fallback(full_path: str) -> FileResponse:
            candidate = runtime_settings.frontend_dist / full_path
            if candidate.is_file() and runtime_settings.frontend_dist in candidate.resolve().parents:
                return FileResponse(candidate)
            return FileResponse(runtime_settings.frontend_dist / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        async def frontend_not_built() -> dict[str, Any]:
            return {"service": "web_agent", "display_name": runtime_settings.display_name, "status": "ok", "message": "Frontend has not been built. Run npm install && npm run build in 01_web_frontend."}

    return app


app = create_app()


def main() -> None:
    settings = Settings.load()
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower(), access_log=False)