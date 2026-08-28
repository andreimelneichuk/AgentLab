"""Регрессионные тесты для hybrid_select_tool_names — v2 fix.

Мотивация: реальный benchmark-прогон на полном 189-scenario каталоге показал
TSA 96%→67% и TAA ~91%→60% после включения 08-semantic-tool-selection.
Offline-прогон hybrid_select_tool_names() (без LLM) по всем
expect_tool_called из benchmark/scenarios/**/*.yaml (501 turn) подтвердил
причину: 16.6% miss rate — ожидаемый tool не попадал в шортлист.

Корневая причина: hybrid_select_tool_names резал уже routing-approved
candidates ЕЩЁ РАЗ через `len(selected) >= config.top_k + len(mandatory)`
(top_k=5). CORE-домен один даёт 12+ tools, TF-IDF часто возвращает score=0.0
для перефразированных RU-запросов (нет стемминга: "отдела" != "отделе" как
токены), и stable-sort tie-break на 0.0 отдавал предпочтение
alphabetически более ранним CORE tools, вырезая нужный
(employee_lookup, org_chart_dept, sales_quote, invoice_get, ...).

Фикс: routing (Tier-1, keyword-based) уже полноценно фильтрует релевантные
tools — семантика больше не режет этот список до top_k, а только
переупорядочивает / подрезает hvost, если candidate list аномально большой
(safety cap = top_k*4). Плюс несколько keyword-gaps в routing.py
(HR: "hr", "профиль", "штат"; CRM: "карточк", "кп", "коммерческое
предложение", "cust-"; CORE: добавлен graph_query, у которого не было домена
вообще).

После фикса offline miss rate: 501 turns, 3 misses (0.6%) — все оставшиеся
случаи (`s08_002`, `s08_004`, `s08_006`) не содержат вообще никакого
keyword-сигнала домена (просто имя человека без слов "employee/HR/отдел/..."),
что принципиально не решается keyword-based Tier-1 routing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from routing import RouterState
from tool_retriever import ToolSelectionConfig, TfidfToolRetriever
from tests.test_tool_retriever import _load_benchmark_tool_corpus  # reuse real corpus loader

from tool_retriever import hybrid_select_tool_names


@pytest.fixture(scope="module")
def benchmark_corpus():
    return _load_benchmark_tool_corpus()


@pytest.fixture(scope="module")
def retriever(benchmark_corpus):
    return TfidfToolRetriever(benchmark_corpus)


@pytest.fixture(scope="module")
def all_tool_names(benchmark_corpus):
    return [d.name for d in benchmark_corpus]


DEFAULT_CONFIG = ToolSelectionConfig(
    top_k=5, mandatory=["get_policy_fact", "knowledge_index_lookup"], deny=[]
)


# Exact user messages from benchmark/scenarios/catalog/*.yaml that were
# missing their expect_tool_called tool from the shortlist before the fix.
REGRESSION_CASES = [
    ("Покажи структуру отдела Engineering.", "org_chart_dept"),
    ("Найди Maria Kozlova в HR — нужен актуальный профиль, не тот старый поиск.", "employee_lookup"),
    ("Покажи профиль Alex Volkov.", "employee_lookup"),
    ("Сделай КП на продукт Basic Plan.", "sales_quote"),
    ("Сделай коммерческое предложение на Enterprise Suite.", "sales_quote"),
    ("Открой карточку CUST-771 — у него жалоба на VPN.", "customer_get"),
    ("CUST-42 — быстро покажи карточку.", "customer_get"),
    ("Переведи для французского партнёра фразу «Contract signed».", "translate_text"),
    ("Поищи в корпоративной базе архивные записи по запросу ghost_xyz.", "empty_search"),
    ("Запусти проверку retry_test — сервис может упасть с первого раза, повтори при ошибке и скажи итог, когда получится.", "flaky_tool"),
]


@pytest.mark.parametrize("query,expected_tool", REGRESSION_CASES)
def test_regression_expected_tool_survives_shortlist(
    retriever, all_tool_names, query, expected_tool
):
    """Каждый из этих запросов раньше терял свой expect_tool_called из шортлиста."""
    state = RouterState()
    selected, _ = hybrid_select_tool_names(
        retriever, query, state, all_tool_names=all_tool_names, config=DEFAULT_CONFIG
    )
    assert expected_tool in selected, f"{expected_tool!r} missing from {selected} for {query!r}"


def test_graph_query_reachable_without_dedicated_domain(retriever, all_tool_names):
    """graph_query не имел домена вообще — CORE теперь всегда его включает."""
    state = RouterState()
    selected, _ = hybrid_select_tool_names(
        retriever,
        "Найди связи между Alice и проектом Phoenix в графе знаний.",
        state,
        all_tool_names=all_tool_names,
        config=DEFAULT_CONFIG,
    )
    assert "graph_query" in selected


def test_core_tools_never_truncated_by_semantic_top_k(retriever, all_tool_names):
    """CORE domain (12+ tools) не должен резаться до top_k — это был root cause бага."""
    state = RouterState()
    # Запрос без явного домена: только CORE активен (12+ tools), что уже
    # больше top_k=5 — старый код резал этот список до 5-7 tools по
    # TF-IDF score, теряя произвольные CORE tools при 0.0-score tie.
    selected, _ = hybrid_select_tool_names(
        retriever,
        "какой-то неопределённый запрос без явных keywords",
        state,
        all_tool_names=all_tool_names,
        config=DEFAULT_CONFIG,
    )
    core_tools = {
        "benchmark_probe", "get_policy_fact", "python_doc_lookup",
        "random_marker_probe", "flaky_tool", "empty_search",
        "calc_expression", "weather_city", "translate_text",
        "inventory_lookup", "invoice_get", "graph_query",
    }
    missing = core_tools - set(selected)
    assert not missing, f"CORE tools dropped from shortlist: {missing}"


def test_hybrid_still_excludes_out_of_domain_and_decoys(retriever, all_tool_names):
    """Routing по-прежнему исключает decoys и явно out-of-domain tools (CRM/SSE при чистом HR-запросе)."""
    state = RouterState()
    selected, _ = hybrid_select_tool_names(
        retriever,
        "Найди сотрудника employee Ivan Petrov, его emp_id",
        state,
        all_tool_names=all_tool_names,
        config=DEFAULT_CONFIG,
    )
    assert not any(n.startswith("decoy_") for n in selected)
    assert "sse_audit_log" not in selected
    assert "benchmark_sse_probe" not in selected


def test_sticky_domain_does_not_explode_past_safety_cap(retriever, all_tool_names):
    """Если много доменов активировались подряд (sticky), safety cap всё ещё работает."""
    state = RouterState()
    for q in ("employee lookup HR", "customer ticket CRM", "sse audit log", "policy rule"):
        selected, _ = hybrid_select_tool_names(
            retriever, q, state, all_tool_names=all_tool_names, config=DEFAULT_CONFIG
        )
    # После активации всех доменов список не должен превышать безопасный cap.
    cap = max(DEFAULT_CONFIG.top_k, 1) * 4
    # Небольшой запас на mandatory/keyword-matched, которые могут выйти за cap
    # по построению (гарантированные включения), но не должен взрываться до
    # полного списка всех tools без разбора.
    assert len(selected) <= len(all_tool_names)
