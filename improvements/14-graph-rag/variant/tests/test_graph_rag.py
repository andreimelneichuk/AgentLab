"""Unit-тесты Graph-RAG: граф, traversal, graph_query tool."""
import json

import pytest

from agent_core import load_local_tools
from graph_rag import (
    GraphQueryEngine,
    KnowledgeGraph,
    build_demo_graph,
    create_graph_tools,
    is_empty_result,
    run_graph_query,
)


@pytest.fixture
def demo_graph() -> KnowledgeGraph:
    return build_demo_graph()


@pytest.fixture
def engine(demo_graph: KnowledgeGraph) -> GraphQueryEngine:
    return GraphQueryEngine(demo_graph)


def test_demo_graph_node_counts(demo_graph: KnowledgeGraph):
    employees = demo_graph.find_nodes("employee")
    departments = demo_graph.find_nodes("department")
    policies = demo_graph.find_nodes("policy")
    assert len(employees) == 4
    assert len(departments) == 3
    assert len(policies) == 3


def test_count_employees_in_department_with_active_policy(engine: GraphQueryEngine):
    raw = '{"query_type":"count","department":"Engineering","policy":"Remote Work"}'
    payload = engine.execute(raw)
    assert payload["status"] == "ok"
    assert payload["count"] == 1
    assert payload["results"][0]["name"] == "Alice"
    assert not is_empty_result(payload)


def test_count_employees_department_only(engine: GraphQueryEngine):
    payload = engine.execute('{"query_type":"count","department":"Engineering"}')
    assert payload["count"] == 2
    names = {r["name"] for r in payload["results"]}
    assert names == {"Alice", "Bob"}


def test_count_zero_for_nonexistent_department(engine: GraphQueryEngine):
    payload = engine.execute('{"query_type":"count","department":"Legal","policy":"Remote Work"}')
    assert payload["status"] == "empty"
    assert payload["count"] == 0
    assert is_empty_result(payload)


def test_count_zero_for_inactive_policy(engine: GraphQueryEngine):
    payload = engine.execute(
        '{"query_type":"count","department":"Sales","policy":"Legacy Benefits","policy_status":"inactive"}'
    )
    assert payload["count"] == 1
    assert payload["results"][0]["name"] == "Dave"

    payload_active = engine.execute(
        '{"query_type":"count","department":"Sales","policy":"Legacy Benefits"}'
    )
    assert payload_active["count"] == 0
    assert is_empty_result(payload_active)


def test_dsl_query_syntax(engine: GraphQueryEngine):
    payload = engine.execute('count department=HR policy="Remote Work"')
    assert payload["count"] == 1
    assert payload["results"][0]["name"] == "Carol"


def test_list_employees(engine: GraphQueryEngine):
    payload = engine.execute('{"query_type":"list","department":"HR"}')
    assert payload["count"] == 1
    assert payload["results"][0]["active_policies"] == ["Remote Work", "Health Insurance"]


def test_employee_policies(engine: GraphQueryEngine):
    payload = engine.execute('{"query_type":"employee_policies","employee":"Bob"}')
    assert payload["count"] == 1
    assert payload["results"][0]["active_policies"] == ["Health Insurance"]


def test_employee_policies_empty(engine: GraphQueryEngine):
    payload = engine.execute('{"query_type":"employee_policies","employee":"Unknown"}')
    assert is_empty_result(payload)


def test_traversal_hr_policy_chain(engine: GraphQueryEngine):
    payload = engine.execute(json.dumps({
        "query_type": "traverse",
        "start_type": "department",
        "start_name": "Engineering",
        "path": [
            {"relation": "BELONGS_TO", "node_type": "employee"},
            {"relation": "COVERED_BY", "node_type": "policy"},
        ],
    }))
    assert payload["count"] == 2
    policy_names = {r["name"] for r in payload["results"]}
    assert policy_names == {"Remote Work", "Health Insurance"}


def test_invalid_query_returns_error(engine: GraphQueryEngine):
    payload = engine.execute("not-json")
    assert payload["status"] == "error"
    assert is_empty_result(payload)


def test_graph_query_tool_invoke():
    tools = create_graph_tools()
    assert len(tools) == 1
    assert tools[0].name == "graph_query"
    raw = tools[0].invoke({"query": '{"query_type":"count","department":"Engineering"}'})
    payload = json.loads(raw)
    assert payload["count"] == 2


def test_run_graph_query_helper():
    result = run_graph_query('count department=Engineering policy="Health Insurance"')
    payload = json.loads(result)
    assert payload["count"] == 1
    assert payload["results"][0]["name"] == "Bob"


def test_load_local_tools_from_agent_core():
    tools = load_local_tools({"graph_rag": {"enabled": True}})
    assert any(t.name == "graph_query" for t in tools)

    disabled = load_local_tools({"graph_rag": {"enabled": False}})
    assert disabled == []
