"""LLM-as-validator: проверка draft answer Worker по tool trace."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from agent_core import build_llm, resolve_model_aliases

logger = logging.getLogger("validator_agent")

DEFAULT_PROMPT_PATH = Path("prompts/validator_system.txt")

LLMInvoke = Callable[[List[Any]], Awaitable[Any]]


@dataclass
class ValidatorCriteria:
    """Опциональные критерии сценария для чеклиста валидатора."""

    expect_contains: List[str] = field(default_factory=list)
    forbid_contains: List[str] = field(default_factory=list)
    expect_tools: List[str] = field(default_factory=list)
    forbid_tools: List[str] = field(default_factory=list)
    require_policy_ok: bool = False


#: Известные коды причин reject. Строки за пределами этого множества
#: конвертируются в code="other" с detail=исходная строка.
KNOWN_REASON_CODES = {
    "request_mismatch",
    "tool_not_called",
    "forbidden_tool_called",
    "fact_inconsistent",
    "format_missing",
    "false_success",
    "missing_tool_result",
    "other",
}


@dataclass
class ValidationReason:
    """Структурированная причина reject: код из фиксированного enum + пояснение."""

    code: str = "other"
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.code}: {self.detail}" if self.detail else self.code

    def __eq__(self, other: Any) -> bool:
        # Обратная совместимость: старые тесты/вызовы сравнивают reasons
        # напрямую со списком строк (см. reasons=["forbidden tool"]).
        # Строка считается равной ValidationReason, если совпадает с её
        # исходным текстом (detail, либо code при отсутствии detail).
        if isinstance(other, ValidationReason):
            return self.code == other.code and self.detail == other.detail
        if isinstance(other, str):
            return other == (self.detail or self.code)
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.code, self.detail))


def _coerce_reason(item: Any) -> ValidationReason:
    """Приводит произвольный элемент reasons к ValidationReason.

    Поддерживает:
    - ValidationReason как есть
    - dict {"code": ..., "detail": ...}
    - произвольную строку (старый формат) → code="other" если строка не входит
      в KNOWN_REASON_CODES, иначе code=строка, detail=строка
    """
    if isinstance(item, ValidationReason):
        return item
    if isinstance(item, dict):
        code = str(item.get("code") or "other")
        detail = str(item.get("detail") or "")
        if code not in KNOWN_REASON_CODES:
            # неизвестный код от LLM — сохраняем как detail, помечаем other
            detail = detail or code
            code = "other"
        return ValidationReason(code=code, detail=detail)
    text = str(item)
    code = text if text in KNOWN_REASON_CODES else "other"
    return ValidationReason(code=code, detail=text)


@dataclass
class ValidationResult:
    """Результат проверки Validator."""

    approved: bool
    feedback: str = ""
    reasons: List["ValidationReason"] = field(default_factory=list)
    raw_response: str = ""

    def __post_init__(self) -> None:
        # Обратная совместимость: старые вызовы передают reasons как
        # List[str]. Конвертируем в List[ValidationReason], сохраняя
        # семантику "простая строка" через _coerce_reason.
        self.reasons = [_coerce_reason(r) for r in (self.reasons or [])]


def load_validator_prompt(prompt_path: Path = DEFAULT_PROMPT_PATH) -> str:
    """Загружает системный промпт валидатора."""
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8").strip()
    return "Ты — Validator. Верни JSON: approved, feedback, reasons."


def build_tool_trace(
    messages: List[Dict[str, Any]],
    tool_call_details: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Собирает tool trace из сообщений хода: вызов → результат.

    Args:
        messages: История сообщений (включая assistant tool_calls и tool results)
        tool_call_details: Дополнительные детали вызовов (fallback по имени)

    Returns:
        Список событий trace с полями tool, arguments, result, status
    """
    details_by_name: Dict[str, Dict[str, Any]] = {}
    for item in tool_call_details or []:
        name = item.get("name", "")
        if name:
            details_by_name[name] = item

    trace: List[Dict[str, Any]] = []
    pending: Dict[str, Dict[str, Any]] = {}

    for msg in messages:
        role = msg.get("role", "")
        if role in ("assistant", "ai"):
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id") or tc.get("name", "")
                name = tc.get("name", "")
                args = tc.get("arguments") or tc.get("args") or {}
                if not name:
                    continue
                entry = {
                    "tool": name,
                    "arguments": args,
                    "result": None,
                    "status": "pending",
                }
                if tc_id:
                    pending[tc_id] = entry
                else:
                    trace.append(entry)
        elif role == "tool":
            tc_id = msg.get("tool_call_id", "")
            content = str(msg.get("content") or "")
            status = "error" if content.strip().lower().startswith("error") else "ok"
            if tc_id and tc_id in pending:
                entry = pending.pop(tc_id)
                entry["result"] = content
                entry["status"] = status
                trace.append(entry)
            elif trace and trace[-1].get("result") is None:
                trace[-1]["result"] = content
                trace[-1]["status"] = status

    for entry in pending.values():
        entry["status"] = "missing_result"
        trace.append(entry)

    if not trace and details_by_name:
        for name, item in details_by_name.items():
            trace.append({
                "tool": name,
                "arguments": item.get("arguments") or {},
                "result": item.get("result"),
                "status": item.get("status", "unknown"),
            })

    return trace


