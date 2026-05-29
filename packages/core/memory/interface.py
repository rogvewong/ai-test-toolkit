"""Layered memory interface: session / project / org.

Design principle: the LLM is NOT the storage. Everything persistent lives here.
The 3 layers:
  * session — scoped to one run_id; short-lived; holds turn-by-turn context.
  * project — scoped to project_id; durable; holds prior reports, conventions, decisions.
  * org     — scoped to tenant_id; cross-project; holds standards, glossaries, golden sets.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class MemoryLayer(str, Enum):
    SESSION = "session"
    PROJECT = "project"
    ORG = "org"


@dataclass
class MemoryRecord:
    id: str
    layer: MemoryLayer
    scope_id: str  # run_id | project_id | tenant_id
    kind: str  # e.g. "report", "turn", "decision", "golden_sample"
    key: str
    value: dict[str, Any]
    tags: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    expires_at: datetime | None = None
    relevance_score: float | None = None


class MemoryStore(ABC):
    """Abstract storage contract. Implementations: SQLite (MVP), Postgres+pgvector (scale)."""

    @abstractmethod
    async def save(self, record: MemoryRecord) -> str: ...

    @abstractmethod
    async def get(
        self, layer: MemoryLayer, scope_id: str, key: str
    ) -> MemoryRecord | None: ...

    @abstractmethod
    async def search(
        self,
        *,
        layer: MemoryLayer,
        scope_id: str,
        query: str | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]: ...

    @abstractmethod
    async def forget(
        self,
        *,
        layer: MemoryLayer,
        scope_id: str,
        key: str | None = None,
        kind: str | None = None,
    ) -> int: ...

    @abstractmethod
    async def purge_expired(self) -> int: ...
