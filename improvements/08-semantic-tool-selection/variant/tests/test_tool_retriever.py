"""Unit-тесты семантического retriever инструментов."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tool_retriever import (
    ToolDocument,
    ToolSelectionConfig,
    TfidfToolRetriever,
    effective_deny_set,
    filter_openai_tools,
    is_decoy_tool,
    select_tool_names,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
REGISTRY_PATH = REPO_ROOT / "benchmark" / "mcp_tool_registry.py"


def _load_benchmark_tool_corpus() -> list[ToolDocument]:
    """Извлечь name + docstring из benchmark/mcp_tool_registry.py через AST."""
    source = REGISTRY_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    documents: list[ToolDocument] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        doc = ast.get_docstring(node) or ""
        if not doc or node.name.startswith("_"):
            continue
        # Пропускаем служебные функции register_*, record, reset_stats
        if node.name in {"record", "reset_stats", "_hash_marker"}:
            continue
        if node.name.startswith("register_"):
            continue
        documents.append(ToolDocument(name=node.name, description=doc.strip()))

    documents.sort(key=lambda d: d.name)
    return documents


@pytest.fixture(scope="module")
def benchmark_corpus() -> list[ToolDocument]:
    assert REGISTRY_PATH.exists(), f"Registry not found: {REGISTRY_PATH}"
    corpus = _load_benchmark_tool_corpus()
    assert len(corpus) >= 20, f"Expected ~25 tools, got {len(corpus)}"
    return corpus


@pytest.fixture(scope="module")
def retriever(benchmark_corpus: list[ToolDocument]) -> TfidfToolRetriever:
    return TfidfToolRetriever(benchmark_corpus)


@pytest.fixture(scope="module")
def all_tool_names(benchmark_corpus: list[ToolDocument]) -> list[str]:
    return [d.name for d in benchmark_corpus]


def test_corpus_includes_decoys_and_real_tools(benchmark_corpus: list[ToolDocument]):
    names = {d.name for d in benchmark_corpus}
    assert "python_doc_lookup" in names
    assert "decoy_python_lookup" in names
    assert "get_policy_fact" in names
    assert "decoy_policy_fact" in names
    assert sum(1 for n in names if n.startswith("decoy_")) >= 5


def test_is_decoy_tool():
    assert is_decoy_tool("decoy_python_lookup")
    assert not is_decoy_tool("python_doc_lookup")


def test_python_doc_query_prefers_real_tool(
    retriever: TfidfToolRetriever,
    all_tool_names: list[str],
):
    selected = select_tool_names(
        retriever,
        "Найди в Python-доке тему decorators",
        all_tool_names=all_tool_names,
        config=ToolSelectionConfig(top_k=5),
    )
    assert "python_doc_lookup" in selected
    assert "decoy_python_lookup" not in selected


def test_policy_query_prefers_real_tool(retriever: TfidfToolRetriever):
    results = dict(retriever.search("Узнай секрет политики компании violet", k=10))
    assert "get_policy_fact" in results
    assert results["get_policy_fact"] > results.get("decoy_policy_fact", 0.0)


def test_inventory_query_prefers_real_tool(retriever: TfidfToolRetriever):
    results = dict(retriever.search("Остаток SKU на складе WH-01", k=10))
    assert "inventory_lookup" in results
    assert results["inventory_lookup"] > results.get("decoy_inventory_lookup", 0.0)


def test_hr_employee_query(retriever: TfidfToolRetriever):
    results = dict(retriever.search("Профиль сотрудника Ivan Petrov HR", k=10))
    assert "employee_lookup" in results
    assert results["employee_lookup"] > results.get("decoy_employee_search", 0.0)


def test_crm_customer_query(retriever: TfidfToolRetriever):
    results = dict(retriever.search("Карточка клиента customer_id CUST-771", k=10))
    assert "customer_get" in results
    assert results["customer_get"] > results.get("decoy_customer_lookup", 0.0)


def test_decoys_never_selected_via_select_tool_names(
    retriever: TfidfToolRetriever,
    all_tool_names: list[str],
):
    config = ToolSelectionConfig(top_k=5, mandatory=[], deny=[])
    for query in (
        "Python documentation topic",
        "policy fact violet",
        "inventory sku warehouse",
        "employee profile HR",
        "customer card CRM",
    ):
        selected = select_tool_names(
            retriever,
            query,
            all_tool_names=all_tool_names,
            config=config,
        )
        assert not any(is_decoy_tool(n) for n in selected), f"decoy in {selected} for {query!r}"


def test_mandatory_tools_always_included(
    retriever: TfidfToolRetriever,
    all_tool_names: list[str],
):
    config = ToolSelectionConfig(top_k=3, mandatory=["get_policy_fact", "benchmark_probe"], deny=[])
    selected = select_tool_names(
        retriever,
        "погода в Moscow weather",
        all_tool_names=all_tool_names,
        config=config,
    )
    assert "get_policy_fact" in selected
    assert "benchmark_probe" in selected
    assert not any(is_decoy_tool(n) for n in selected)


def test_deny_list_excludes_tools(
    retriever: TfidfToolRetriever,
    all_tool_names: list[str],
):
    config = ToolSelectionConfig(top_k=5, mandatory=[], deny=["weather_city"])
    selected = select_tool_names(
        retriever,
        "погода в городе Moscow weather temp",
        all_tool_names=all_tool_names,
        config=config,
    )
    assert "weather_city" not in selected


def test_effective_deny_includes_all_decoys(all_tool_names: list[str]):
    config = ToolSelectionConfig(deny=["weather_city"])
    denied = effective_deny_set(config, all_tool_names)
    assert "weather_city" in denied
    assert "decoy_python_lookup" in denied
    assert "python_doc_lookup" not in denied


def test_filter_openai_tools_preserves_order():
    tools = [
        {"name": "a", "description": "A", "parameters": {}},
        {"name": "b", "description": "B", "parameters": {}},
        {"name": "c", "description": "C", "parameters": {}},
    ]
    filtered = filter_openai_tools(tools, ["c", "a"])
    assert [t["name"] for t in filtered] == ["c", "a"]


def test_top_k_limits_results(retriever: TfidfToolRetriever):
    results = retriever.search("invoice weather calc employee customer", k=5)
    assert len(results) == 5
    assert all(score >= 0 for _, score in results)


def test_search_returns_sorted_by_score(retriever: TfidfToolRetriever):
    results = retriever.search("Python doc lookup topic", k=5)
    scores = [s for _, s in results]
    assert scores == sorted(scores, reverse=True)


def test_disabled_selection_returns_all(all_tool_names: list[str], retriever: TfidfToolRetriever):
    config = ToolSelectionConfig(enabled=False, top_k=2)
    selected = select_tool_names(
        retriever,
        "anything",
        all_tool_names=all_tool_names,
        config=config,
    )
    assert selected == all_tool_names
