from __future__ import annotations

from typing import Any

from web_memory.repository import new_id, utc_now
from web_memory.store import Database


class CanvasService:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _decode_canvas(row: dict[str, Any] | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    @staticmethod
    def _decode_revision(row: dict[str, Any] | None, db: Database) -> dict[str, Any] | None:
        if not row:
            return None
        row["metadata"] = db.loads(row.pop("metadata_json", None), {})
        return row

    async def create(self, title: str, content: str, kind: str = "markdown", status: str = "draft", created_by: str = "admin") -> dict[str, Any]:
        now = utc_now()
        canvas_id = new_id("canvas")
        revision_id = new_id("rev")
        await self.db.execute(
            "INSERT INTO canvases(id,title,kind,status,current_revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (canvas_id, title[:200] or "Untitled Canvas", kind[:50] or "markdown", status[:50] or "draft", 1, now, now),
        )
        await self.db.execute(
            "INSERT INTO canvas_revisions(id,canvas_id,revision_number,content,metadata_json,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (revision_id, canvas_id, 1, content, self.db.dumps({"initial": True}), created_by, now),
        )
        await self.db.commit()
        return await self.get(canvas_id)  # type: ignore[return-value]

    async def get(self, canvas_id: str, revision: int | None = None) -> dict[str, Any] | None:
        canvas = await self.db.fetchone("SELECT * FROM canvases WHERE id=?", (canvas_id,))
        if not canvas:
            return None
        revision_number = revision or int(canvas["current_revision"])
        current = await self.db.fetchone(
            "SELECT * FROM canvas_revisions WHERE canvas_id=? AND revision_number=?", (canvas_id, revision_number)
        )
        canvas["revision"] = self._decode_revision(current, self.db)
        canvas["revisions"] = await self.history(canvas_id)
        return canvas

    async def list(self) -> list[dict[str, Any]]:
        return await self.db.fetchall("SELECT * FROM canvases ORDER BY updated_at DESC")

    async def update(
        self,
        canvas_id: str,
        content: str,
        title: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        created_by: str = "admin",
    ) -> dict[str, Any] | None:
        canvas = await self.db.fetchone("SELECT * FROM canvases WHERE id=?", (canvas_id,))
        if not canvas:
            return None
        next_revision = int(canvas["current_revision"]) + 1
        now = utc_now()
        await self.db.execute(
            "INSERT INTO canvas_revisions(id,canvas_id,revision_number,content,metadata_json,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (new_id("rev"), canvas_id, next_revision, content, self.db.dumps(metadata or {}), created_by, now),
        )
        await self.db.execute(
            "UPDATE canvases SET title=COALESCE(?,title),kind=COALESCE(?,kind),status=COALESCE(?,status),current_revision=?,updated_at=? WHERE id=?",
            (title[:200] if title else None, kind[:50] if kind else None, status[:50] if status else None, next_revision, now, canvas_id),
        )
        await self.db.commit()
        return await self.get(canvas_id)

    async def history(self, canvas_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT id,canvas_id,revision_number,metadata_json,created_by,created_at,length(content) AS content_length FROM canvas_revisions WHERE canvas_id=? ORDER BY revision_number DESC",
            (canvas_id,),
        )
        for row in rows:
            row["metadata"] = self.db.loads(row.pop("metadata_json", None), {})
        return rows

    async def restore(self, canvas_id: str, revision_number: int, created_by: str = "admin") -> dict[str, Any] | None:
        source = await self.db.fetchone(
            "SELECT content,metadata_json FROM canvas_revisions WHERE canvas_id=? AND revision_number=?",
            (canvas_id, revision_number),
        )
        if not source:
            return None
        metadata = self.db.loads(source.get("metadata_json"), {})
        metadata["restored_from"] = revision_number
        return await self.update(canvas_id, source["content"], metadata=metadata, status="restored", created_by=created_by)
