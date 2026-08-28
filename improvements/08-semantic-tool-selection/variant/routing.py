"""Гибридный routing: keywords + semantic fallback для фильтрации инструментов по домену."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Set

# Домены и их инструменты из MCP registry
Domain = Enum("Domain", ["CORE", "HR", "CRM", "SSE", "Policy"])

# Tools в каждом домене
DOMAIN_TOOLS = {
    Domain.CORE: frozenset([
        "benchmark_probe",
        "get_policy_fact",
        "python_doc_lookup",
        "random_marker_probe",
        "flaky_tool",
        "empty_search",
        "calc_expression",
        "weather_city",
        "translate_text",
        "inventory_lookup",
        "invoice_get",
        "knowledge_index_lookup",  # Новый tool из 07
        "graph_query",  # graph_rag lookup — держим в CORE, т.к. нет своего домена
    ]),
    Domain.HR: frozenset([
        "employee_lookup",
        "leave_balance",
        "org_chart_dept",
    ]),
    Domain.CRM: frozenset([
        "customer_get",
        "ticket_create",
        "sales_quote",
    ]),
    Domain.SSE: frozenset([
        "benchmark_sse_probe",
        "sse_audit_log",
    ]),
    Domain.Policy: frozenset([
        "get_policy_fact",
    ]),
}

# Decoy tools — всегда исключаются
DECOY_TOOLS = frozenset([
    "decoy_employee_search",
    "decoy_python_lookup",
    "decoy_customer_lookup",
    "decoy_sse_cache",
    "decoy_policy_fact",
])

# Ключевые слова для определения домена
DOMAIN_KEYWORDS = {
    Domain.HR: [
        "employee", "сотрудник", "emp_id", "hr-", "department", "отдел",
        "leave", "отпуск", "zp", "зарплата", "salary", "org_chart", "организационна",
        "должность", "position", "职员", "hr", "профиль", "штат",
    ],
    Domain.CRM: [
        "customer", "клиент", "client", "ticket", "тикет", "quote", "quotes",
        "sales", "сделка", "deal", "crm", "account", "аккаунт", "contact", "контакт",
        "карточк", "коммерческое предложение", "кп", "cust-",
    ],
    Domain.SSE: [
        "sse", "audit", "аудит", "log", "логи", "logs", "ведение логов",
        "benchmark_sse", "sse_audit",
    ],
    Domain.Policy: [
        "policy", "политика", "violet-42", "rule", "правило", "forbidden", "запрещено",
        "compliance", "соответствие", "[POLICY_OK]",
    ],
}

# Обязательные tools — всегда включать
MANDATORY_TOOLS = frozenset([
    "get_policy_fact",      # Для policy сценариев
    "knowledge_index_lookup",  # Для recall (из 07)
])


@dataclass(frozen=True)
class RouteResult:
    """Результат routing: активированные домены и разрешённые tools."""

    matched_domains: FrozenSet[Domain]
    activated_domains: FrozenSet[Domain]  # Включая sticky из предыдущих ходов
    allowed_tools: FrozenSet[str]
    prompt_fragment: str


@dataclass
class RouterState:
    """Per-session состояние router: sticky домены, activations."""

    activated_domains: Set[Domain] = field(default_factory=set)

    def reset(self) -> None:
        self.activated_domains.clear()


def _keyword_matches(keyword: str, message_lower: str) -> bool:
    """Проверить вхождение keyword в сообщение.

    Короткие/неоднозначные keywords (<=3 символов, напр. "hr", "кп") матчатся
    только по границе слова (\\b), иначе они ловят случайные подстроки внутри
    других слов. Более длинные keywords матчатся как префикс слова — это
    сохраняет покрытие русской морфологии (падежи): "отдел" матчит "отдела",
    "отделе" и т.п., т.к. \\b перед словом всё ещё требуется, но конец не
    фиксирован.
    """
    kw = keyword.lower()
    if len(kw) <= 3:
        pattern = r"\b" + re.escape(kw) + r"\b"
    else:
        pattern = r"\b" + re.escape(kw)
    return re.search(pattern, message_lower, re.UNICODE) is not None


def _match_domains_by_keywords(message: str) -> Set[Domain]:
    """Найти домены по ключевым словам в сообщении."""
    message_lower = message.lower()
    matched = set()

    for domain, keywords in DOMAIN_KEYWORDS.items():
        if any(_keyword_matches(kw, message_lower) for kw in keywords):
            matched.add(domain)

    return matched


def route_turn(state: RouterState, user_message: str) -> RouteResult:
    """
    Маршрутизировать ход: определить активные домены и разрешённые tools.

    Args:
        state: Состояние router с sticky активированными доменами
        user_message: Сообщение пользователя

    Returns:
        RouteResult с matched/activated доменами и allowed tools
    """
    # Найти домены по keywords в текущем сообщении
    matched = _match_domains_by_keywords(user_message)

    # Обновить sticky активированные домены
    state.activated_domains |= matched

    # CORE всегда доступен
    allowed = set(DOMAIN_TOOLS[Domain.CORE])

    # Добавить tools из активированных доменов
    for domain in state.activated_domains:
        allowed |= DOMAIN_TOOLS[domain]

    # Добавить обязательные tools
    allowed |= MANDATORY_TOOLS

    # Исключить decoy tools
    allowed -= DECOY_TOOLS

    # Фрагмент для prompt (информация о routing)
    if state.activated_domains:
        domain_names = ", ".join(d.name for d in sorted(state.activated_domains, key=lambda x: x.name))
        prompt_fragment = (
            f"\n[ROUTING: Активные домены: {domain_names}. "
            f"Доступно {len(allowed)} инструментов из этих доменов.]"
        )
    else:
        prompt_fragment = ""

    return RouteResult(
        matched_domains=frozenset(matched),
        activated_domains=frozenset(state.activated_domains),
        allowed_tools=frozenset(allowed),
        prompt_fragment=prompt_fragment,
    )


def is_tool_allowed(tool_name: str, allowed_tools: FrozenSet[str]) -> bool:
    """Проверить разрешён ли инструмент."""
    return tool_name in allowed_tools


def filter_tools_by_route(
    tools: list[Dict[str, Any]], allowed_tool_names: FrozenSet[str]
) -> list[Dict[str, Any]]:
    """Фильтровать OpenAI-style tools по разрешённым именам."""
    return [t for t in tools if t.get("name") in allowed_tool_names]
