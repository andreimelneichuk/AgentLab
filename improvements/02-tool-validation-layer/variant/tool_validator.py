"""Pre-execution validation trace and helpers for variant 02.

Строгая проверка аргументов выполняется в tool_executor (strict=True).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from langchain_core.tools import BaseTool

from tool_executor import (
    SCHEMA_AUGMENTATIONS,
    build_schema_registry,
    extract_input_schema,
    merge_schema_augmentation,
    prepare_and_validate_arguments,
)

logger = logging.getLogger("tool_validator")

DEFAULT_MAX_VALIDATION_RETRIES = 3

# Re-export for tests and augmentations.
__all__ = [
    "DEFAULT_MAX_VALIDATION_RETRIES",
    "SCHEMA_AUGMENTATIONS",
    "ValidationRejection",
    "ValidationResult",
    "ToolValidator",
    "extract_json_schema",
    "format_validation_errors",
    "merge_schema_augmentation",
    "validation_limits",
    "validation_stop_message",
]

extract_json_schema = extract_input_schema


@dataclass
class ValidationResult:
    """Результат проверки аргументов tool call."""

    ok: bool
    errors: List[str] = field(default_factory=list)
    normalized_args: Optional[Dict[str, Any]] = None


@dataclass
class ValidationRejection:
    """Запись об отклонённом вызове для trace."""

    tool_name: str
    args: Dict[str, Any]
    errors: List[str]
    attempt: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "args": dict(self.args),
            "errors": list(self.errors),
            "attempt": self.attempt,
        }


def format_validation_errors(tool_name: str, errors: List[str]) -> str:
    """Формирует сообщение об ошибке валидации для feedback LLM."""
    details = "; ".join(errors)
    return (
        f"Validation failed for tool '{tool_name}': {details}. "
        "Исправьте аргументы согласно схеме инструмента и повторите вызов."
    )


def validation_stop_message(max_retries: int) -> str:
    """Сообщение пользователю при исчерпании попыток валидации."""
    return (
        "Не удалось выполнить инструмент: аргументы не прошли проверку схемы "
        f"после {max_retries} попыток. Задача остановлена."
    )


def validation_limits(config: Dict[str, Any]) -> int:
    """Читает max_retries из config.tools.validation."""
    validation_cfg = (config.get("tools") or {}).get("validation") or {}
    return int(validation_cfg.get("max_retries", DEFAULT_MAX_VALIDATION_RETRIES))


class ToolValidator:
    """Тонкая обёртка: реестр схем, trace отказов, делегирование в tool_executor."""

    def __init__(
        self,
        tools: List[BaseTool],
        *,
        max_retries: int = DEFAULT_MAX_VALIDATION_RETRIES,
    ):
        self.max_retries = max_retries
        self._schemas = build_schema_registry(tools)
        self._tools: Dict[str, BaseTool] = {tool.name: tool for tool in tools}
        self.rejections: List[ValidationRejection] = []

    @property
    def schema_registry(self) -> Dict[str, Dict[str, Any]]:
        """Реестр JSON Schema по имени инструмента."""
        return dict(self._schemas)

    def reset_trace(self) -> None:
        self.rejections.clear()

    def validate(self, tool_name: str, args: Dict[str, Any]) -> ValidationResult:
        """Проверяет аргументы через единый путь tool_executor (strict)."""
        tool = self._tools.get(tool_name)
        if tool is None:
            return ValidationResult(ok=False, errors=[f"unknown tool '{tool_name}'"])

        result, normalized = prepare_and_validate_arguments(
            tool,
            tool_name,
            args,
            strict=True,
            schema_registry=self._schemas,
        )
        if not result.success:
            errors = list(result.validation_errors) or ([result.error] if result.error else ["validation failed"])
            return ValidationResult(ok=False, errors=errors)

        final_args = normalized
        if isinstance(result.data, dict):
            final_args = result.data
        return ValidationResult(ok=True, normalized_args=dict(final_args))

    def record_rejection(
        self,
        tool_name: str,
        args: Dict[str, Any],
        errors: List[str],
        attempt: int,
    ) -> ValidationRejection:
        """Логирует отклонённый вызов и возвращает запись для trace."""
        rejection = ValidationRejection(
            tool_name=tool_name,
            args=dict(args),
            errors=list(errors),
            attempt=attempt,
        )
        self.rejections.append(rejection)
        logger.warning(
            "Tool validation rejected: tool=%s args=%r errors=%s attempt=%s",
            tool_name,
            args,
            errors,
            attempt,
        )
        return rejection
