"""Исполнение tool calls — поведение синхронизировано с Basic ToolExecutor.

См. ai-api-gateway/gd_ai/services/basic_assistant/src/gb_mcp/tool_executor.py
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from langchain_core.tools import BaseTool

logger = logging.getLogger("tool_executor")

# Дополнительные ограничения поверх MCP inputSchema (имя tool → фрагмент JSON Schema).
SCHEMA_AUGMENTATIONS: Dict[str, Dict[str, Any]] = {}


@dataclass
class ToolExecutionResult:
    """Унифицированный результат выполнения инструмента (как в Basic)."""

    success: bool
    data: Any = None
    error: Optional[str] = None
    tool_name: Optional[str] = None
    error_kind: Optional[str] = None
    validation_errors: List[str] = field(default_factory=list)


def extract_input_schema(tool: BaseTool) -> Dict[str, Any]:
    """JSON Schema из args_schema LangChain-инструмента (MCP inputSchema)."""
    schema = tool.args_schema
    if schema is None:
        return {"type": "object", "properties": {}}
    if isinstance(schema, dict):
        return dict(schema)
    if hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    return {"type": "object", "properties": {}}


def _deep_merge_schema(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, val in override.items():
        if key == "properties" and isinstance(val, dict):
            props = dict(out.get("properties") or {})
            for prop_name, prop_schema in val.items():
                if isinstance(prop_schema, dict) and isinstance(props.get(prop_name), dict):
                    props[prop_name] = _deep_merge_schema(props[prop_name], prop_schema)
                else:
                    props[prop_name] = dict(prop_schema) if isinstance(prop_schema, dict) else prop_schema
            out["properties"] = props
        elif isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge_schema(out[key], val)
        else:
            out[key] = val
    return out


def merge_schema_augmentation(tool_name: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    """Накладывает SCHEMA_AUGMENTATIONS поверх схемы из MCP metadata."""
    aug = SCHEMA_AUGMENTATIONS.get(tool_name)
    if not aug:
        return schema
    return _deep_merge_schema(schema, aug)


def build_schema_registry(tools: List[BaseTool]) -> Dict[str, Dict[str, Any]]:
    """Реестр JSON Schema с augmentations для strict-режима."""
    registry: Dict[str, Dict[str, Any]] = {}
    for tool in tools:
        schema = extract_input_schema(tool)
        if schema.get("type") is None:
            schema = {**schema, "type": "object"}
        schema.setdefault("properties", {})
        registry[tool.name] = merge_schema_augmentation(tool.name, schema)
    return registry


def get_pydantic_model(tool: BaseTool) -> Optional[type]:
    """Pydantic-модель из args_schema, если задана."""
    schema = tool.args_schema
    try:
        from pydantic import BaseModel

        if isinstance(schema, type) and issubclass(schema, BaseModel):
            return schema
    except Exception:
        return None
    return None


def _format_jsonschema_error(error: Any) -> str:
    path = ".".join(str(p) for p in error.absolute_path) or error.json_path.lstrip(".")
    constraint = error.validator
    if path:
        return f"поле '{path}': {error.message} (ограничение: {constraint})"
    return f"{error.message} (ограничение: {constraint})"


def _format_pydantic_error(error: Mapping[str, Any]) -> str:
    loc = ".".join(str(part) for part in error.get("loc", ()))
    msg = error.get("msg", "invalid value")
    if loc:
        return f"поле '{loc}': {msg}"
    return str(msg)


def normalize_arguments(tool_name: str, arguments: Dict[str, Any], input_schema: Dict[str, Any]) -> Dict[str, Any]:
    """Нормализация args перед валидацией (логика Basic execute_tool_command)."""
    args = dict(arguments) if isinstance(arguments, dict) else {}
    required_params = input_schema.get("required", [])

    if required_params and len(required_params) == 1:
        expected_param = required_params[0]
        if expected_param not in args:
            if "input" in args:
                args = {**args, expected_param: args["input"]}
                logger.debug("Tool '%s': mapped 'input' -> '%s'", tool_name, expected_param)
            elif "query" in args and expected_param != "query":
                args = {**args, expected_param: args["query"]}
                logger.debug("Tool '%s': mapped 'query' -> '%s'", tool_name, expected_param)

    if tool_name and "search" in tool_name.lower() and "input" in args and "query" not in args:
        args = {**args, "query": args["input"]}
        logger.debug("Tool '%s': added 'query' from 'input' for search tool", tool_name)

    return args


def validate_arguments(tool_name: str, input_schema: Dict[str, Any], arguments: Dict[str, Any]) -> ToolExecutionResult:
    """Валидирует аргументы (required + базовые типы), как Basic _validate_arguments."""
    try:
        properties = input_schema.get("properties", {})
        required = input_schema.get("required", [])

        for param_name in required:
            if param_name not in arguments:
                return ToolExecutionResult(
                    success=False,
                    error=f"Missing required parameter: {param_name}",
                    tool_name=tool_name,
                    error_kind="validation",
                    validation_errors=[f"Missing required parameter: {param_name}"],
                )

        for param_name, param_value in arguments.items():
            if param_name not in properties:
                logger.warning("Unexpected parameter '%s' for tool '%s'", param_name, tool_name)
                continue

            param_schema = properties[param_name]
            expected_type = param_schema.get("type")

            if param_value is None and param_name not in required:
                continue

            if expected_type and not _check_type(param_value, expected_type):
                msg = f"Parameter '{param_name}' has wrong type. Expected: {expected_type}"
                return ToolExecutionResult(
                    success=False,
                    error=msg,
                    tool_name=tool_name,
                    error_kind="validation",
                    validation_errors=[msg],
                )

        return ToolExecutionResult(success=True, tool_name=tool_name)

    except Exception as exc:
        logger.error("Error validating arguments for %s: %s", tool_name, exc)
        msg = f"Validation error: {exc}"
        return ToolExecutionResult(
            success=False,
            error=msg,
            tool_name=tool_name,
            error_kind="validation",
            validation_errors=[msg],
        )


def validate_arguments_strict(
    tool_name: str,
    schema: Dict[str, Any],
    arguments: Dict[str, Any],
    *,
    pydantic_model: Optional[type] = None,
) -> ToolExecutionResult:
    """Строгая jsonschema/pydantic проверка поверх Basic baseline (вариант 02)."""
    if pydantic_model is not None:
        try:
            from pydantic import ValidationError as PydanticValidationError

            parsed = pydantic_model.model_validate(arguments)
            return ToolExecutionResult(
                success=True,
                data=parsed.model_dump(),
                tool_name=tool_name,
            )
        except PydanticValidationError as exc:
            errors = [_format_pydantic_error(err) for err in exc.errors()]
            return ToolExecutionResult(
                success=False,
                error="; ".join(errors),
                tool_name=tool_name,
                error_kind="validation",
                validation_errors=errors,
            )

    try:
        import jsonschema
        from jsonschema import ValidationError
    except ImportError:
        msg = "jsonschema is required for strict validation"
        return ToolExecutionResult(
            success=False,
            error=msg,
            tool_name=tool_name,
            error_kind="validation",
            validation_errors=[msg],
        )

    try:
        jsonschema.validate(instance=arguments, schema=schema)
    except ValidationError as exc:
        errors = [_format_jsonschema_error(exc)]
        return ToolExecutionResult(
            success=False,
            error=errors[0],
            tool_name=tool_name,
            error_kind="validation",
            validation_errors=errors,
        )

    return ToolExecutionResult(success=True, tool_name=tool_name)


def prepare_and_validate_arguments(
    tool: BaseTool,
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    strict: bool = False,
    schema_registry: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[ToolExecutionResult, Dict[str, Any]]:
    """normalize → Basic validate → optional strict jsonschema/pydantic."""
    input_schema = extract_input_schema(tool)
    normalized = normalize_arguments(tool_name, arguments, input_schema)

    validation = validate_arguments(tool_name, input_schema, normalized)
    if not validation.success:
        return validation, normalized

    if not strict:
        return ToolExecutionResult(success=True, tool_name=tool_name), normalized

    if schema_registry and tool_name in schema_registry:
        strict_schema = schema_registry[tool_name]
    else:
        strict_schema = merge_schema_augmentation(tool_name, input_schema)

    strict_result = validate_arguments_strict(
        tool_name,
        strict_schema,
        normalized,
        pydantic_model=get_pydantic_model(tool),
    )
    if not strict_result.success:
        return strict_result, normalized

    if isinstance(strict_result.data, dict):
        normalized = strict_result.data
    return ToolExecutionResult(success=True, tool_name=tool_name, data=normalized), normalized


def _check_type(value: Any, expected_type: str) -> bool:
    type_mapping = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    expected_python_type = type_mapping.get(expected_type)
    if not expected_python_type:
        return True
    if isinstance(expected_python_type, tuple):
        return isinstance(value, expected_python_type)
    return isinstance(value, expected_python_type)


def _unify_result_format(result: Any, tool_name: str) -> Dict[str, Any]:
    """Унификация ответа инструмента (Basic _unify_result_format)."""
    if isinstance(result, dict) and "content" in result:
        return result

    unified: Dict[str, Any] = {"tool_name": tool_name}
    if isinstance(result, str):
        unified["content"] = result
        unified["type"] = "text"
    elif isinstance(result, dict):
        unified["content"] = result
        unified["type"] = "structured_data"
    elif isinstance(result, list):
        unified["content"] = result
        unified["type"] = "list"
    else:
        unified["content"] = str(result)
        unified["type"] = "text"
    return unified


async def execute_tool_command(
    tool: Optional[BaseTool],
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    strict: bool = False,
    schema_registry: Optional[Dict[str, Dict[str, Any]]] = None,
) -> ToolExecutionResult:
    """Выполняет один tool call с pre-validation (аналог Basic execute_tool_command)."""
    if not isinstance(arguments, dict):
        return ToolExecutionResult(
            success=False,
            error="Field 'arguments' must be a dictionary",
            tool_name=tool_name,
            error_kind="validation",
            validation_errors=["Field 'arguments' must be a dictionary"],
        )

    if not tool_name:
        return ToolExecutionResult(
            success=False,
            error="Missing required field 'tool_name'",
            error_kind="validation",
            validation_errors=["Missing required field 'tool_name'"],
        )

    if not tool:
        return ToolExecutionResult(
            success=False,
            tool_name=tool_name,
            error=f"Tool '{tool_name}' not found",
            error_kind="not_found",
        )

    validation, normalized = prepare_and_validate_arguments(
        tool,
        tool_name,
        arguments,
        strict=strict,
        schema_registry=schema_registry,
    )
    if not validation.success:
        return validation

    try:
        raw = await tool.ainvoke(normalized)
        unified = _unify_result_format(raw, tool_name)
        return ToolExecutionResult(success=True, data=unified, tool_name=tool_name)
    except Exception as exc:
        logger.error("Error executing tool '%s': %s", tool_name, exc, exc_info=True)
        return ToolExecutionResult(
            success=False,
            tool_name=tool_name,
            error=f"Execution failed: {str(exc)}",
            error_kind="infra",
        )


def tool_result_to_content(result: Dict[str, Any]) -> str:
    """Формат tool-сообщения для LLM (Basic _messages_after_tools)."""
    if result.get("success"):
        data = result.get("data")
        if isinstance(data, (dict, list)):
            return json.dumps(data, ensure_ascii=False, indent=2)
        return str(data)
    return f"Error: {result.get('error', 'Unknown error')}"


def tool_call_dedup_key(tool_call: Dict[str, Any]) -> Tuple[Any, str]:
    """Ключ дедупликации: (имя, нормализованные аргументы)."""
    name = tool_call.get("name")
    args = tool_call.get("arguments", {})
    try:
        args_repr = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        args_repr = str(args)
    return (name, args_repr)


def is_infra_error_result(result: Dict[str, Any]) -> bool:
    """Инфраструктурная ли ошибка (Basic _is_infra_error_result)."""
    kind = result.get("error_kind")
    if kind:
        return kind == "infra"
    return _is_infra_tool_error(result.get("error", ""))


def _is_infra_tool_error(error: str) -> bool:
    if not error:
        return False
    error_lower = error.lower()
    infra_patterns = [
        "timeout", "timed out", "connection refused", "connection reset",
        "connection error", "connection failed", "network", "unreachable",
        "dns", "resolve", "no route to host",
        "502", "503", "504", "bad gateway", "service unavailable", "gateway timeout",
        "500", "internal server error",
        "execution failed:", "service not available", "server error",
        "unavailable", "cannot connect", "failed to connect",
        "connection closed", "broken pipe", "eof", "reset by peer",
        "mcp error", "tool service unavailable", "tool execution timeout",
    ]
    return any(pattern in error_lower for pattern in infra_patterns)
