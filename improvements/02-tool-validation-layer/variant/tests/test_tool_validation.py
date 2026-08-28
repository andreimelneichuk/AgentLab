"""Unit-тесты валидационного слоя tool call."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from agent_core import AgentResources, BasicLoopSession
from tool_validator import (
    SCHEMA_AUGMENTATIONS,
    ToolValidator,
    extract_json_schema,
    format_validation_errors,
    merge_schema_augmentation,
    validation_limits,
    validation_stop_message,
)


def _make_tool(
    name: str,
    *,
    args_schema: Dict[str, Any] | type[BaseModel] | None = None,
    handler=None,
):
    if handler is None:
        handler = lambda **kwargs: f"ok:{kwargs}"  # noqa: E731
    return StructuredTool.from_function(
        func=handler,
        name=name,
        description=f"Tool {name}",
        args_schema=args_schema,
    )


PYTHON_DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
    },
    "required": ["topic"],
}


@pytest.fixture
def python_doc_tool():
    return _make_tool("python_doc_lookup", args_schema=PYTHON_DOC_SCHEMA)


@pytest.fixture
def validator(python_doc_tool):
    return ToolValidator([python_doc_tool], max_retries=3)


def test_extract_json_schema_from_dict(python_doc_tool):
    schema = extract_json_schema(python_doc_tool)
    assert schema["type"] == "object"
    assert "topic" in schema["properties"]


def test_validate_valid_args_passes(validator):
    result = validator.validate("python_doc_lookup", {"topic": "asyncio"})
    assert result.ok is True
    assert result.normalized_args == {"topic": "asyncio"}
    assert result.errors == []


def test_validate_missing_required_field(validator):
    result = validator.validate("python_doc_lookup", {})
    assert result.ok is False
    assert result.errors
    assert any("topic" in err.lower() for err in result.errors)


def test_validate_wrong_type(validator):
    result = validator.validate("python_doc_lookup", {"topic": 42})
    assert result.ok is False
    assert result.errors


def test_validate_unknown_tool(validator):
    result = validator.validate("missing_tool", {"x": 1})
    assert result.ok is False
    assert any("unknown tool" in err for err in result.errors)


def test_format_validation_errors_contains_tool_name():
    msg = format_validation_errors("python_doc_lookup", ["поле 'topic': обязательное"])
    assert "python_doc_lookup" in msg
    assert "topic" in msg


def test_record_rejection_appends_trace(validator):
    validator.record_rejection("python_doc_lookup", {"topic": 1}, ["bad type"], attempt=1)
    assert len(validator.rejections) == 1
    entry = validator.rejections[0].to_dict()
    assert entry["tool_name"] == "python_doc_lookup"
    assert entry["attempt"] == 1
    assert entry["errors"]


def test_schema_augmentation_applies_constraints():
    SCHEMA_AUGMENTATIONS["book_hotel"] = {
        "properties": {
            "guests": {"type": "integer", "maximum": 10},
        },
        "required": ["guests"],
    }
    try:
        tool = _make_tool(
            "book_hotel",
            args_schema={
                "type": "object",
                "properties": {"guests": {"type": "integer"}},
            },
        )
        tv = ToolValidator([tool])
        ok = tv.validate("book_hotel", {"guests": 5})
        bad = tv.validate("book_hotel", {"guests": 15})
        assert ok.ok is True
        assert bad.ok is False
    finally:
        SCHEMA_AUGMENTATIONS.pop("book_hotel", None)


def test_merge_schema_augmentation_deep_merges_properties():
    base = {"type": "object", "properties": {"guests": {"type": "integer"}}}
    SCHEMA_AUGMENTATIONS["tmp_tool"] = {
        "properties": {"guests": {"maximum": 10}},
    }
    try:
        merged = merge_schema_augmentation("tmp_tool", base)
        assert merged["properties"]["guests"]["maximum"] == 10
        assert merged["properties"]["guests"]["type"] == "integer"
    finally:
        SCHEMA_AUGMENTATIONS.pop("tmp_tool", None)


class TranslateArgs(BaseModel):
    text: str
    target_lang: str = Field(pattern=r"^[a-z]{2}$")


def test_validate_pydantic_model_schema():
    tool = _make_tool("translate_text", args_schema=TranslateArgs)
    tv = ToolValidator([tool])
    ok = tv.validate("translate_text", {"text": "hello", "target_lang": "en"})
    bad = tv.validate("translate_text", {"text": "hello", "target_lang": "english"})
    assert ok.ok is True
    assert bad.ok is False


def test_validation_limits_from_config():
    cfg = {"tools": {"validation": {"max_retries": 5}}}
    assert validation_limits(cfg) == 5
    assert validation_limits({}) == 3


def test_validation_stop_message():
    msg = validation_stop_message(3)
    assert "3" in msg
    assert "остановлена" in msg.lower()


def test_execute_tools_blocks_invalid_args(python_doc_tool):
    python_doc_tool.coroutine = AsyncMock(return_value="should-not-run")
    resources = MagicMock(spec=AgentResources)
    resources.tool_map = {"python_doc_lookup": python_doc_tool}
    resources.tool_validator = ToolValidator([python_doc_tool], max_retries=3)
    resources.tool_exec_fail_retries = 3

    session = BasicLoopSession(resources)

    async def _run():
        return await session._execute_tools(
            [{"id": "c1", "name": "python_doc_lookup", "arguments": {"topic": 123}}],
            validation_attempt=1,
        )

    results, had_failure = asyncio.run(_run())

    assert had_failure is True
    assert len(results) == 1
    assert "Validation failed" in results[0]["content"]
    python_doc_tool.coroutine.assert_not_awaited()


def test_execute_tools_invokes_on_valid_args(python_doc_tool):
    python_doc_tool.coroutine = AsyncMock(return_value="REF=PYDOC-778")
    resources = MagicMock(spec=AgentResources)
    resources.tool_map = {"python_doc_lookup": python_doc_tool}
    resources.tool_validator = ToolValidator([python_doc_tool], max_retries=3)
    resources.tool_exec_fail_retries = 3

    session = BasicLoopSession(resources)

    async def _run():
        return await session._execute_tools(
            [{"id": "c1", "name": "python_doc_lookup", "arguments": {"topic": "typing"}}],
            validation_attempt=1,
        )

    results, had_failure = asyncio.run(_run())

    assert had_failure is False
    assert "REF=PYDOC-778" in results[0]["content"]


def test_validation_retry_counter_stops_after_max(python_doc_tool):
    """После max_retries невалидных раундов run_turn возвращает STOP."""
    resources = MagicMock(spec=AgentResources)
    resources.tool_map = {"python_doc_lookup": python_doc_tool}
    resources.tool_validator = ToolValidator([python_doc_tool], max_retries=3)
    resources.openai_tools = [{"name": "python_doc_lookup", "description": "", "parameters": PYTHON_DOC_SCHEMA}]
    resources.run_limit = 10
    resources.tool_exec_fail_retries = 3
    resources.system_prompt = ""
    resources.config = {"llm_defaults": {"attempts_per_model": 1}, "llm": {}}

    bad_tool_call = {
        "id": "call_bad",
        "name": "python_doc_lookup",
        "args": {},
    }
    llm_response = MagicMock()
    llm_response.content = ""
    llm_response.tool_calls = [bad_tool_call]
    llm_response.response_metadata = {"token_usage": {}}

    llm = MagicMock()
    llm.bind_tools.return_value = llm
    llm.ainvoke = AsyncMock(return_value=llm_response)

    session = BasicLoopSession(resources)
    session._messages = []

    call_count = {"n": 0}

    async def fake_build_llm(*_args, **_kwargs):
        call_count["n"] += 1
        return llm

    import agent_core as agent_core_mod

    original = agent_core_mod.build_llm
    agent_core_mod.build_llm = fake_build_llm

    async def _run():
        return await session.run_turn("lookup python topic")

    try:
        result = asyncio.run(_run())
    finally:
        agent_core_mod.build_llm = original

    assert result.stopped_reason == "tool_validation_exhausted"
    assert "остановлена" in result.answer.lower()
    assert len(result.validation_rejections) == 3
    assert call_count["n"] == 1
    assert llm.ainvoke.await_count == 3


def test_schema_registry_contains_loaded_tools():
    tools: List[StructuredTool] = [
        _make_tool("inventory_lookup", args_schema={"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]}),
        _make_tool("employee_lookup", args_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
    ]
    tv = ToolValidator(tools)
    registry = tv.schema_registry
    assert "inventory_lookup" in registry
    assert "employee_lookup" in registry
    assert registry["inventory_lookup"]["required"] == ["sku"]


def test_unknown_tool_not_counted_as_validation_retry(python_doc_tool):
    """unknown tool → error_kind=not_found, не validation."""
    resources = MagicMock(spec=AgentResources)
    resources.tool_map = {"python_doc_lookup": python_doc_tool}
    resources.tool_validator = ToolValidator([python_doc_tool], max_retries=3)

    session = BasicLoopSession(resources)

    async def _run():
        return await session._execute_single_tool(
            {"id": "c1", "name": "missing_tool", "arguments": {"x": 1}},
            validation_attempt=1,
        )

    result = asyncio.run(_run())
    assert result["success"] is False
    assert result["error_kind"] == "not_found"
    assert len(resources.tool_validator.rejections) == 0
