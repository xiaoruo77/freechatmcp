from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import aiosqlite


class Database:
    """Small async SQLite repository using WAL and explicit transactions."""

    def __init__(self, path: Path, schema_path: Path):
        self.path = path
        self.schema_path = schema_path
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA synchronous=NORMAL")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.execute("PRAGMA busy_timeout=5000")
        await self._connection.commit()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database is not connected")
        return self._connection

    async def init(self) -> None:
        await self.connect()
        schema = self.schema_path.read_text(encoding="utf-8")
        await self.connection.executescript(schema)
        await self.connection.commit()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def execute(self, sql: str, parameters: Iterable[Any] = ()) -> aiosqlite.Cursor:
        return await self.connection.execute(sql, tuple(parameters))

    async def executemany(self, sql: str, parameters: Iterable[Iterable[Any]]) -> aiosqlite.Cursor:
        return await self.connection.executemany(sql, parameters)

    async def fetchone(self, sql: str, parameters: Iterable[Any] = ()) -> dict[str, Any] | None:
        cursor = await self.execute(sql, parameters)
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def fetchall(self, sql: str, parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
        cursor = await self.execute(sql, parameters)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def commit(self) -> None:
        await self.connection.commit()

    async def rollback(self) -> None:
        await self.connection.rollback()

    async def begin_immediate(self) -> None:
        await self.connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def loads(value: str | None, default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
