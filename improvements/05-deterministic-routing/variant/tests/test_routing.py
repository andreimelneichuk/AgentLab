"""Unit- и регрессионные тесты детерминированного router (v2).

v1 тестировался только на ключевых фразах, которые сам же и определял
(`s08_multiserver`), поэтому 37/37 зелёных юнит-тестов не поймали
реального провала на бенчмарке (SR 26% → 14%, TSA 95% → 64%). Основной
тест в этом файле (`test_router_covers_full_catalog`) намеренно устроен
иначе: он гоняет router по ВСЕМ ~265 ходам с `expect_tool_called` из
`benchmark/scenarios/catalog/*.yaml` и проверяет, что ожидаемый tool
физически достижим (не спрятан фильтром). Это единственная метрика,
которая напрямую предсказывает TSA/SR на реальном бенчмарке без
необходимости гонять LLM.
"""
from __future__ import annotations

import glob
from pathlib import Path

import pytest
import yaml

from routing import (
    CORE_TOOLS,
    CRM_TOOLS,
    DECOY_TOOLS,
    HR_TOOLS,
    POLICY_TOOLS,
    SSE_TOOLS,
    Domain,
    RouterState,
    filter_tools_by_route,
    is_tool_allowed,
    route_turn,
)

CATALOG_DIR = Path(__file__).resolve().parents[4] / "benchmark" / "scenarios" / "catalog"


class _FakeTool:
    def __init__(self, name: str):
        self.name = name


def _load_catalog_scenarios():
    scenarios = []
    for path in sorted(glob.glob(str(CATALOG_DIR / "*.yaml"))):
        data = yaml.safe_load(open(path, encoding="utf-8")) or {}
        scenarios.extend(data.get("scenarios", []))
    return scenarios


_CATALOG_SCENARIOS = _load_catalog_scenarios()


@pytest.mark.skipif(not _CATALOG_SCENARIOS, reason="benchmark/scenarios/catalog не найден")
def test_router_covers_full_catalog():
    """Router не должен физически прятать tool, который ожидает сценарий.

    Реплеит каждый сценарий по его ходам (RouterState на весь сценарий,
    как в agent_core.BasicLoopSession), проверяет, что для каждого
    хода с expect_tool_called этот tool входит в allowed_tools на данном
    ходу. Это ground-truth тест — данные берутся из реального каталога,
    а не из фраз, придуманных под сам router.
    """
    total = 0
    misses = []
    for scenario in _CATALOG_SCENARIOS:
        state = RouterState()
        for idx, turn in enumerate(scenario.get("turns", [])):
            route = route_turn(state, turn["user"])
            expected = turn.get("expect_tool_called")
            if not expected:
                continue
            total += 1
            if expected not in route.allowed_tools:
                misses.append((scenario["id"], idx, expected, turn["user"][:80]))

    assert total > 0, "каталог сценариев пуст — проверьте CATALOG_DIR"
    assert not misses, (
        f"{len(misses)}/{total} ходов требуют tool, скрытый router'ом: {misses[:10]}"
    )


@pytest.mark.skipif(not _CATALOG_SCENARIOS, reason="benchmark/scenarios/catalog не найден")
def test_router_never_exposes_decoys_on_full_catalog():
    """Ни на одном ходу каталога decoy-инструмент не должен быть allowed."""
    for scenario in _CATALOG_SCENARIOS:
        state = RouterState()
        for turn in scenario.get("turns", []):
            route = route_turn(state, turn["user"])
            leaked = route.allowed_tools & DECOY_TOOLS
            assert not leaked, f"{scenario['id']}: decoy в allowed_tools: {leaked}"


def test_route_is_deterministic():
    """Один и тот же message + одинаковое состояние → тот же результат."""
    msg = "Загрузи аудит-лог SSE за 2024-06-15"
    first = route_turn(RouterState(), msg)
    second = route_turn(RouterState(), msg)
    assert first.allowed_tools == second.allowed_tools
    assert first.matched_domains == second.matched_domains


def test_core_tools_always_available_even_with_no_keyword_match():
    """CORE — не 'домен', а базовый набор: доступен даже без единого keyword-хита."""
    route = route_turn(RouterState(), "Расскажи анекдот про компиляторы")
    assert route.matched_domains == frozenset()
    assert CORE_TOOLS <= route.allowed_tools
    assert route.allowed_tools.isdisjoint(HR_TOOLS | CRM_TOOLS | SSE_TOOLS)


