from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .store import Database


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class ConversationRepository:
    def __init__(self, db: Database):
        self.db = db

    async def create(self, title: str = "New conversation") -> dict[str, Any]:
        now = utc_now()
        item = {"id": new_id("conv"), "title": title[:200] or "New conversation", "created_at": now, "updated_at": now}
        await self.db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)", item.values())
        await self.db.commit()
        return item

    async def list(self) -> list[dict[str, Any]]:
        return await self.db.fetchall("SELECT * FROM conversations ORDER BY updated_at DESC")

    async def get(self, conversation_id: str) -> dict[str, Any] | None:
        item = await self.db.fetchone("SELECT * FROM conversations WHERE id=?", (conversation_id,))
        if item:
            item["messages"] = await self.db.fetchall(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at ASC", (conversation_id,)
            )
            for message in item["messages"]:
                message["tool_input"] = self.db.loads(message.pop("tool_input_json", None), None)
                message["tool_output"] = self.db.loads(message.pop("tool_output_json", None), None)
        return item

    async def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        tool_name: str | None = None,
        tool_input: Any = None,
        tool_output: Any = None,
    ) -> dict[str, Any]:
        message = {
            "id": new_id("msg"),
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "tool_name": tool_name,
            "tool_input_json": self.db.dumps(tool_input) if tool_input is not None else None,
            "tool_output_json": self.db.dumps(tool_output) if tool_output is not None else None,
            "created_at": utc_now(),
        }
        await self.db.execute(
            "INSERT INTO messages(id,conversation_id,role,content,tool_name,tool_input_json,tool_output_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            message.values(),
        )
        await self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (message["created_at"], conversation_id))
        await self.db.commit()
        message["tool_input"] = tool_input
        message["tool_output"] = tool_output
        return message
