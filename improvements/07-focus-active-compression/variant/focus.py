"""Семантический индекс фактов сессии: Knowledge Index для быстрого поиска без повторных tool calls."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

KNOWLEDGE_INDEX_LOOKUP = "knowledge_index_lookup"

Outcome = Literal["success", "blocked", "partial"]

VALID_OUTCOMES = frozenset({"success", "blocked", "partial"})

# Категории которые автоматически индексируются при tool response
AUTO_INDEX_CATEGORIES = {
    "get_policy_fact": ("policy", "POLICY_FACT"),
    "benchmark_probe": ("marker", "BENCH_MARKER_STREAMABLE"),
    "benchmark_sse_probe": ("marker_sse", "BENCH_MARKER_SSE"),
    "employee_lookup": ("employee", "EMP_ID"),
    "customer_get": ("customer", "CUSTOMER"),
    "invoice_get": ("invoice", "INVOICE"),
    "sse_audit_log": ("audit", "AUDIT"),
}


class FocusError(Exception):
    """Ошибка Knowledge Index или логики фокуса."""


@dataclass
class IndexEntry:
    """Одна запись в семантическом индексе — с полным форматом для цитирования."""

    id: str
    key: str  # "violet-42", "HR-001", etc.
    category: str  # "policy", "employee", "marker", "customer", "invoice"
    summary: str  # Краткое описание (one-liner)
    full_format: str  # Точный формат как вернул инструмент
    from_tool: str  # "get_policy_fact", "employee_lookup", etc.
    turn_index: int  # На каком ходе получено
    confidence: Literal["high", "medium", "low"] = "high"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "category": self.category,
            "summary": self.summary,
            "full_format": self.full_format,
            "from_tool": self.from_tool,
            "turn_index": self.turn_index,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IndexEntry":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            key=str(data.get("key") or ""),
            category=str(data.get("category") or ""),
            summary=str(data.get("summary") or ""),
            full_format=str(data.get("full_format") or ""),
            from_tool=str(data.get("from_tool") or ""),
            turn_index=int(data.get("turn_index") or 0),
            confidence=data.get("confidence", "high"),  # type: ignore[arg-type]
        )


@dataclass
class KnowledgeIndex:
    """Семантический индекс фактов сессии, организованный по категориям."""

    index: Dict[str, Dict[str, IndexEntry]] = field(
        default_factory=dict
    )  # {category: {key: entry}}

    def add_entry(self, entry: IndexEntry) -> None:
        """Добавить запись в индекс. Перезаписывает если key уже существует."""
        if entry.category not in self.index:
            self.index[entry.category] = {}
        self.index[entry.category][entry.key] = entry

    def lookup(self, category: str, key: str) -> Optional[IndexEntry]:
        """Поиск по категории и ключу."""
        return self.index.get(category, {}).get(key)

    def lookup_by_category(self, category: str) -> Dict[str, IndexEntry]:
        """Все entries в категории."""
        return dict(self.index.get(category, {}))

    def to_dict(self) -> Dict[str, Any]:
        return {
            cat: {k: e.to_dict() for k, e in entries.items()}
            for cat, entries in self.index.items()
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeIndex":
        kb = cls()
        for category, entries_dict in (data or {}).items():
            for key, entry_data in entries_dict.items():
                if isinstance(entry_data, dict):
                    entry = IndexEntry.from_dict(entry_data)
                    kb.add_entry(entry)
        return kb

    def to_context_text(self) -> str:
        """Форматировать индекс для инжекции в system context."""
        if not self.index:
            return ""
        return "### KNOWLEDGE INDEX\n```json\n" + self.to_json() + "\n```"

    @property
    def size(self) -> int:
        """Общее количество entries в индексе."""
        return sum(len(entries) for entries in self.index.values())


class KnowledgeIndexManager:
    """Управление Knowledge Index сессии с автоматической индексацией tool responses."""

    def __init__(self) -> None:
        self.knowledge = KnowledgeIndex()

    @property
    def knowledge_dict(self) -> Dict[str, Any]:
        return self.knowledge.to_dict()

    def lookup(self, category: str, key: str) -> Optional[IndexEntry]:
        """Поиск записи в индексе."""
        return self.knowledge.lookup(category, key)

    def auto_index_from_tool_response(
        self, tool_name: str, tool_response: str, turn_index: int
    ) -> None:
        """
        Автоматически индексировать результат tool-а.

        Парсит response и добавляет relevant entries в Knowledge Index.
        """
        if tool_name not in AUTO_INDEX_CATEGORIES:
            return

        category, marker = AUTO_INDEX_CATEGORIES[tool_name]

        # Парс типичных форматов
        if marker in tool_response:
            # Найти значение маркера
            lines = tool_response.split("\n")
            for line in lines:
                if marker in line:
                    # Примеры:
                    # "POLICY_FACT=violet-42. Правило: ..."
                    # "EMP_ID=HR-001 NAME=Alice DEPT=Engineering"
                    # "CUSTOMER=C-001 TIER=gold REGION=EMEA"
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        value_part = parts[1]
                        # Извлечь первую часть до пробела или точки
                        key = value_part.split()[0].rstrip(".,;:")

                        entry = IndexEntry(
                            id=uuid.uuid4().hex[:12],
                            key=key,
                            category=category,
                            summary=f"{marker}={key}",
                            full_format=tool_response,
                            from_tool=tool_name,
                            turn_index=turn_index,
                            confidence="high",
                        )
                        self.knowledge.add_entry(entry)
                        break

    def handle_lookup_request(
        self, category: str, key: str
    ) -> Dict[str, Any]:
        """Handle pseudo-tool knowledge_index_lookup request from LLM."""
        entry = self.knowledge.lookup(category, key)
        if entry:
            return {
                "found": True,
                "category": category,
                "key": key,
                "summary": entry.summary,
                "full_format": entry.full_format,
                "from_tool": entry.from_tool,
                "turn_index": entry.turn_index,
                "note": "Use full_format field for accurate citation",
            }
        return {
            "found": False,
            "category": category,
            "key": key,
            "note": "Fact not in index, call the appropriate tool to retrieve it",
        }


def knowledge_index_lookup_schema() -> Dict[str, Any]:
    """OpenAI-схема pseudo-tool для LLM."""
    return {
        "name": KNOWLEDGE_INDEX_LOOKUP,
        "description": (
            "Поиск факта в Knowledge Index перед вызовом инструмента. "
            "Если факт в индексе, используй full_format field для цитирования. "
            "Если не найден, вызови соответствующий инструмент."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "policy",
                        "marker",
                        "marker_sse",
                        "employee",
                        "customer",
                        "invoice",
                        "audit",
                        "marker_random",
                    ],
                    "description": "Категория для поиска",
                },
                "key": {
                    "type": "string",
                    "description": "Ключ для поиска (например violet-42, HR-001, orchid-17)",
                },
            },
            "required": ["category", "key"],
        },
    }


def is_knowledge_index_tool(name: str) -> bool:
    return name == KNOWLEDGE_INDEX_LOOKUP
