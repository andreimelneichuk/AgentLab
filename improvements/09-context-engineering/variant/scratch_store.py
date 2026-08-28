"""Внешнее хранилище (scratch pad) для больших ответов MCP/инструментов."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional


def estimate_tokens(text: str) -> int:
    """Грубая оценка числа токенов (≈4 символа на токен)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


@dataclass
class ScratchStore:
    """
    In-memory scratch pad для результатов инструментов, не помещающихся в окно контекста.

    Полный payload хранится вне message list; в контекст попадает только ссылка.
    """

    _entries: Dict[str, str] = field(default_factory=dict)

    def make_key(self, prefix: str = "scratch") -> str:
        """Генерирует уникальный ключ scratch."""
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", prefix)[:32]
        return f"{safe}_{uuid.uuid4().hex[:8]}"

    def save(self, key: str, content: str) -> str:
        """
        Сохраняет содержимое по ключу.

        Returns:
            Ключ записи (нормализованный).
        """
        key = (key or "").strip() or self.make_key()
        self._entries[key] = content
        return key

    def load(self, key: str) -> Optional[str]:
        """Загружает содержимое по ключу или None."""
        return self._entries.get(key)

    def has(self, key: str) -> bool:
        """Проверяет наличие ключа."""
        return key in self._entries

    def delete(self, key: str) -> bool:
        """Удаляет запись; возвращает True если ключ существовал."""
        return self._entries.pop(key, None) is not None

    def keys(self) -> list[str]:
        """Список всех ключей."""
        return list(self._entries.keys())
