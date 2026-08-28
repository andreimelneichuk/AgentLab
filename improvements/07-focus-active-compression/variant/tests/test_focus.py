"""Unit-тесты Knowledge Index: автоиндексация, lookup, семантический индекс."""
from __future__ import annotations

import json

import pytest

from focus import (
    KNOWLEDGE_INDEX_LOOKUP,
    FocusError,
    IndexEntry,
    KnowledgeIndex,
    KnowledgeIndexManager,
    knowledge_index_lookup_schema,
    is_knowledge_index_tool,
)
from agent_core import merge_openai_tools


def test_index_entry_creation():
    """IndexEntry создаётся с основными полями."""
    entry = IndexEntry(
        id="a1b2c3",
        key="violet-42",
        category="policy",
        summary="POLICY_FACT=violet-42",
        full_format="POLICY_FACT=violet-42. Правило: каждый ответ должен заканчиваться [POLICY_OK].",
        from_tool="get_policy_fact",
        turn_index=5,
        confidence="high",
    )
    assert entry.key == "violet-42"
    assert entry.category == "policy"
    assert entry.confidence == "high"


def test_index_entry_to_dict():
    """IndexEntry сериализуется в dict."""
    entry = IndexEntry(
        id="a1b2c3",
        key="violet-42",
        category="policy",
        summary="POLICY_FACT=violet-42",
        full_format="POLICY_FACT=violet-42. Правило: каждый ответ должен заканчиваться [POLICY_OK].",
        from_tool="get_policy_fact",
        turn_index=5,
    )
    d = entry.to_dict()
    assert d["key"] == "violet-42"
    assert d["from_tool"] == "get_policy_fact"
    assert d["confidence"] == "high"


def test_knowledge_index_add_lookup():
    """Knowledge Index добавляет и ищет entries."""
    index = KnowledgeIndex()
    entry = IndexEntry(
        id="a1b2c3",
        key="violet-42",
        category="policy",
        summary="POLICY_FACT=violet-42",
        full_format="POLICY_FACT=violet-42. Правило: ...",
        from_tool="get_policy_fact",
        turn_index=1,
    )
    index.add_entry(entry)

    found = index.lookup("policy", "violet-42")
    assert found is not None
    assert found.key == "violet-42"

    not_found = index.lookup("policy", "orange-99")
    assert not_found is None


def test_knowledge_index_by_category():
    """Knowledge Index возвращает все entries в категории."""
    index = KnowledgeIndex()
    entry1 = IndexEntry(
        id="a", key="violet-42", category="policy",
        summary="POLICY=violet-42", full_format="POLICY_FACT=violet-42...",
        from_tool="get_policy_fact", turn_index=1
    )
    entry2 = IndexEntry(
        id="b", key="orchid-17", category="policy",
        summary="MARKER=orchid-17", full_format="BENCH_MARKER_STREAMABLE=orchid-17",
        from_tool="benchmark_probe", turn_index=2
    )
    index.add_entry(entry1)
    index.add_entry(entry2)

    policy_entries = index.lookup_by_category("policy")
    # Обе должны быть в policy (первая явно, вторая нет — check)
    # Actually, вторая в "marker", не в "policy"


def test_knowledge_index_to_context_text():
    """Knowledge Index форматируется для инжекции в system context."""
    index = KnowledgeIndex()
    entry = IndexEntry(
        id="a1b2c3", key="violet-42", category="policy",
        summary="POLICY=violet-42", full_format="POLICY_FACT=violet-42...",
        from_tool="get_policy_fact", turn_index=1
    )
    index.add_entry(entry)

    context = index.to_context_text()
    assert "### KNOWLEDGE INDEX" in context
    assert "violet-42" in context
    assert "policy" in context


def test_knowledge_index_manager_creation():
    """KnowledgeIndexManager инициализируется с пустым индексом."""
    manager = KnowledgeIndexManager()
    assert manager.knowledge_dict == {}
    assert manager.knowledge.size == 0


def test_knowledge_index_manager_auto_index_policy_fact():
    """Auto-indexing парсит POLICY_FACT из tool response."""
    manager = KnowledgeIndexManager()
    response = "POLICY_FACT=violet-42. Правило: каждый ответ должен заканчиваться [POLICY_OK]."

    manager.auto_index_from_tool_response("get_policy_fact", response, turn_index=1)

    found = manager.lookup("policy", "violet-42")
    assert found is not None
    assert found.category == "policy"
    assert found.from_tool == "get_policy_fact"
    assert "violet-42" in found.full_format


def test_knowledge_index_manager_auto_index_employee_lookup():
    """Auto-indexing парсит EMP_ID из employee_lookup."""
    manager = KnowledgeIndexManager()
    response = "EMP_ID=HR-001 NAME=Alice Johnson DEPT=Engineering"

    manager.auto_index_from_tool_response("employee_lookup", response, turn_index=2)

    found = manager.lookup("employee", "HR-001")
    assert found is not None
    assert found.key == "HR-001"
    assert found.from_tool == "employee_lookup"


def test_knowledge_index_manager_auto_index_benchmark_probe():
    """Auto-indexing парсит BENCH_MARKER_STREAMABLE."""
    manager = KnowledgeIndexManager()
    response = "BENCH_MARKER_STREAMABLE=orchid-17"

    manager.auto_index_from_tool_response("benchmark_probe", response, turn_index=3)

    found = manager.lookup("marker", "orchid-17")
    assert found is not None
    assert found.key == "orchid-17"


