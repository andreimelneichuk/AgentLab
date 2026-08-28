"""Модели данных для memory formation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExtractedFact:
    """Факт, извлечённый из диалога до записи в хранилище."""

    key: str
    content: str
    fact_type: str
    ttl_seconds: Optional[int] = None


@dataclass
class MemoryFact:
    """Каноническая запись в долговременной памяти пользователя."""

    id: str
    user_id: str
    key: str
    content: str
    fact_type: str
    created_at: float
    updated_at: float
    session_id: str = ""
    expires_at: Optional[float] = None
    keywords: str = ""
    embedding: Optional[list[float]] = field(default=None, repr=False)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        import time
        return time.time() > self.expires_at

    def to_context_line(self) -> str:
        return f"- [{self.fact_type}] {self.content}"