def test_domain_is_sticky_across_turns_without_keyword_repetition():
    """Once активирован — домен не 'забывается' на follow-up ходу без keyword.

    Это прямое исправление провала v1: s05_003 ("А напомни — в каком отделе
    работает Maria Chen?") не содержит ни одного HR-keyword и после первого
    хода ('Найди в системе профиль Maria Chen') должен оставаться в домене HR.
    """
    state = RouterState()
    first = route_turn(state, "Найди в системе профиль Maria Chen")
    assert Domain.HR in first.activated_domains

    second = route_turn(state, "А напомни — в каком отделе работает Maria Chen?")
    assert Domain.HR in second.activated_domains
    assert "org_chart_dept" in second.allowed_tools
    assert "employee_lookup" in second.allowed_tools


def test_multi_domain_union_not_first_match_wins():
    """Сообщение, задевающее два домена, получает объединение их tools."""
    route = route_turn(
        RouterState(),
        "Найди сотрудника-контакта и заодно карточку клиента CRM по этой сделке",
    )
    assert Domain.HR in route.matched_domains
    assert Domain.CRM in route.matched_domains
    assert "employee_lookup" in route.allowed_tools
    assert "customer_get" in route.allowed_tools


def test_unmatched_message_falls_back_to_full_core_not_narrow_subset():
    """v1 давал 2 tool на unknown; v2 не имеет 'crippled' fallback — сразу CORE (11 tools)."""
    route = route_turn(RouterState(), "Расскажи анекдот про компиляторы")
    assert len(route.allowed_tools) == len(CORE_TOOLS)


def test_decoys_never_in_allowed_sets():
    samples = [
        "Найди профиль в HR-системе",
        "Загрузи карточку клиента из CRM",
        "Получи политику безопасности",
        "Аудит-лог SSE",
        "Проверь погоду в Москве",
    ]
    for msg in samples:
        route = route_turn(RouterState(), msg)
        assert route.allowed_tools.isdisjoint(DECOY_TOOLS)


def test_domain_tool_sets_match_registry():
    """Карта доменов соответствует группировке mcp_tool_registry."""
    assert HR_TOOLS == frozenset({"employee_lookup", "leave_balance", "org_chart_dept"})
    assert CRM_TOOLS == frozenset({"customer_get", "ticket_create", "sales_quote"})
    assert SSE_TOOLS == frozenset({"benchmark_sse_probe", "sse_audit_log"})
    assert POLICY_TOOLS == frozenset({"get_policy_fact"})
    assert "get_policy_fact" in CORE_TOOLS
    assert "decoy_policy_fact" not in CORE_TOOLS


def test_filter_tools_by_route():
    tools = [_FakeTool("employee_lookup"), _FakeTool("customer_get"), _FakeTool("decoy_employee_search")]
    route = route_turn(RouterState(), "Найди сотрудника в HR-системе")
    filtered = filter_tools_by_route(tools, route)
    names = {t.name for t in filtered}
    assert names == {"employee_lookup"}


def test_is_tool_allowed_respects_route():
    route = route_turn(RouterState(), "Создай тикет поддержки")
    assert is_tool_allowed("ticket_create", route)
    assert not is_tool_allowed("employee_lookup", route)
    assert not is_tool_allowed("decoy_customer_lookup", route)
    # CORE доступен всегда, независимо от домена
    assert is_tool_allowed("calc_expression", route)


def test_no_tools_recall_hint_does_not_remove_tools():
    """Recall-подсказка — это только текст в prompt, а не срез allowed_tools.

    v1 обнулял tools на no_tools-интенте, что ломало ходы, где recall-фраза
    ошибочно триггерилась на самом деле нужном tool-ходу. v2 никогда не
    режет tools из-за recall-эвристики — только добавляет подсказку в промпт.
    """
    state = RouterState()
    route_turn(state, "Найди сотрудника Ivan Petrov")
    route = route_turn(state, "Напомни без повторных вызовов — из какого он отдела?")
    assert route.is_recall_hint
    assert "employee_lookup" in route.allowed_tools
    assert "повтор" in route.prompt_fragment.lower() or "контекст" in route.prompt_fragment.lower()
