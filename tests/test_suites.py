"""Unit-тесты для сбалансированных наборов (suites) и доменных метрик."""
from pathlib import Path

from benchmark.compare import load_scenarios, parse_args
from benchmark.mcp_tool_registry import graph_query_result

REPO = Path(__file__).resolve().parent.parent
SUITES_DIR = REPO / "benchmark/scenarios/suites"


def test_suites_exist():
    expected = [
        "suite_tools.yaml",
        "suite_safety.yaml",
        "suite_memory.yaml",
        "suite_graph.yaml",
        "suite_balanced.yaml",
    ]
    for filename in expected:
        path = SUITES_DIR / filename
        assert path.exists(), f"Файл сюиты {filename} отсутствует"


def test_suite_balanced_composition():
    scenarios = load_scenarios(SUITES_DIR / "suite_balanced.yaml")
    assert len(scenarios) == 40

    domains = {"suite_tools": 0, "suite_safety": 0, "suite_memory": 0, "suite_graph": 0}
    for sc in scenarios:
        tags = set(sc.get("tags") or [])
        matched = False
        for dom in domains:
            if dom in tags:
                domains[dom] += 1
                matched = True
                break
        assert matched, f"Сценарий {sc.get('id')} не принадлежит ни одному домену сюиты"

    for dom, count in domains.items():
        assert count == 10, f"Домен {dom} должен иметь ровно 10 сценариев, получено {count}"


def test_graph_queries_support():
    scenarios = load_scenarios(SUITES_DIR / "suite_graph.yaml")
    assert len(scenarios) == 15
    # Проверяем, что graph_query_result не падает и даёт непустой json
    for sc in scenarios:
        for turn in sc.get("turns") or []:
            expected_args = turn.get("expect_tool_args") or {}
            query = expected_args.get("query")
            if query:
                res = graph_query_result(query)
                assert res.startswith("{") and res.endswith("}")


def test_compare_argparse_suite():
    import sys
    from unittest.mock import patch

    with patch.object(sys, "argv", ["compare.py", "--suite", "balanced"]):
        args = parse_args()
        assert args.suite == "balanced"


def test_load_scenarios_avoids_suites_duplication():
    catalog_scenarios = load_scenarios(REPO / "benchmark/scenarios")
    # ID новых сценариев типа bal_g01 или bal_s01_t0 не должны попадать в общий каталог
    for sc in catalog_scenarios:
        assert not sc["id"].startswith("bal_"), f"Обнаружен {sc['id']} из suites в общем каталоге"
