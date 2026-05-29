"""Layered memory facade.

Typical call pattern:
    mem = LayeredMemory(store, run_id=..., project_id=..., tenant_id=...)
    await mem.session.save(kind="turn", key="step1.1", value={...})
    prior = await mem.project.search(kind="report", query="登录")
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from packages.core.memory.interface import MemoryLayer, MemoryRecord, MemoryStore


@dataclass
class LayerHandle:
    store: MemoryStore
    layer: MemoryLayer
    scope_id: str

    async def save(
        self,
        *,
        kind: str,
        key: str,
        value: dict[str, Any],
        tags: list[str] | None = None,
        ttl_seconds: int | None = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds) if ttl_seconds else None
        record = MemoryRecord(
            id=str(uuid4()),
            layer=self.layer,
            scope_id=self.scope_id,
            kind=kind,
            key=key,
            value=value,
            tags=tags or [],
            created_at=now,
            expires_at=expires_at,
        )
        return await self.store.save(record)

    async def get(self, key: str) -> MemoryRecord | None:
        return await self.store.get(self.layer, self.scope_id, key)

    async def search(
        self,
        *,
        query: str | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        return await self.store.search(
            layer=self.layer,
            scope_id=self.scope_id,
            query=query,
            kind=kind,
            tags=tags,
            limit=limit,
        )

    async def forget(
        self, *, key: str | None = None, kind: str | None = None
    ) -> int:
        return await self.store.forget(
            layer=self.layer, scope_id=self.scope_id, key=key, kind=kind
        )


@dataclass
class LayeredMemory:
    store: MemoryStore
    run_id: str
    project_id: str
    tenant_id: str = "default"

    @property
    def session(self) -> LayerHandle:
        return LayerHandle(self.store, MemoryLayer.SESSION, self.run_id)

    @property
    def project(self) -> LayerHandle:
        return LayerHandle(self.store, MemoryLayer.PROJECT, self.project_id)

    @property
    def org(self) -> LayerHandle:
        return LayerHandle(self.store, MemoryLayer.ORG, self.tenant_id)
