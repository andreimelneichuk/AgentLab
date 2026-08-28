"""Unit-тесты реалистичности mock-инструментов benchmark/mcp_tool_registry."""
import json

from benchmark.mcp_tool_registry import (
    employee_lookup_result,
    graph_query_result,
    inventory_lookup_result,
    translate_text_result,
)


def test_employee_lookup_normal_name():
    result = employee_lookup_result("Anna Kozlova")
    assert result.startswith("EMP_ID=HR-")
    assert "NAME=Anna Kozlova" in result
    assert "NOT_FOUND" not in result


def test_employee_lookup_noname_returns_not_found():
    result = employee_lookup_result("XYZ NoName")
    assert result == "NOT_FOUND name='XYZ NoName'"
    assert "EMP_ID" not in result


def test_employee_lookup_nonexist_marker():
    result = employee_lookup_result("John NONEXIST Smith")
    assert result.startswith("NOT_FOUND")
    assert "EMP_ID" not in result


def test_inventory_lookup_normal_sku():
    result = inventory_lookup_result("SKU-1010")
    data = json.loads(result)
    assert data["sku"] == "SKU-1010"
    assert data["qty"] > 0
    assert data["warehouse"] == "WH-01"
    assert "status" not in data


def test_inventory_lookup_sku_0000_not_found():
    result = inventory_lookup_result("SKU-0000")
    data = json.loads(result)
    assert data == {"sku": "SKU-0000", "qty": 0, "status": "NOT_FOUND"}


def test_inventory_lookup_notfound_pattern():
    result = inventory_lookup_result("SKU-NOTFOUND-42")
    data = json.loads(result)
    assert data["qty"] == 0
    assert data["status"] == "NOT_FOUND"


def test_graph_query_legal_department_empty():
    result = graph_query_result("count department=Legal policy=Remote Work")
    data = json.loads(result)
    assert data["count"] == 0
    assert data["results"] == []
    assert data["status"] == "empty"


def test_graph_query_engineering_remote_has_results():
    result = graph_query_result("engineering remote work count")
    data = json.loads(result)
    assert data["count"] == 1
    assert data["results"][0]["name"] == "Alice"


def test_translate_text_keeps_benchmark_marker():
    result = translate_text_result("hello world", "fi")
    assert result.startswith("TRANSLATED[fi]=dlrow olleh")
    assert "benchmark placeholder" in result
    assert "reversed source text" in result