def format_validation_payload(
    user_message: str,
    draft_answer: str,
    tool_trace: List[Dict[str, Any]],
    criteria: Optional[ValidatorCriteria] = None,
) -> str:
    """Формирует user-payload для LLM-валидатора."""
    crit = criteria or ValidatorCriteria()
    payload = {
        "user_message": user_message,
        "draft_answer": draft_answer,
        "tool_trace": tool_trace,
        "criteria": {
            "expect_contains": crit.expect_contains,
            "forbid_contains": crit.forbid_contains,
            "expect_tools": crit.expect_tools,
            "forbid_tools": crit.forbid_tools,
            "require_policy_ok": crit.require_policy_ok,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def parse_validator_response(text: str) -> ValidationResult:
    """
    Парсит JSON-ответ валидатора.

    Поддерживает чистый JSON или JSON внутри markdown-блока.
    """
    raw = (text or "").strip()
    if not raw:
        return ValidationResult(
            approved=False,
            feedback="Пустой ответ валидатора",
            reasons=["empty_validator_response"],
            raw_response=raw,
        )

    candidates = [raw]
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        candidates.insert(0, fence.group(1))
    brace = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        approved = bool(data.get("approved"))
        feedback = str(data.get("feedback") or "")
        reasons_raw = data.get("reasons") or []
        if not isinstance(reasons_raw, list):
            reasons_raw = [reasons_raw]
        reasons = [_coerce_reason(r) for r in reasons_raw]
        return ValidationResult(
            approved=approved,
            feedback=feedback,
            reasons=reasons,
            raw_response=raw,
        )

    return ValidationResult(
        approved=False,
        feedback="Не удалось разобрать ответ валидатора",
        reasons=["invalid_validator_json"],
        raw_response=raw,
    )


#: Actionable подсказка для Worker'а по каждому известному коду reason.
_RETRY_HINTS: Dict[str, str] = {
    "request_mismatch": "Перечитай вопрос пользователя внимательно и ответь именно на него.",
    "tool_not_called": "Вызови обязательный инструмент перед тем как отвечать.",
    "forbidden_tool_called": "Используй официальный инструмент вместо запрещённого/decoy.",
    "fact_inconsistent": "Процитируй точное значение из результата инструмента, не изменяй его.",
    "format_missing": "Добавь обязательный формат/маркер в конец ответа.",
    "false_success": "Инструмент вернул ошибку — сообщи об этом честно, не утверждай успех.",
    "missing_tool_result": "Дождись результата инструмента перед тем как отвечать.",
    "other": "",
}


#: Маппинг rule_id из symbolic_precheck.SymbolicViolation → код ValidationReason,
#: чтобы tier-1 (символические) и tier-2 (LLM) отказы давали одинаково
#: структурированный retry-feedback через format_retry_feedback().
SYMBOLIC_RULE_TO_REASON_CODE: Dict[str, str] = {
    "deny_decoy_tools": "forbidden_tool_called",
    "no_false_success_after_tool_error": "false_success",
    "policy_ok_requires_fact": "format_missing",
    "no_untraceable_markers": "fact_inconsistent",
    "missing_tool_result": "missing_tool_result",
}


def validation_result_from_symbolic_violation(violation: Any) -> ValidationResult:
    """
    Конвертирует SymbolicViolation (symbolic_precheck.py, tier-1 проверка без
    LLM) в ValidationResult — чтобы retry-pipeline в agent_core.py обрабатывал
    tier-1 и tier-2 (LLM) отказы одинаково через format_retry_feedback().
    """
    code = SYMBOLIC_RULE_TO_REASON_CODE.get(violation.rule_id, "other")
    return ValidationResult(
        approved=False,
        feedback=violation.message,
        reasons=[ValidationReason(code=code, detail=violation.message)],
        raw_response="",
    )


def format_retry_feedback(result: ValidationResult) -> str:
    """
    Формирует actionable retry-сообщение для Worker'а на основе структурированных reasons.

    Для каждой уникальной reason.code добавляет конкретную подсказку из
    _RETRY_HINTS, плюс detail из reason (если есть), плюс общий feedback
    валидатора (если есть и не дублирует уже показанные detail).

    Args:
        result: ValidationResult с approved=False и (опционально) reasons.

    Returns:
        Многострочная строка вида:

        [VALIDATOR REJECTED]
        - tool_not_called: get_policy_fact не вызван → Вызови обязательный инструмент ...
        - format_missing: нет [POLICY_OK] → Добавь обязательный формат/маркер ...

        Исправь ответ с учётом замечаний валидатора.

        Если reasons пуст, используется только общий feedback (или дефолтная фраза).
    """
    lines: List[str] = ["[VALIDATOR REJECTED]"]

    seen_codes: set = set()
    bullet_added = False
    for reason in result.reasons or []:
        code = getattr(reason, "code", None) or "other"
        detail = getattr(reason, "detail", "") or ""
        dedup_key = (code, detail)
        if dedup_key in seen_codes:
            continue
        seen_codes.add(dedup_key)

        hint = _RETRY_HINTS.get(code, _RETRY_HINTS["other"])
        label = f"{code}: {detail}" if detail else code
        if hint:
            lines.append(f"- {label} → {hint}")
        else:
            lines.append(f"- {label}")
        bullet_added = True

    if result.feedback:
        lines.append(result.feedback)
    elif not bullet_added:
        lines.append("Ответ отклонён валидатором без детальных причин.")

    lines.append("")
    lines.append("Исправь ответ с учётом замечаний валидатора.")
    return "\n".join(lines)


def validator_enabled_for_tags(config: Dict[str, Any], scenario_tags: Optional[List[str]]) -> bool:
    """Проверяет, нужно ли запускать валидатор для набора тегов сценария."""
    vcfg = config.get("validator") or {}
    if vcfg.get("enabled") is True:
        return True
    enabled_tags = list(vcfg.get("enabled_for_tags") or [])
    if not enabled_tags:
        return False
    if not scenario_tags:
        return False
    return bool(set(enabled_tags) & set(scenario_tags))


def resolve_validator_model_alias(
    config: Dict[str, Any],
    worker_model_alias: Optional[str] = None,
) -> str:
    """Возвращает model_alias для валидатора."""
    vcfg = config.get("validator") or {}
    model = vcfg.get("model", "same_or_smaller")
    if model and model != "same_or_smaller":
        return str(model)
    if worker_model_alias:
        return worker_model_alias
    return resolve_model_aliases(config)[0]


class ValidatorAgent:
    """LLM-as-validator: approve/reject draft answer по tool trace."""

    def __init__(
        self,
        config: Dict[str, Any],
        *,
        prompt_path: Path = DEFAULT_PROMPT_PATH,
        model_alias: Optional[str] = None,
        llm_invoke: Optional[LLMInvoke] = None,
    ):
        self.config = config
        self.prompt_path = prompt_path
        self.model_alias = resolve_validator_model_alias(config, model_alias)
        self._system_prompt = load_validator_prompt(prompt_path)
        self._llm_invoke = llm_invoke

    def is_enabled_for_tags(self, scenario_tags: Optional[List[str]]) -> bool:
        """Нужен ли валидатор для тегов сценария."""
        return validator_enabled_for_tags(self.config, scenario_tags)

    async def _call_llm(self, user_payload: str) -> str:
        if self._llm_invoke is not None:
            result = await self._llm_invoke([
                SystemMessage(content=self._system_prompt),
                HumanMessage(content=user_payload),
            ])
            return str(getattr(result, "content", result) or "")

        llm = await build_llm(self.config, self.model_alias)
        response = await llm.ainvoke([
            SystemMessage(content=self._system_prompt),
            HumanMessage(content=user_payload),
        ])
        return str(response.content or "")

    async def validate(
        self,
        user_message: str,
        draft_answer: str,
        tool_trace: List[Dict[str, Any]],
        *,
        criteria: Optional[ValidatorCriteria] = None,
    ) -> ValidationResult:
        """
        Проверяет черновик ответа Worker.

        Args:
            user_message: Исходный запрос пользователя
            draft_answer: Черновик ответа Worker
            tool_trace: Трейс вызовов инструментов
            criteria: Опциональные критерии сценария

        Returns:
            ValidationResult с approved/reject и feedback
        """
        payload = format_validation_payload(
            user_message, draft_answer, tool_trace, criteria,
        )
        raw = await self._call_llm(payload)
        result = parse_validator_response(raw)
        logger.info(
            "Validator: approved=%s reasons=%s",
            result.approved,
            result.reasons,
        )
        return result
