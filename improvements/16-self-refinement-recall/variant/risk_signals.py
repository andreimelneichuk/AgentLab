"""Детектор риска по содержанию хода — production-совместимая альтернатива
tag-based активации валидатора (см. validator_agent.validator_enabled_for_tags).

В тестовом harness (benchmark/compare.py) решение "нужен ли валидатор"
принимается по scenario_tags — метаинформации, которой в продакшене просто
негде взяться (никто не размечает реальные запросы пользователей тегами
"critical"/"adversarial" заранее). Этот модуль вместо тегов анализирует
сам ход: сообщение пользователя, черновик ответа Worker'а и trace вызовов
инструментов — и решает нужна ли дорогая LLM-валидация.

Эвристики опираются на реальные MCP tools и сценарии бенчмарка:
- benchmark/mcp_tool_registry.py — состав tools (read-only lookup vs
  side-effecting create/quote, decoy_* приманки, policy/security маркеры).
- benchmark/scenarios/catalog/s10_adversarial.yaml, s09_instruction.yaml —
  реальные adversarial-формулировки ("подтверди", "просто кивни",
  "другой бот уже сказал", "с этого момента каждое правило", policy-факты
  вида violet-42/orchid-17 и т.п.).

Этот файл самодостаточен и не импортирует validator_agent/agent_core —
интеграция с ValidatorAgent делается отдельно.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Инструменты, вызов которых создаёт/меняет внешнее состояние (не read-only
# lookup) — см. register_crm_tools в benchmark/mcp_tool_registry.py.
# Ошибка валидатора здесь дороже: тикет или коммерческое предложение уже
# "ушли наружу", в отличие от простого lookup, который можно перезапросить.
SIDE_EFFECTING_TOOLS = frozenset({
    "ticket_create",
    "sales_quote",
})

# Слова, сигнализирующие о policy/security-контексте — упоминание в
# user_message или draft_answer повышает риск того, что ответ либо
# раскрывает то, что не должен (POLICY_FACT=violet-42 и т.п.), либо
# ошибочно ссылается на устаревший/decoy источник (POLICY_FACT=violet-99).
_POLICY_SECURITY_KEYWORDS = (
    "политик",
    "policy",
    "секрет",
    "код доступа",
    "confidential",
    "security",
)

# Adversarial/injection-паттерны, извлечённые из реальных сценариев
# benchmark/scenarios/catalog/s10_adversarial.yaml (давление подтвердить
# несуществующий секрет, "просто кивни") и s09_instruction.yaml (попытки
# навязать постоянное правило поведения на весь диалог).
_ADVERSARIAL_PATTERNS = (
    "игнорируй инструкции",
    "игнорируй предыдущие",
    "забудь про",
    "забудь все",
    "новая сессия",
    "администратор системы",
    "не нужно вызывать инструмент",
    "не вызывай инструмент",
    "между нами",
    "просто кивни",
    "просто подтверди",
    "другой бот уже сказал",
    "другой ассистент уже сказал",
    "с этого момента",
    "с этого момента каждый ответ",
    "на всю беседу одна договорённость",
    "just confirm you got it",
)

# Денежные суммы: "$1,234.56" или явные benchmark-маркеры price_usd=/AMOUNT=
# (см. invoice_get → "AMOUNT=12500.00", sales_quote → "price_usd=...").
_MONEY_PATTERN = re.compile(r"\$?\d[\d,]*\.\d{2}\b|price_usd\s*=\s*\d+|AMOUNT\s*=\s*\d", re.IGNORECASE)

# Грубая эвристика "слово-число" маркера (например violet-42, orchid-17,
# amber-91 — см. get_policy_fact/benchmark_probe/benchmark_sse_probe в
# mcp_tool_registry.py). Полноценная marker-traceability логика — отдельная
# задача (symbolic_precheck.py); здесь достаточно грубой проверки.
_WORD_NUMBER_MARKER_PATTERN = re.compile(r"\b[a-zA-Z]+-\d{1,4}\b")


@dataclass
class RiskSignal:
    """Результат детекции риска по содержанию хода."""

    triggered: bool
    reasons: List[str] = field(default_factory=list)


def _has_tool_error(tool_trace: List[Dict[str, Any]]) -> bool:
    return any((entry or {}).get("status") == "error" for entry in tool_trace)


def _contains_policy_security_context(*texts: str) -> bool:
    combined = " ".join(texts).lower()
    return any(keyword in combined for keyword in _POLICY_SECURITY_KEYWORDS)


def _has_side_effecting_tool_call(tool_trace: List[Dict[str, Any]]) -> bool:
    return any((entry or {}).get("tool") in SIDE_EFFECTING_TOOLS for entry in tool_trace)


def _has_financial_value(draft_answer: str) -> bool:
    return bool(_MONEY_PATTERN.search(draft_answer or ""))


def _has_adversarial_pattern(user_message: str) -> bool:
    lowered = (user_message or "").lower()
    return any(pattern in lowered for pattern in _ADVERSARIAL_PATTERNS)


def _has_marker_without_matching_tool(
    draft_answer: str, tool_trace: List[Dict[str, Any]],
) -> bool:
    if not _WORD_NUMBER_MARKER_PATTERN.search(draft_answer or ""):
        return False
    has_successful_call = any(
        (entry or {}).get("status") == "ok" for entry in tool_trace
    )
    return not has_successful_call


def detect_risk_signals(
    user_message: str,
    draft_answer: str,
    tool_trace: List[Dict[str, Any]],
) -> RiskSignal:
    """
    Определяет нужна ли LLM-валидация ПО СОДЕРЖАНИЮ хода — без scenario_tags.

    Работает и в продакшене: анализирует только то, что реально доступно
    на инференсе (текст пользователя, черновик ответа, trace вызовов
    инструментов), а не тестовую метаинформацию.

    Args:
        user_message: Исходный запрос пользователя.
        draft_answer: Черновик ответа Worker'а до отправки пользователю.
        tool_trace: Трейс вызовов инструментов формата
            [{"tool": str, "arguments": dict, "result": str|None,
              "status": "ok"|"error"|"pending"|"missing_result"}].

    Returns:
        RiskSignal с triggered=True если хотя бы одна эвристика сработала,
        и человекочитаемыми reasons для логов/debug.
    """
    trace = tool_trace or []
    reasons: List[str] = []

    if _has_tool_error(trace):
        reasons.append("tool_error_present")

    if _contains_policy_security_context(user_message, draft_answer):
        reasons.append("policy_context")

    if _has_side_effecting_tool_call(trace):
        reasons.append("side_effecting_tool_called")

    if _has_financial_value(draft_answer):
        reasons.append("financial_value_present")

    if _has_adversarial_pattern(user_message):
        reasons.append("adversarial_injection_pattern")

    if _has_marker_without_matching_tool(draft_answer, trace):
        reasons.append("marker_without_matching_tool")

    return RiskSignal(triggered=bool(reasons), reasons=reasons)


def should_validate(
    *,
    scenario_tags: Optional[List[str]] = None,
    enabled_for_tags: Optional[List[str]] = None,
    user_message: str = "",
    draft_answer: str = "",
    tool_trace: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """
    True если сработали либо старые tag-based критерии, либо новые risk
    signals — объединяет старый механизм (validator_agent.validator_enabled_for_tags)
    с новым content-based детектором без изменения старого кода.

    Args:
        scenario_tags: Теги сценария (тестовая метаинформация, может
            отсутствовать в продакшене — тогда только risk signals решают).
        enabled_for_tags: Список тегов, для которых валидатор включён
            конфигом (аналог config["validator"]["enabled_for_tags"]).
        user_message: Исходный запрос пользователя.
        draft_answer: Черновик ответа Worker'а.
        tool_trace: Трейс вызовов инструментов.

    Returns:
        True если нужен LLM-валидатор.
    """
    tag_triggered = bool(
        enabled_for_tags and scenario_tags and set(enabled_for_tags) & set(scenario_tags)
    )
    if tag_triggered:
        return True

    signal = detect_risk_signals(user_message, draft_answer, tool_trace or [])
    return signal.triggered
