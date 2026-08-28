"""Unit-тесты tool_executor (паритет с Basic ToolExecutor)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from tool_executor import (
    SCHEMA_AUGMENTATIONS,
    execute_tool_command,
    is_infra_error_result,
    merge_schema_augmentation,
    normalize_arguments,
    tool_call_dedup_key,
    tool_result_to_content,
    validate_arguments,
    validate_arguments_strict,
)


def _schema(required=None, properties=None):
    return {
        "type": "object",
        "properties": properties or {"topic": {"type": "string"}},
        "required": required or ["topic"],
    }


def test_validate_missing_required():
    result = validate_arguments("python_doc_lookup", _schema(), {})
    assert not result.success
    assert result.error_kind == "validation"
    assert "Missing required parameter" in (result.error or "")


def test_validate_wrong_type():
    result = validate_arguments("python_doc_lookup", _schema(), {"topic": 42})
    assert not result.success
    assert "wrong type" in (result.error or "")


def test_normalize_input_to_single_required():
    schema = _schema(required=["query"], properties={"query": {"type": "string"}})
    args = normalize_arguments("knowledge_base_search", {"input": "hello"}, schema)
    assert args["query"] == "hello"


def test_normalize_search_input_to_query():
    schema = _schema(required=["query"], properties={"query": {"type": "string"}})
    args = normalize_arguments("empty_search", {"input": "x"}, schema)
    assert args["query"] == "x"


def test_execute_unknown_tool():
    result = asyncio.run(execute_tool_command(None, "missing_tool", {"x": 1}))
    assert not result.success
    assert result.error_kind == "not_found"
    assert "not found" in (result.error or "")


def test_execute_validation_blocks_mcp():
    tool = MagicMock()
    tool.args_schema = _schema()
    tool.ainvoke = AsyncMock()

    async def _run():
        return await execute_tool_command(tool, "python_doc_lookup", {})

    result = asyncio.run(_run())
    assert not result.success
    assert result.error_kind == "validation"
    tool.ainvoke.assert_not_awaited()


def test_execute_success_unified_format():
    tool = MagicMock()
    tool.args_schema = _schema()
    tool.ainvoke = AsyncMock(return_value="REF=PYDOC-778")

    async def _run():
        return await execute_tool_command(tool, "python_doc_lookup", {"topic": "asyncio"})

    result = asyncio.run(_run())
    assert result.success
    assert result.data["content"] == "REF=PYDOC-778"
    tool.ainvoke.assert_awaited_once_with({"topic": "asyncio"})


def test_tool_result_to_content_error_format():
    text = tool_result_to_content({"success": False, "error": "Missing required parameter: query"})
    assert text == "Error: Missing required parameter: query"


def test_is_infra_vs_validation():
    assert not is_infra_error_result({"error_kind": "validation", "error": "x"})
    assert is_infra_error_result({"error_kind": "infra", "error": "timeout"})
    assert is_infra_error_result({"error": "Execution failed: connection reset"})


def test_dedup_key_stable():
    a = tool_call_dedup_key({"name": "kb", "arguments": {"q": "1"}})
    b = tool_call_dedup_key({"name": "kb", "arguments": {"q": "1"}})
    c = tool_call_dedup_key({"name": "kb", "arguments": {"q": "2"}})
    assert a == b
    assert a != c


def test_validate_arguments_strict_maximum():
    schema = {
        "type": "object",
        "properties": {"guests": {"type": "integer", "maximum": 10}},
        "required": ["guests"],
    }
    ok = validate_arguments_strict("book_hotel", schema, {"guests": 5})
    bad = validate_arguments_strict("book_hotel", schema, {"guests": 15})
    assert ok.success
    assert not bad.success
    assert bad.error_kind == "validation"


def test_merge_schema_augmentation_applies_constraints():
    SCHEMA_AUGMENTATIONS["tmp_tool"] = {
        "properties": {"guests": {"maximum": 10}},
    }
    try:
        base = {"type": "object", "properties": {"guests": {"type": "integer"}}}
        merged = merge_schema_augmentation("tmp_tool", base)
        assert merged["properties"]["guests"]["maximum"] == 10
    finally:
        SCHEMA_AUGMENTATIONS.pop("tmp_tool", None)


def test_execute_strict_blocks_invalid_args():
    tool = MagicMock()
    tool.args_schema = _schema()
    tool.ainvoke = AsyncMock()

    async def _run():
        return await execute_tool_command(tool, "python_doc_lookup", {"topic": 42}, strict=True)

    result = asyncio.run(_run())
    assert not result.success
    assert result.error_kind == "validation"
    tool.ainvoke.assert_not_awaited()


def test_execute_non_strict_uses_basic_only():
    tool = MagicMock()
    tool.args_schema = _schema()
    tool.ainvoke = AsyncMock(return_value="ok")

    async def _run():
        return await execute_tool_command(tool, "python_doc_lookup", {"topic": "asyncio"}, strict=False)

    result = asyncio.run(_run())
    assert result.success
    tool.ainvoke.assert_awaited_once()
