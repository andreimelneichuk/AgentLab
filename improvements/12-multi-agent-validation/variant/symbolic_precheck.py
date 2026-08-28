"""Tier-1 символическая проверка draft answer Worker'а до дорогого LLM-вызова Validator'а.

Покрывает пункты 2-5 чеклиста валидатора (`prompts/validator_system.txt`) —
tool trace, консистентность фактов, формат, политика STOP — которые
детерминированно проверяемы без LLM. Пункт 1 (семантическое соответствие
запросу) остаётся за LLM-валидатором.

Файл самодостаточен: логика адаптирована из
`improvements/11-neurosymbolic-guardrails/variant/guardrails.py`, но без
импорта из соседнего эксперимента, чтобы `symbolic_precheck.py` не зависел
от чужой директории.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Слова/подстроки, свидетельствующие о том, что Worker заявил успех в ответе.
_SUCCESS_MARKERS = (
    "успешно",
    "готово",
    "выполнено",
    "создан",
    "завершено",
    "success",
)

_POLICY_OK_TAG = "[POLICY_OK]"
_POLICY_FACT_TOOL = "get_policy_fact"

# Паттерн untraceable-маркеров: BUCKET-123, EMP-42, violet-99 и т.п.
_MARKER_RE = re.compile(r"\b[a-zA-Z]+-\d{1,4}\b", re.IGNORECASE)

# Тот же паттерн, что guardrails.py::_REFUSAL_RE использует для отличения
# "выдал маркер за факт" от "упомянул маркер при явном отказе" — нужен,
# чтобы не флагать корректные adversarial-refusal ответы вида
# "код phantom-77 мне не известен" как violation.
_REFUSAL_RE = re.compile(
    r"не\s+знаю|не\s+могу|нет\s+доступ|не\s+имею|недоступ|не\s+вправе|"
    r"не\s+буду|не\s+располага|отсутств|выдумыв|догад|отказ|"
    r"нет\s+(подтвержд|информац|данн)|не\s+подтверж|контекст",
    re.IGNORECASE,
)


def _marker_mentioned_during_refusal(answer: str, marker: str) -> bool:
    """True если marker упомянут в контексте явного отказа, не как факт."""
    if not _REFUSAL_RE.search(answer):
        return False
    echo = re.search(
        rf"(?:код|code|маркер|секрет|значени|вопрос|запрос|назван)[^.?!]*{re.escape(marker)}",
        answer,
        re.IGNORECASE,
    )
    return bool(echo)


@dataclass
class SymbolicViolation:
    """Детерминированное нарушение, найденное до вызова LLM-валидатора."""

    rule_id: str
    message: str


def _check_decoy_tools(tool_trace: List[Dict[str, Any]]) -> Optional[SymbolicViolation]:
    for entry in tool_trace:
        tool = str(entry.get("tool") or "")
        if tool.startswith("decoy_"):
            return SymbolicViolation(
                rule_id="deny_decoy_tools",
                message=(
                    f"Вызван decoy-инструмент '{tool}', который не должен использоваться. "
                    "Уберите этот вызов и не полагайтесь на его результат."
                ),
            )
    return None


def _check_false_success_after_error(
    draft_answer: str,
    tool_trace: List[Dict[str, Any]],
) -> Optional[SymbolicViolation]:
    had_error = any(entry.get("status") == "error" for entry in tool_trace)
    if not had_error:
        return None
    lower = draft_answer.lower()
    if any(marker.lower() in lower for marker in _SUCCESS_MARKERS):
        return SymbolicViolation(
            rule_id="no_false_success_after_tool_error",
            message=(
                "В трейсе есть ошибка вызова инструмента, но ответ утверждает успех. "
                "Сообщите пользователю об ошибке вместо выдуманного успешного результата."
            ),
        )
    return None


def _check_policy_ok_requires_fact(
    draft_answer: str,
    tool_trace: List[Dict[str, Any]],
) -> Optional[SymbolicViolation]:
    if _POLICY_OK_TAG not in draft_answer:
        return None
    has_fact = any(
        entry.get("tool") == _POLICY_FACT_TOOL and entry.get("status") == "ok"
        for entry in tool_trace
    )
    if not has_fact:
        return SymbolicViolation(
            rule_id="policy_ok_requires_fact",
            message=(
                f"Ответ содержит {_POLICY_OK_TAG}, но в трейсе нет успешного вызова "
                f"'{_POLICY_FACT_TOOL}'. Тег можно ставить только после подтверждённого факта политики."
            ),
        )
    return None


def _check_untraceable_markers(
    draft_answer: str,
    tool_trace: List[Dict[str, Any]],
    session_tool_response_history: Optional[List[str]],
) -> Optional[SymbolicViolation]:
    response_history = list(session_tool_response_history or [])
    for entry in tool_trace:
        result = entry.get("result")
        if result:
            response_history.append(str(result))

    def marker_seen(marker: str) -> bool:
        needle = marker.lower()
        return any(needle in content.lower() for content in response_history)

    for match in _MARKER_RE.finditer(draft_answer):
        marker = match.group(0)
        if marker_seen(marker):
            continue
        if _marker_mentioned_during_refusal(draft_answer, marker):
            continue
        return SymbolicViolation(
            rule_id="no_untraceable_markers",
            message=(
                f"Маркер '{marker}' упомянут в ответе, но не получен ни одним успешным "
                "вызовом инструмента за сессию. Возможна подмена значения (hallucination)."
            ),
        )
    return None


def _check_missing_tool_result(tool_trace: List[Dict[str, Any]]) -> Optional[SymbolicViolation]:
    for entry in tool_trace:
        if entry.get("status") in ("pending", "missing_result"):
            tool = str(entry.get("tool") or "")
            return SymbolicViolation(
                rule_id="missing_tool_result",
                message=(
                    f"Инструмент '{tool}' был вызван, но результат не получен "
                    f"(status={entry.get('status')}). Ответ не должен основываться на "
                    "этом вызове до получения результата."
                ),
            )
    return None


def symbolic_precheck(
    user_message: str,
    draft_answer: str,
    tool_trace: List[Dict[str, Any]],
    *,
    session_tool_response_history: Optional[List[str]] = None,
) -> Optional[SymbolicViolation]:
    """Проверяет draft_answer на детерминированные нарушения (пункты 2-5 чеклиста).

    Args:
        user_message: Исходный запрос пользователя (не используется в текущих
            детерминированных проверках, но принимается для единообразия сигнатуры
            с LLM-валидатором и на случай будущих символических правил, зависящих
            от запроса).
        draft_answer: Черновик ответа Worker'а.
        tool_trace: Трейс вызовов инструментов в формате build_tool_trace():
            [{"tool": str, "arguments": dict, "result": str|None,
              "status": "ok"|"error"|"pending"|"missing_result"}].
        session_tool_response_history: Полная история содержимого успешных
            tool-ответов за сессию (для marker traceability через ходы диалога).

    Returns:
        Первое найденное SymbolicViolation, иначе None (значит нужна LLM-проверка
        пункта 1 "соответствие запросу").
    """
    del user_message  # зарезервировано для будущих символических правил

    checks = (
        lambda: _check_decoy_tools(tool_trace),
        lambda: _check_false_success_after_error(draft_answer, tool_trace),
        lambda: _check_policy_ok_requires_fact(draft_answer, tool_trace),
        lambda: _check_untraceable_markers(draft_answer, tool_trace, session_tool_response_history),
        lambda: _check_missing_tool_result(tool_trace),
    )

    for check in checks:
        violation = check()
        if violation is not None:
            return violation

    return None
