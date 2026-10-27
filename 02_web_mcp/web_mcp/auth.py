from __future__ import annotations

import secrets
from typing import Any

from fastapi import HTTPException, Request, status


def extract_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-web-agent-token", "").strip()


def require_request_token(request: Request, expected: str) -> dict[str, Any]:
    supplied = extract_token(request)
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Valid Xiaoruo1 token required")
    return {"mode": "admin"}


def valid_token(request: Request, expected: str) -> bool:
    supplied = extract_token(request)
    return bool(supplied and secrets.compare_digest(supplied, expected))