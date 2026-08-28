"""Извлечение дискретных фактов из диалога."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from memory.models import ExtractedFact
from memory.policy import (
    DECISION_PATTERNS,
    ENTITY_ID_PATTERNS,
    PREFERENCE_PATTERNS,
    is_allowed_fact_type,
    should_never_store,
    ttl_for_type,
)

logger = logging.getLogger("memory.extractor")

EXTRACTOR_PROMPT = """Из диалога извлеки дискретные факты о пользователе.
Разрешённые типы: preference, entity_id, decision.
НЕ извлекай: ошибки инструментов, сырой XML tool_call, временные сбои.

Верни JSON-массив объектов:
[{"key": "canonical_key", "content": "текст факта", "fact_type": "preference|entity_id|decision"}]

Если фактов нет — верни [].

Диалог:
{dialog}
"""


def _messages_to_dialog_text(messages: List[Dict[str, Any]]) -> str:
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        content = (msg.get("content") or "").strip()
        if not content or role == "tool":
            continue
        if should_never_store(content):
            continue
        label = "Пользователь" if role == "user" else "Ассистент"
        lines.append(f"{label}: {content}")
    return "\n".join(lines)


def _extract_by_rules(text: str) -> list[ExtractedFact]:
    facts: list[ExtractedFact] = []
    seen_keys: set[str] = set()

    for patterns, fact_type in (
        (PREFERENCE_PATTERNS, "preference"),
        (ENTITY_ID_PATTERNS, "entity_id"),
        (DECISION_PATTERNS, "decision"),
    ):
        for pattern, key_template in patterns:
            for match in pattern.finditer(text):
                if should_never_store(match.group(0)):
                    continue
                if key_template == "preference_tool_choice":
                    value = match.group(1).strip()
                    alt = match.group(2).strip()
                    content = f"Пользователь предпочитает {value}, не {alt}"
                elif key_template == "preference_brevity":
                    content = "Пользователь предпочитает краткие ответы"
                elif key_template == "preference_language":
                    value = match.group(1).strip().rstrip(".")
                    content = f"Пользователь предпочитает язык: {value}"
                elif key_template == "preference_response_language":
                    value = match.group(1).strip().rstrip(".")
                    content = f"Пользователь просит отвечать на: {value}"
                elif key_template == "preference_format":
                    value = match.group(1).strip().rstrip(".")
                    content = f"Предпочитаемый формат ответа: {value}"
                elif key_template in ("employee_id", "policy_code"):
                    value = match.group(1).strip().rstrip(".")
                    content = f"{key_template}={value}"
                elif key_template == "tool_decision":
                    tool = match.group(1).strip()
                    content = f"Ранее использовали инструмент: {tool}"
                else:
                    value = match.group(1).strip().rstrip(".")
                    content = f"{key_template}: {value}"

                norm_key = key_template
                if norm_key in seen_keys:
                    continue
                seen_keys.add(norm_key)
                facts.append(
                    ExtractedFact(
                        key=norm_key,
                        content=content,
                        fact_type=fact_type,
                        ttl_seconds=ttl_for_type(fact_type),
                    )
                )
    return facts


def _parse_llm_json(raw: str) -> list[ExtractedFact]:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    if not isinstance(data, list):
        return []

    facts: list[ExtractedFact] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        content = str(item.get("content") or "").strip()
        fact_type = str(item.get("fact_type") or "preference").strip()
        if not key or not content or should_never_store(content):
            continue
        if not is_allowed_fact_type(fact_type):
            continue
        facts.append(
            ExtractedFact(
                key=key,
                content=content,
                fact_type=fact_type,
                ttl_seconds=ttl_for_type(fact_type),
            )
        )
    return facts


class FactExtractor:
    """Rule-based + опциональный LLM extractor."""

    def __init__(self, *, use_llm: bool = False, llm=None):
        self.use_llm = use_llm
        self._llm = llm

    async def extract_from_messages(
        self,
        messages: List[Dict[str, Any]],
    ) -> list[ExtractedFact]:
        dialog = _messages_to_dialog_text(messages)
        if not dialog.strip():
            return []

        rule_facts = _extract_by_rules(dialog)
        if not self.use_llm or self._llm is None:
            return rule_facts

        try:
            prompt = EXTRACTOR_PROMPT.format(dialog=dialog)
            result = await self._llm.ainvoke(prompt)
            content = str(getattr(result, "content", result) or "")
            llm_facts = _parse_llm_json(content)
            merged = {f.key: f for f in rule_facts}
            for fact in llm_facts:
                merged[fact.key] = fact
            return list(merged.values())
        except Exception as exc:
            logger.warning("LLM extraction failed, using rules only: %s", exc)
            return rule_facts

    def extract_from_messages_sync(
        self,
        messages: List[Dict[str, Any]],
    ) -> list[ExtractedFact]:
        """Синхронная обёртка для unit-тестов (только rules)."""
        dialog = _messages_to_dialog_text(messages)
        return _extract_by_rules(dialog)
