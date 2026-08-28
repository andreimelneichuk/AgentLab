"""Политика извлечения и хранения фактов."""
from __future__ import annotations

import re
from typing import Optional, Pattern

# Типы фактов, разрешённые к хранению.
ALLOWED_FACT_TYPES = frozenset({"preference", "entity_id", "decision"})

# Типы с ограниченным TTL (time-sensitive, например policy).
TTL_BY_TYPE: dict[str, int] = {
    "policy": 3600,
    "entity_id": 86400 * 30,
}

DEFAULT_TTL_BY_TYPE: dict[str, Optional[int]] = {
    "preference": None,
    "entity_id": 86400 * 30,
    "decision": 86400 * 7,
    "policy": 3600,
}

NEVER_STORE_PATTERNS: list[Pattern[str]] = [
    re.compile(r"<tool_call", re.I),
    re.compile(r"</?function", re.I),
    re.compile(r"Error executing \w+:", re.I),
    re.compile(r"Error: unknown tool", re.I),
    re.compile(r'^\s*\{[\s\S]*"tool_calls"', re.M),
    re.compile(r"traceback \(most recent call last\)", re.I),
]

PREFERENCE_PATTERNS: list[tuple[Pattern[str], str]] = [
    (re.compile(r"(?:я\s+)?предпочитаю\s+кратк\w*", re.I), "preference_brevity"),
    (re.compile(r"(?:я\s+)?предпочитаю\s+(.+?)(?:\.|$)", re.I), "preference_language"),
    (re.compile(r"используй\s+(\w+)[,\s]+не\s+(\w+)", re.I), "preference_tool_choice"),
    (re.compile(r"(?:отвечай|пиши)\s+(?:на\s+)?(\w+)", re.I), "preference_response_language"),
    (re.compile(r"формат\s+ответа[:\s]+(.+?)(?:\.|$)", re.I), "preference_format"),
]

ENTITY_ID_PATTERNS: list[tuple[Pattern[str], str]] = [
    (re.compile(r"employee_id\s*[=:]\s*(\S+)", re.I), "employee_id"),
    (re.compile(r"policy_code\s*[=:]\s*(\S+)", re.I), "policy_code"),
    (re.compile(r"(?:мой|мо[йё]й)\s+employee_id[:\s]+(\S+)", re.I), "employee_id"),
]

DECISION_PATTERNS: list[tuple[Pattern[str], str]] = [
    (re.compile(r"(?:в прошлый раз|ранее)\s+(?:использовали|вызывали)\s+(\w+)", re.I), "tool_decision"),
    (re.compile(r"решили\s+использовать\s+(\w+)", re.I), "tool_decision"),
]


def should_never_store(text: str) -> bool:
    """Проверяет, что текст нельзя сохранять (tool XML, ошибки и т.д.)."""
    stripped = (text or "").strip()
    if not stripped:
        return True
    return any(p.search(stripped) for p in NEVER_STORE_PATTERNS)


def ttl_for_type(fact_type: str, override: Optional[int] = None) -> Optional[int]:
    """Возвращает TTL в секундах для типа факта."""
    if override is not None:
        return override
    return DEFAULT_TTL_BY_TYPE.get(fact_type)


def is_allowed_fact_type(fact_type: str) -> bool:
    return fact_type in ALLOWED_FACT_TYPES or fact_type == "policy"
