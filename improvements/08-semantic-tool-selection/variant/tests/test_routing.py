"""Unit-тесты для keyword routing и RouterState."""
from __future__ import annotations

import pytest

from routing import (
    Domain,
    DOMAIN_KEYWORDS,
    DOMAIN_TOOLS,
    DECOY_TOOLS,
    MANDATORY_TOOLS,
    RouterState,
    route_turn,
    _match_domains_by_keywords,
    is_tool_allowed,
    filter_tools_by_route,
)


def test_domain_keywords_matching_hr():
    """Keyword matching для HR домена."""
    messages = [
        "Find an employee named Alice",
        "Сотрудник Bob",
        "emp_id для разработчика",
        "Какой отдел у Carol?",
    ]
    for msg in messages:
        matched = _match_domains_by_keywords(msg)
        assert Domain.HR in matched, f"Failed for: {msg}"


def test_domain_keywords_matching_crm():
    """Keyword matching для CRM домена."""
    messages = [
        "Get customer C-001",
        "Create a ticket",
        "Сделка с клиентом",
        "Quote для ProductX",
    ]
    for msg in messages:
        matched = _match_domains_by_keywords(msg)
        assert Domain.CRM in matched, f"Failed for: {msg}"


def test_domain_keywords_matching_sse():
    """Keyword matching для SSE домена."""
    messages = [
        "Check SSE audit log",
        "Аудит за 2024-01",
        "SSE benchmark",
    ]
    for msg in messages:
        matched = _match_domains_by_keywords(msg)
        assert Domain.SSE in matched, f"Failed for: {msg}"


def test_domain_keywords_matching_policy():
    """Keyword matching для Policy домена."""
    messages = [
        "What is the policy?",
        "Какая политика безопасности?",
        "violet-42",
    ]
    for msg in messages:
        matched = _match_domains_by_keywords(msg)
        assert Domain.Policy in matched, f"Failed for: {msg}"


def test_router_state_sticky_activation():
    """Activated domains остаются между ходами (sticky)."""
    state = RouterState()

    # Ход 1: HR домен активируется
    result1 = route_turn(state, "Find employee Alice")
    assert Domain.HR in result1.activated_domains

    # Ход 2: Обычный вопрос (без keywords)
    result2 = route_turn(state, "What is 2+2?")
    # HR должен остаться (sticky)
    assert Domain.HR in result2.activated_domains
    assert result2.activated_domains == result1.activated_domains


def test_router_state_multiple_domains_sticky():
    """Multiple домены могут быть активированы и остаются sticky."""
    state = RouterState()

    # Ход 1: HR
    route_turn(state, "Find employee")
    assert Domain.HR in state.activated_domains

    # Ход 2: CRM
    route_turn(state, "Create a ticket")
    assert Domain.HR in state.activated_domains
    assert Domain.CRM in state.activated_domains


def test_router_state_reset():
    """reset() сбрасывает activated_domains."""
    state = RouterState()
    route_turn(state, "Find employee")
    assert len(state.activated_domains) > 0

    state.reset()
    assert len(state.activated_domains) == 0


def test_route_turn_allowed_tools_core_always():
    """CORE домен всегда разрешён."""
    state = RouterState()
    result = route_turn(state, "random unmatched message xyz")

    # CORE tools должны быть в allowed
    allowed_names = set(result.allowed_tools)
    core_tools = DOMAIN_TOOLS[Domain.CORE]
    assert core_tools.issubset(allowed_names)


def test_route_turn_allowed_tools_by_domain():
    """Allowed tools включают tools из активированных доменов."""
    state = RouterState()
    result = route_turn(state, "Find an employee")

    # HR tools должны быть в allowed
    hr_tools = DOMAIN_TOOLS[Domain.HR]
    assert hr_tools.issubset(result.allowed_tools)


def test_route_turn_decoy_excluded():
    """Decoy tools исключаются из allowed."""
    state = RouterState()
    result = route_turn(state, "Find employee")

    # Ни один decoy не должен быть в allowed
    allowed_names = set(result.allowed_tools)
    assert not (DECOY_TOOLS & allowed_names)


def test_route_turn_mandatory_always_included():
    """Mandatory tools всегда в allowed_tools."""
    state = RouterState()
    result = route_turn(state, "random message")

    # Mandatory tools должны быть в allowed
    assert MANDATORY_TOOLS.issubset(result.allowed_tools)


def test_route_turn_returns_route_result():
    """route_turn возвращает правильный RouteResult."""
    state = RouterState()
    result = route_turn(state, "Find employee")

    assert result.matched_domains is not None
    assert result.activated_domains is not None
    assert result.allowed_tools is not None
    assert isinstance(result.prompt_fragment, str)


