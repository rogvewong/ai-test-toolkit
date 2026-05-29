"""SQLite reference implementation of MemoryStore.

Keyword LIKE search only — good enough for MVP. Swap to Postgres+pgvector for scale.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import aiosqlite

from packages.core.memory.interface import MemoryLayer, MemoryRecord, MemoryStore

_DDL = """
CREATE TABLE IF NOT EXISTS memory (
  id TEXT PRIMARY KEY,
  layer TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  UNIQUE (layer, scope_id, key)
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory(layer, scope_id, kind);
CREATE INDEX IF NOT EXISTS idx_memory_expires ON memory(expires_at);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteMemoryStore(MemoryStore):
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._initialized = False

    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_DDL)
            await db.commit()
        self._initialized = True

    async def save(self, record: MemoryRecord) -> str:
        await self._ensure_init()
        rid = record.id or str(uuid4())
        created = (record.created_at or datetime.now(timezone.utc)).isoformat()
        expires = record.expires_at.isoformat() if record.expires_at else None
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO memory
                  (id, layer, scope_id, kind, key, value_json, tags_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(layer, scope_id, key) DO UPDATE SET
                  value_json = excluded.value_json,
                  tags_json = excluded.tags_json,
                  kind = excluded.kind,
                  created_at = excluded.created_at,
                  expires_at = excluded.expires_at
                """,
                (
                    rid,
                    record.layer.value,
                    record.scope_id,
                    record.kind,
                    record.key,
                    json.dumps(record.value, ensure_ascii=False),
                    json.dumps(record.tags, ensure_ascii=False),
                    created,
                    expires,
                ),
            )
            await db.commit()
        return rid

    async def get(
        self, layer: MemoryLayer, scope_id: str, key: str
    ) -> MemoryRecord | None:
        await self._ensure_init()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id, layer, scope_id, kind, key, value_json, tags_json, created_at, expires_at "
                "FROM memory WHERE layer=? AND scope_id=? AND key=?",
                (layer.value, scope_id, key),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def search(
        self,
        *,
        layer: MemoryLayer,
        scope_id: str,
        query: str | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        await self._ensure_init()
        sql = (
            "SELECT id, layer, scope_id, kind, key, value_json, tags_json, created_at, expires_at "
            "FROM memory WHERE layer=? AND scope_id=?"
        )
        params: list[Any] = [layer.value, scope_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if query:
            sql += " AND (key LIKE ? OR value_json LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like])
        if tags:
            for tag in tags:
                sql += " AND tags_json LIKE ?"
                params.append(f'%"{tag}"%')
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def forget(
        self,
        *,
        layer: MemoryLayer,
        scope_id: str,
        key: str | None = None,
        kind: str | None = None,
    ) -> int:
        await self._ensure_init()
        sql = "DELETE FROM memory WHERE layer=? AND scope_id=?"
        params: list[Any] = [layer.value, scope_id]
        if key:
            sql += " AND key=?"
            params.append(key)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(sql, params)
            await db.commit()
            return cur.rowcount or 0

    async def purge_expired(self) -> int:
        await self._ensure_init()
        now = _utcnow_iso()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM memory WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            await db.commit()
            return cur.rowcount or 0


def _row_to_record(row: tuple[Any, ...]) -> MemoryRecord:
    return MemoryRecord(
        id=row[0],
        layer=MemoryLayer(row[1]),
        scope_id=row[2],
        kind=row[3],
        key=row[4],
        value=json.loads(row[5]),
        tags=json.loads(row[6]),
        created_at=datetime.fromisoformat(row[7]) if row[7] else None,
        expires_at=datetime.fromisoformat(row[8]) if row[8] else None,
    )
