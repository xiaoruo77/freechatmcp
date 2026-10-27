from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient


def test_oauth_pkce_flow(isolated_settings):
    from web_mcp.server import create_app

    settings = replace(
        isolated_settings,
        oauth_enabled=True,
        oauth_issuer_url="http://127.0.0.1",
        oauth_resource_url="http://127.0.0.1/mcp",
        oauth_scopes=["web_agent"],
        oauth_allowed_redirect_hosts=["localhost"],
    )
    redirect_uri = "http://localhost/callback"
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    with TestClient(create_app(settings)) as client:
        metadata = client.get("/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200
        assert metadata.json()["token_endpoint_auth_methods_supported"] == ["none"]

        registration = client.post(
            "/register",
            json={
                "redirect_uris": [redirect_uri],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_name": "pytest",
            },
        )
        assert registration.status_code == 201, registration.text
        client_id = registration.json()["client_id"]

        authorization = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "test-state",
                "scope": "web_agent",
                "resource": "http://127.0.0.1/mcp",
            },
            follow_redirects=False,
        )
        assert authorization.status_code == 302
        consent_url = authorization.headers["location"]
        assert client.get(consent_url).status_code == 200
        pending_id = parse_qs(urlparse(consent_url).query)["request_id"][0]

        approved = client.post(
            "/oauth/consent",
            data={"request_id": pending_id, "web_agent_token": settings.token},
            follow_redirects=False,
        )
        assert approved.status_code == 303
        callback_params = parse_qs(urlparse(approved.headers["location"]).query)
        code = callback_params["code"][0]
        assert callback_params["state"] == ["test-state"]

        token = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
        access_token = token.json()["access_token"]
        assert client.get("/api/config", headers={"Authorization": f"Bearer {access_token}"}).status_code == 200

        # Authorization codes are single-use.
        replay = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert replay.status_code == 400