from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from web_config.settings import Settings
from web_memory.store import Database


class LocalOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """Small OAuth 2.1 provider for one local Xiaoruo1 administrator.

    The user proves local ownership once by entering the existing Xiaoruo1 token
    on the consent page. ChatGPT then receives a short-lived OAuth access token and
    sends it as a normal Bearer token to the MCP resource server.

    OAuth state and issued tokens are stored in the existing WAL SQLite database.
    Raw access/refresh tokens are not persisted; only SHA-256 hashes are stored.
    """

    AUTH_CODE_TTL = 600
    ACCESS_TOKEN_TTL = 3600
    REFRESH_TOKEN_TTL = 30 * 24 * 3600

    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.issuer_url = settings.oauth_issuer_url.rstrip("/")
        self.resource_url = settings.oauth_resource_url.rstrip("/")
        self.scopes = list(settings.oauth_scopes) or ["web_agent"]
        self.allowed_redirect_hosts = {
            host.lower().strip().rstrip(".")
            for host in settings.oauth_allowed_redirect_hosts
            if host.strip()
        }

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> int:
        return int(time.time())

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not client_id:
            return None
        row = await self.database.fetchone(
            "SELECT metadata_json FROM oauth_clients WHERE client_id=?",
            (client_id,),
        )
        if not row:
            return None
        try:
            return OAuthClientInformationFull.model_validate(
                self.database.loads(row["metadata_json"], {})
            )
        except Exception:
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        client_id = (client_info.client_id or "").strip()
        if not client_id:
            raise RegistrationError("invalid_client_metadata", "OAuth client_id is required")
        if client_info.token_endpoint_auth_method not in {"none", None}:
            raise RegistrationError(
                "invalid_client_metadata",
                "web_agent OAuth clients must use token_endpoint_auth_method=none",
            )
        if not client_info.redirect_uris:
            raise RegistrationError("invalid_redirect_uri", "At least one OAuth redirect URI is required")
        for redirect_uri in client_info.redirect_uris:
            host = (redirect_uri.host or "").lower().rstrip(".")
            if redirect_uri.scheme != "https" and host not in {"localhost", "127.0.0.1", "::1"}:
                raise RegistrationError("invalid_redirect_uri", "OAuth redirect URI must use HTTPS")
            if host not in self.allowed_redirect_hosts:
                raise RegistrationError("invalid_redirect_uri", f"OAuth redirect host is not allowed: {host}")

        metadata = client_info.model_dump(mode="json")
        await self.database.execute(
            """
            INSERT INTO oauth_clients(client_id, metadata_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET metadata_json=excluded.metadata_json
            """,
            (client_id, self.database.dumps(metadata), self._now()),
        )
        await self.database.commit()

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource_url:
            raise AuthorizeError("invalid_request", "The OAuth resource does not match this MCP server")

        pending_id = secrets.token_urlsafe(32)
        pending = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "state": params.state,
            "scopes": params.scopes or self.scopes,
            "code_challenge": params.code_challenge,
            "resource": params.resource or self.resource_url,
        }
        await self.database.execute(
            "INSERT INTO oauth_pending_authorizations(id, data_json, expires_at) VALUES (?, ?, ?)",
            (pending_id, self.database.dumps(pending), self._now() + self.AUTH_CODE_TTL),
        )
        await self.database.commit()
        return f"{self.issuer_url}/oauth/consent?{urlencode({'request_id': pending_id})}"

    async def get_pending_authorization(self, pending_id: str) -> dict[str, Any] | None:
        row = await self.database.fetchone(
            "SELECT data_json, expires_at FROM oauth_pending_authorizations WHERE id=?",
            (pending_id,),
        )
        if not row:
            return None
        if int(row["expires_at"]) < self._now():
            await self.database.execute(
                "DELETE FROM oauth_pending_authorizations WHERE id=?",
                (pending_id,),
            )
            await self.database.commit()
            return None
        return self.database.loads(row["data_json"], {})

    async def approve_authorization(
        self,
        pending_id: str,
        local_token: str,
    ) -> tuple[str | None, str | None]:
        pending = await self.get_pending_authorization(pending_id)
        if not pending:
            return None, "授权请求已过期，请返回 ChatGPT 重新连接。"
        if not local_token or not secrets.compare_digest(local_token, self.settings.token):
            return None, "本机 Xiaoruo1 token 不正确。"

        code_value = secrets.token_urlsafe(48)
        authorization_code = AuthorizationCode(
            code=code_value,
            scopes=list(pending.get("scopes") or self.scopes),
            expires_at=float(self._now() + self.AUTH_CODE_TTL),
            client_id=str(pending["client_id"]),
            code_challenge=str(pending["code_challenge"]),
            redirect_uri=str(pending["redirect_uri"]),
            redirect_uri_provided_explicitly=bool(pending.get("redirect_uri_provided_explicitly", True)),
            resource=str(pending.get("resource") or self.resource_url),
            subject="local-admin",
        )
        data = authorization_code.model_dump(mode="json")
        data.pop("code", None)
        await self.database.execute(
            "INSERT INTO oauth_authorization_codes(code_hash, data_json, expires_at, used) VALUES (?, ?, ?, 0)",
            (
                self._hash(code_value),
                self.database.dumps(data),
                int(authorization_code.expires_at),
            ),
        )
        await self.database.execute(
            "DELETE FROM oauth_pending_authorizations WHERE id=?",
            (pending_id,),
        )
        await self.database.commit()
        redirect_uri = str(authorization_code.redirect_uri)
        return construct_redirect_uri(redirect_uri, code=code_value, state=pending.get("state")), None

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        row = await self.database.fetchone(
            "SELECT data_json, expires_at, used FROM oauth_authorization_codes WHERE code_hash=?",
            (self._hash(authorization_code),),
        )
        if not row or int(row["used"]):
            return None
        if int(row["expires_at"]) < self._now():
            return None
        payload = self.database.loads(row["data_json"], {})
        payload["code"] = authorization_code
        try:
            value = AuthorizationCode.model_validate(payload)
        except Exception:
            return None
        if value.client_id != client.client_id:
            return None
        return value

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        cursor = await self.database.execute(
            "UPDATE oauth_authorization_codes SET used=1 WHERE code_hash=? AND used=0",
            (self._hash(authorization_code.code),),
        )
        if cursor.rowcount != 1:
            await self.database.commit()
            raise TokenError("invalid_grant", "authorization code has already been used")

        access_value = secrets.token_urlsafe(48)
        refresh_value = secrets.token_urlsafe(48)
        now = self._now()
        access = AccessToken(
            token=access_value,
            client_id=str(client.client_id),
            scopes=list(authorization_code.scopes),
            expires_at=now + self.ACCESS_TOKEN_TTL,
            resource=authorization_code.resource or self.resource_url,
            subject=authorization_code.subject or "local-admin",
            claims={"iss": self.issuer_url},
        )
        refresh = RefreshToken(
            token=refresh_value,
            client_id=str(client.client_id),
            scopes=list(authorization_code.scopes),
            expires_at=now + self.REFRESH_TOKEN_TTL,
            subject=authorization_code.subject or "local-admin",
        )
        await self._store_access_token(access)
        await self._store_refresh_token(refresh)
        await self.database.commit()
        return OAuthToken(
            access_token=access_value,
            token_type="Bearer",
            expires_in=self.ACCESS_TOKEN_TTL,
            scope=" ".join(access.scopes),
            refresh_token=refresh_value,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        row = await self.database.fetchone(
            "SELECT data_json, expires_at, revoked FROM oauth_refresh_tokens WHERE token_hash=?",
            (self._hash(refresh_token),),
        )
        if not row or int(row["revoked"]) or int(row["expires_at"]) < self._now():
            return None
        payload = self.database.loads(row["data_json"], {})
        payload["token"] = refresh_token
        try:
            value = RefreshToken.model_validate(payload)
        except Exception:
            return None
        return value if value.client_id == client.client_id else None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if not set(scopes).issubset(set(refresh_token.scopes)):
            raise TokenError("invalid_scope", "requested scope was not granted")
        await self.database.execute(
            "UPDATE oauth_refresh_tokens SET revoked=1 WHERE token_hash=?",
            (self._hash(refresh_token.token),),
        )
        now = self._now()
        access_value = secrets.token_urlsafe(48)
        refresh_value = secrets.token_urlsafe(48)
        access = AccessToken(
            token=access_value,
            client_id=str(client.client_id),
            scopes=list(scopes),
            expires_at=now + self.ACCESS_TOKEN_TTL,
            resource=self.resource_url,
            subject=refresh_token.subject or "local-admin",
            claims={"iss": self.issuer_url},
        )
        refresh = RefreshToken(
            token=refresh_value,
            client_id=str(client.client_id),
            scopes=list(scopes),
            expires_at=now + self.REFRESH_TOKEN_TTL,
            subject=refresh_token.subject or "local-admin",
        )
        await self._store_access_token(access)
        await self._store_refresh_token(refresh)
        await self.database.commit()
        return OAuthToken(
            access_token=access_value,
            token_type="Bearer",
            expires_in=self.ACCESS_TOKEN_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh_value,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token and secrets.compare_digest(token, self.settings.token):
            return AccessToken(
                token=token,
                client_id="web_agent-static",
                scopes=list(self.scopes),
                expires_at=None,
                resource=self.resource_url,
                subject="local-admin",
                claims={"iss": self.issuer_url},
            )

        row = await self.database.fetchone(
            "SELECT data_json, expires_at, revoked FROM oauth_access_tokens WHERE token_hash=?",
            (self._hash(token),),
        )
        if not row or int(row["revoked"]) or int(row["expires_at"]) < self._now():
            return None
        payload = self.database.loads(row["data_json"], {})
        payload["token"] = token
        try:
            return AccessToken.model_validate(payload)
        except Exception:
            return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        token_hash = self._hash(token.token)
        await self.database.execute(
            "UPDATE oauth_access_tokens SET revoked=1 WHERE token_hash=?",
            (token_hash,),
        )
        await self.database.execute(
            "UPDATE oauth_refresh_tokens SET revoked=1 WHERE token_hash=?",
            (token_hash,),
        )
        await self.database.commit()

    async def _store_access_token(self, token: AccessToken) -> None:
        data = token.model_dump(mode="json")
        data.pop("token", None)
        await self.database.execute(
            """
            INSERT INTO oauth_access_tokens(token_hash, data_json, expires_at, revoked)
            VALUES (?, ?, ?, 0)
            """,
            (self._hash(token.token), self.database.dumps(data), int(token.expires_at or 0)),
        )

    async def _store_refresh_token(self, token: RefreshToken) -> None:
        data = token.model_dump(mode="json")
        data.pop("token", None)
        await self.database.execute(
            """
            INSERT INTO oauth_refresh_tokens(token_hash, data_json, expires_at, revoked)
            VALUES (?, ?, ?, 0)
            """,
            (self._hash(token.token), self.database.dumps(data), int(token.expires_at or 0)),
        )