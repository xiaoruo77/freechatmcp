from __future__ import annotations

import json
import time
from dataclasses import replace
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AccessToken
from PIL import Image

from web_mcp.server import _capture_route_uses_own_auth, create_app


BRIDGE_TOKEN = "capture-background-bridge-test-secret-" * 2
PUBLIC_ORIGIN = "https://capture.example"


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def configured_settings(settings, tmp_path, *, oauth_enabled=True, config=None):
    token_path = tmp_path / "background-bridge.token"
    token_path.write_text(BRIDGE_TOKEN, encoding="utf-8")
    config_path = tmp_path / "background.json"
    config_path.write_text(json.dumps(config if config is not None else {
        "token_file": str(token_path), "capture_public_origin": PUBLIC_ORIGIN,
    }), encoding="utf-8")
    return replace(settings, oauth_enabled=oauth_enabled,
                   oauth_issuer_url="http://127.0.0.1",
                   oauth_resource_url="http://127.0.0.1/mcp",
                   oauth_scopes=["web_agent"],
                   extra={**settings.extra, "background_desktop_config": str(config_path)})


def image_bytes():
    output = BytesIO()
    Image.new("RGB", (5, 4), "red").save(output, "PNG")
    return output.getvalue()


def mcp_initialize(client, token):
    return client.post("/mcp", headers={**auth(token), "Host": "127.0.0.1:48271",
                       "Accept": "application/json, text/event-stream"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                           "protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "capture-regression", "version": "1"}}})


def test_oauth_enabled_capture_credentials_reach_only_the_self_authenticated_router(
        isolated_settings, tmp_path, monkeypatch):
    settings = configured_settings(isolated_settings, tmp_path)
    app = create_app(settings)
    manager = app.state.remote_gui_capture_manager
    assert manager is not None
    with TestClient(app, client=("127.0.0.1", 50000)) as local:
        provider = app.state.web_agent.oauth_provider
        observed = []
        original = provider.load_access_token

        async def observe(token):
            observed.append(token)
            return await original(token)

        monkeypatch.setattr(provider, "load_access_token", observe)
        created = local.post("/internal/remote-gui/captures", headers=auth(BRIDGE_TOKEN))
        assert created.status_code == 200
        ticket = created.json()
        capture_id = ticket["capture_id"]
        public_path = "/remote-gui/captures/" + capture_id
        internal_path = "/internal/remote-gui/captures/" + capture_id
        assert settings.token not in json.dumps(ticket)
        assert BRIDGE_TOKEN not in json.dumps(ticket)
        assert local.get(internal_path, headers=auth(BRIDGE_TOKEN)).json()["status"] == "pending"
        assert local.get(internal_path, headers=auth(settings.token)).status_code == 401
        assert local.post(public_path, content=image_bytes(), headers=auth(settings.token)).status_code == 401
        assert local.post(public_path, content=image_bytes(), headers=auth(BRIDGE_TOKEN)).status_code == 401
        remote = TestClient(app, client=("203.0.113.15", 50001))
        assert remote.get(internal_path, headers=auth(BRIDGE_TOKEN)).status_code == 403
        uploaded = remote.post(public_path, headers=auth(ticket["token"]), content=image_bytes())
        assert uploaded.status_code == 200
        completed = local.get(internal_path, headers=auth(BRIDGE_TOKEN)).json()
        assert completed["status"] == "completed"
        assert completed["capture"]["mime_type"] == "image/png"
        assert manager.capture_root == settings.data_root / "background_desktop" / "captures"
        assert observed == []

        # A ticket remains powerless outside the narrowly matched capture routes.
        assert local.get("/api/config", headers=auth(ticket["token"])).status_code == 401
        assert mcp_initialize(local, ticket["token"]).status_code == 401
        assert ticket["token"] in observed
        assert local.get("/api/config").status_code == 401
        assert local.get("/api/config", headers=auth(settings.token)).status_code == 200
        assert mcp_initialize(local, settings.token).status_code == 200

        # Exercise a persisted OAuth access token through the unchanged provider,
        # rather than only the administrator credential's compatibility path.
        oauth_token = "capture-regression-oauth-access-token"

        async def persist_oauth_token():
            await provider._store_access_token(AccessToken(
                token=oauth_token, client_id="capture-test-client", scopes=["web_agent"],
                expires_at=int(time.time()) + 300, resource=settings.oauth_resource_url))
            await app.state.web_agent.db.commit()

        local.portal.call(persist_oauth_token)
        assert local.get("/api/config", headers=auth(oauth_token)).status_code == 200
        assert mcp_initialize(local, oauth_token).status_code == 200
        assert oauth_token in observed
        assert local.delete(internal_path, headers=auth(BRIDGE_TOKEN)).status_code == 204
        local.post("/internal/remote-gui/captures", headers=auth(BRIDGE_TOKEN))
        assert manager._tickets
    assert manager._tickets == {}


def test_nearby_routes_and_wrong_methods_still_run_global_oauth_validation(
        isolated_settings, tmp_path, monkeypatch):
    app = create_app(configured_settings(isolated_settings, tmp_path))
    with TestClient(app, client=("127.0.0.1", 50000)) as local:
        ticket = local.post("/internal/remote-gui/captures", headers=auth(BRIDGE_TOKEN)).json()
        provider = app.state.web_agent.oauth_provider
        observed = []
        original = provider.load_access_token

        async def observe(token):
            observed.append(token)
            return await original(token)

        monkeypatch.setattr(provider, "load_access_token", observe)
        capture_id = ticket["capture_id"]
        paths = [
            ("POST", f"/remote-gui/captures/{capture_id}/other"),
            ("POST", f"/remote-gui/captures/{capture_id}/"),
            ("POST", f"/remote-gui/captures/{'A' * 32}"),
            ("POST", "/remote-gui/captures"),
            ("GET", f"/remote-gui/captures/{capture_id}"),
            ("POST", f"/internal/remote-gui/captures/{capture_id}"),
            ("GET", "/internal/remote-gui/captures"),
            ("DELETE", f"/internal/remote-gui/captures/{capture_id}/other"),
        ]
        for method, path in paths:
            count = len(observed)
            local.request(method, path, headers=auth(ticket["token"]), follow_redirects=False)
            assert len(observed) == count + 1, (method, path)
        assert app.state.remote_gui_capture_manager.inspect(capture_id)["status"] == "pending"


@pytest.mark.parametrize("missing", ["config_reference", "config_file", "origin", "token_reference", "token_file"])
def test_capture_router_is_disabled_when_its_configuration_is_incomplete(isolated_settings, tmp_path, missing):
    config = {"capture_public_origin": PUBLIC_ORIGIN, "token_file": str(tmp_path / "background-bridge.token")}
    if missing == "origin":
        del config["capture_public_origin"]
    elif missing == "token_reference":
        del config["token_file"]
    settings = configured_settings(isolated_settings, tmp_path, config=config)
    if missing == "config_reference":
        settings = replace(settings, project_root=tmp_path / "unconfigured-project",
                           extra={key: value for key, value in settings.extra.items()
                                           if key != "background_desktop_config"})
    elif missing == "config_file":
        tmp_path.joinpath("background.json").unlink()
    elif missing == "token_file":
        tmp_path.joinpath("background-bridge.token").unlink()
    app = create_app(settings)
    assert app.state.remote_gui_capture_manager is None
    assert "/internal/remote-gui/captures" not in {getattr(route, "path", None) for route in app.routes}


def test_default_production_config_location_is_used_when_extra_has_none(isolated_settings, tmp_path):
    project = tmp_path / "fake-production-project"
    config_directory = project / "07_web_config"
    config_directory.mkdir(parents=True)
    token_file = config_directory / "bridge.token"
    token_file.write_text(BRIDGE_TOKEN, encoding="utf-8")
    config_directory.joinpath("background_desktop.json").write_text(json.dumps({
        "capture_public_origin": PUBLIC_ORIGIN, "token_file": str(token_file),
    }), encoding="utf-8")
    settings = replace(isolated_settings, project_root=project, oauth_enabled=True,
                       oauth_issuer_url="http://127.0.0.1", oauth_resource_url="http://127.0.0.1/mcp",
                       extra={**isolated_settings.extra, "background_desktop_config": None})
    app = create_app(settings)
    manager = app.state.remote_gui_capture_manager
    assert manager is not None
    assert manager.capture_root == settings.data_root / "background_desktop" / "captures"
    # The ticket routes need no database or GUI; avoid starting the fake
    # project's unrelated service lifespan while proving its real ASGI route.
    client = TestClient(app, client=("127.0.0.1", 50000))
    response = client.post("/internal/remote-gui/captures", headers=auth(BRIDGE_TOKEN))
    assert response.status_code == 200
    assert response.json()["upload_url"].startswith(PUBLIC_ORIGIN + "/remote-gui/captures/")
    assert client.post("/internal/remote-gui/captures", headers=auth(settings.token)).status_code == 401
    manager.close()


def test_non_oauth_server_still_has_ticket_auth_and_original_api_auth(isolated_settings, tmp_path):
    settings = configured_settings(isolated_settings, tmp_path, oauth_enabled=False)
    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        created = client.post("/internal/remote-gui/captures", headers=auth(BRIDGE_TOKEN))
        assert created.status_code == 200
        ticket = created.json()
        assert client.post("/remote-gui/captures/" + ticket["capture_id"],
                           headers=auth(ticket["token"]), content=image_bytes()).status_code == 200
        assert client.get("/api/config", headers=auth(ticket["token"])).status_code == 401
        assert mcp_initialize(client, ticket["token"]).status_code == 401
        assert client.get("/api/config", headers=auth(settings.token)).status_code == 200
        assert mcp_initialize(client, settings.token).status_code == 200


@pytest.mark.parametrize("method,path", [
    ("POST", "/internal/remote-gui/captures"),
    ("GET", "/internal/remote-gui/captures/" + "a" * 32),
    ("DELETE", "/internal/remote-gui/captures/" + "1" * 32),
    ("POST", "/remote-gui/captures/" + "0" * 32),
])
def test_capture_oauth_exemption_is_http_only_and_method_specific(method, path):
    assert _capture_route_uses_own_auth({"type": "http", "method": method, "path": path})
    assert not _capture_route_uses_own_auth({"type": "websocket", "method": method, "path": path})
    assert not _capture_route_uses_own_auth({"type": "http", "method": "PATCH", "path": path})