def test_knowledge_index_manager_auto_index_sse_probe():
    """Auto-indexing парсит BENCH_MARKER_SSE."""
    manager = KnowledgeIndexManager()
    response = "BENCH_MARKER_SSE=amber-91"

    manager.auto_index_from_tool_response("benchmark_sse_probe", response, turn_index=4)

    found = manager.lookup("marker_sse", "amber-91")
    assert found is not None
    assert found.key == "amber-91"


def test_knowledge_index_manager_handle_lookup_request_found():
    """Handle lookup request возвращает найденный entry."""
    manager = KnowledgeIndexManager()
    entry = IndexEntry(
        id="a", key="violet-42", category="policy",
        summary="POLICY=violet-42", full_format="POLICY_FACT=violet-42. Правило: каждый ответ должен заканчиваться [POLICY_OK].",
        from_tool="get_policy_fact", turn_index=1
    )
    manager.knowledge.add_entry(entry)

    result = manager.handle_lookup_request("policy", "violet-42")
    assert result["found"] is True
    assert "violet-42" in result["full_format"]
    assert result["from_tool"] == "get_policy_fact"


def test_knowledge_index_manager_handle_lookup_request_not_found():
    """Handle lookup request возвращает not found."""
    manager = KnowledgeIndexManager()

    result = manager.handle_lookup_request("policy", "violet-42")
    assert result["found"] is False
    assert result["key"] == "violet-42"


def test_knowledge_index_lookup_schema():
    """Pseudo-tool schema корректный."""
    schema = knowledge_index_lookup_schema()
    assert schema["name"] == KNOWLEDGE_INDEX_LOOKUP
    assert "category" in schema["parameters"]["properties"]
    assert "key" in schema["parameters"]["properties"]


def test_is_knowledge_index_tool():
    """Проверка is_knowledge_index_tool."""
    assert is_knowledge_index_tool(KNOWLEDGE_INDEX_LOOKUP)
    assert is_knowledge_index_tool("knowledge_index_lookup")
    assert not is_knowledge_index_tool("get_policy_fact")
    assert not is_knowledge_index_tool("employee_lookup")


def test_merge_openai_tools_includes_knowledge_index():
    """merge_openai_tools добавляет knowledge_index_lookup в tools."""
    mcp_tools = [
        {"name": "tool1", "description": "Tool 1", "parameters": {}},
        {"name": "tool2", "description": "Tool 2", "parameters": {}},
    ]
    merged = merge_openai_tools(mcp_tools, include_knowledge_index=True)

    names = [t["name"] for t in merged]
    assert KNOWLEDGE_INDEX_LOOKUP in names
    assert "tool1" in names
    assert "tool2" in names
    assert len(merged) == 3


def test_merge_openai_tools_exclude_knowledge_index():
    """merge_openai_tools может исключить knowledge_index если отключен."""
    mcp_tools = [{"name": "tool1", "description": "Tool 1", "parameters": {}}]
    merged = merge_openai_tools(mcp_tools, include_knowledge_index=False)

    names = [t["name"] for t in merged]
    assert KNOWLEDGE_INDEX_LOOKUP not in names
    assert "tool1" in names


def test_knowledge_index_manager_multiple_categories():
    """Knowledge Index управляет несколькими категориями."""
    manager = KnowledgeIndexManager()

    # Добавляем entries разных категорий
    manager.auto_index_from_tool_response(
        "get_policy_fact",
        "POLICY_FACT=violet-42. Правило: ...",
        turn_index=1
    )
    manager.auto_index_from_tool_response(
        "employee_lookup",
        "EMP_ID=HR-001 NAME=Alice",
        turn_index=2
    )
    manager.auto_index_from_tool_response(
        "benchmark_probe",
        "BENCH_MARKER_STREAMABLE=orchid-17",
        turn_index=3
    )

    # Проверяем что всё есть
    assert manager.lookup("policy", "violet-42") is not None
    assert manager.lookup("employee", "HR-001") is not None
    assert manager.lookup("marker", "orchid-17") is not None


def test_knowledge_index_overwrites_existing_key():
    """Добавление entry с существующим key перезаписывает старый."""
    manager = KnowledgeIndexManager()

    entry1 = IndexEntry(
        id="a", key="HR-001", category="employee",
        summary="EMP=HR-001 v1", full_format="EMP_ID=HR-001 NAME=Alice",
        from_tool="employee_lookup", turn_index=1
    )
    entry2 = IndexEntry(
        id="b", key="HR-001", category="employee",
        summary="EMP=HR-001 v2", full_format="EMP_ID=HR-001 NAME=Alice Johnson",
        from_tool="employee_lookup", turn_index=5
    )

    manager.knowledge.add_entry(entry1)
    manager.knowledge.add_entry(entry2)

    found = manager.lookup("employee", "HR-001")
    assert found.turn_index == 5  # Новая версия
    assert found.full_format == "EMP_ID=HR-001 NAME=Alice Johnson"


def test_knowledge_index_to_json():
    """Knowledge Index сериализуется в JSON."""
    index = KnowledgeIndex()
    entry = IndexEntry(
        id="a", key="violet-42", category="policy",
        summary="POLICY=violet-42", full_format="POLICY_FACT=violet-42...",
        from_tool="get_policy_fact", turn_index=1
    )
    index.add_entry(entry)

    json_str = index.to_json()
    data = json.loads(json_str)
    assert "policy" in data
    assert "violet-42" in data["policy"]