def test_route_result_matched_vs_activated():
    """matched_domains — на этом ходе, activated — включая sticky."""
    state = RouterState()

    # Ход 1: HR
    result1 = route_turn(state, "Find employee")
    assert Domain.HR in result1.matched_domains
    assert Domain.HR in result1.activated_domains

    # Ход 2: CRM (без HR keywords)
    result2 = route_turn(state, "Create ticket")
    assert Domain.CRM in result2.matched_domains
    assert Domain.HR not in result2.matched_domains  # HR НЕ в matched (т.к. нет keywords на этом ходе)
    assert Domain.CRM in result2.activated_domains
    assert Domain.HR in result2.activated_domains  # HR остался (sticky)


def test_is_tool_allowed():
    """is_tool_allowed проверяет инструмент."""
    allowed = frozenset(["employee_lookup", "leave_balance", "get_policy_fact"])

    assert is_tool_allowed("employee_lookup", allowed)
    assert is_tool_allowed("get_policy_fact", allowed)
    assert not is_tool_allowed("customer_get", allowed)
    assert not is_tool_allowed("decoy_employee_search", allowed)


def test_filter_tools_by_route_openai_dicts():
    """filter_tools_by_route фильтрует OpenAI-style dicts."""
    openai_tools = [
        {"name": "employee_lookup", "description": "...", "parameters": {}},
        {"name": "customer_get", "description": "...", "parameters": {}},
        {"name": "get_policy_fact", "description": "...", "parameters": {}},
    ]

    allowed = frozenset(["employee_lookup", "get_policy_fact"])
    filtered = filter_tools_by_route(openai_tools, allowed)

    names = [t["name"] for t in filtered]
    assert "employee_lookup" in names
    assert "get_policy_fact" in names
    assert "customer_get" not in names


def test_prompt_fragment_non_empty_when_domains_activated():
    """prompt_fragment содержит информацию об активных доменах."""
    state = RouterState()
    result = route_turn(state, "Find employee")

    assert result.prompt_fragment
    assert "ROUTING" in result.prompt_fragment or len(result.prompt_fragment) > 10


def test_prompt_fragment_empty_when_no_domains():
    """prompt_fragment может быть пусто если нет специальных доменов."""
    # Хотя обычно CORE + mandatory всегда есть, проверим поведение
    state = RouterState()
    result = route_turn(state, "2+2=?")

    # prompt_fragment может быть пусто или содержать только CORE
    assert isinstance(result.prompt_fragment, str)


def test_domain_keywords_hr_bare_abbreviation_and_profile():
    """Regression (v2 fix): plain 'HR', 'профиль', 'штат' must trigger HR domain.

    These are exact user messages from benchmark/scenarios/catalog/*.yaml that
    were NOT matching Domain.HR before the fix (old DOMAIN_KEYWORDS only had
    "hr-" with a trailing dash, and no "профиль"/"штат" entries at all),
    which meant employee_lookup/org_chart_dept never entered the candidate
    list handed to hybrid_select_tool_names.
    """
    messages = [
        "Найди Maria Kozlova в HR — нужен актуальный профиль, не тот старый поиск.",
        "Покажи профиль Alex Volkov.",
        "И напоследок найди James Park в штате — старый поиск врёт, бери нормальный каталог",
    ]
    for msg in messages:
        matched = _match_domains_by_keywords(msg)
        assert Domain.HR in matched, f"Failed for: {msg}"


def test_domain_keywords_crm_quote_and_card_abbreviations():
    """Regression (v2 fix): 'КП', 'карточка'/'карточку', 'CUST-' must trigger CRM domain."""
    messages = [
        "Сделай КП на продукт Basic Plan.",
        "Открой карточку CUST-771 — у него жалоба на VPN.",
        "CUST-42 — быстро покажи карточку.",
    ]
    for msg in messages:
        matched = _match_domains_by_keywords(msg)
        assert Domain.CRM in matched, f"Failed for: {msg}"


def test_short_keyword_hr_does_not_false_positive_on_substring():
    """'hr' is matched only as a whole word, not as a substring of an unrelated word."""
    matched = _match_domains_by_keywords("share the anywhere policy chart, please")
    assert Domain.HR not in matched


def test_graph_query_in_core_domain():
    """Regression (v2 fix): graph_query had no domain at all before the fix."""
    assert "graph_query" in DOMAIN_TOOLS[Domain.CORE]


def test_route_multiple_turns_session_simulation():
    """Simulation сессии на несколько ходов."""
    state = RouterState()
    messages = [
        "Find employee Alice",
        "What is her leave balance?",
        "Create a support ticket",
        "How many tickets are open?",
    ]

    domains_by_turn = []
    for msg in messages:
        result = route_turn(state, msg)
        domains_by_turn.append(set(result.activated_domains))

    # Ход 1: HR появляется
    assert Domain.HR in domains_by_turn[0]

    # Ход 2: HR остаётся
    assert Domain.HR in domains_by_turn[1]

    # Ход 3: CRM добавляется, HR остаётся
    assert Domain.CRM in domains_by_turn[2]
    assert Domain.HR in domains_by_turn[2]

    # Ход 4: CRM и HR остаются
    assert Domain.CRM in domains_by_turn[3]
    assert Domain.HR in domains_by_turn[3]
