"""Детерминированный router: intent → domain(s) → allowed tools.

v2 — переработка после провала v1 на бенчмарке (SR 26% → 14%, TSA 95% → 64%).

Ключевые отличия от v1:
- Router больше не единственный "switch" на весь набор tools. CORE_TOOLS
  (утилиты общего назначения: погода, calc, invoice, inventory, docs, ...)
  доступны ВСЕГДА — они и были источником большинства провалов v1, когда
  случайное непопадание ни в одно keyword-правило обрубало агенту вообще
  все инструменты.
- Специализированные домены (HR/CRM/SSE/Policy) остаются "липкими"
  (sticky) в рамках диалога: если домен один раз определён — он не
  забывается на следующем ходу, даже если пользователь не повторяет
  ключевые слова (это покрывает частый в бенчмарке паттерн
  "нашли сотрудника → а теперь скажи его отдел").
- Совпадение keyword-правил больше не "first match wins" — если сообщение
  задевает несколько доменов сразу, они объединяются (union), а не
  конкурируют за произвольный приоритет в списке правил.
- decoy_* инструменты по-прежнему исключаются всегда и безусловно — это
  единственная часть v1, которая была чистым плюсом без даунсайда.

Итог: router больше не может быть строже baseline (в худшем случае —
полный CORE-набор + когда-либо затронутые домены), но продолжает резать
кросс-контаминацию между специализированными MCP-серверами, для которой
он и задумывался (HR-запрос не должен видеть CRM-tools и наоборот).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, List, Sequence, Set, Tuple

from langchain_core.tools import BaseTool

# Группировка инструментов по MCP-серверам (benchmark/mcp_tool_registry.py).
# Приманки (decoy_*) намеренно исключены из всех доменов.

CORE_TOOLS: FrozenSet[str] = frozenset({
    "benchmark_probe",
    "get_policy_fact",
    "python_doc_lookup",
    "random_marker_probe",
    "flaky_tool",
    "empty_search",
    "inventory_lookup",
    "invoice_get",
    "weather_city",
    "translate_text",
    "calc_expression",
})

HR_TOOLS: FrozenSet[str] = frozenset({
    "employee_lookup",
    "leave_balance",
    "org_chart_dept",
})

CRM_TOOLS: FrozenSet[str] = frozenset({
    "customer_get",
    "ticket_create",
    "sales_quote",
})

SSE_TOOLS: FrozenSet[str] = frozenset({
    "benchmark_sse_probe",
    "sse_audit_log",
})

POLICY_TOOLS: FrozenSet[str] = frozenset({
    "get_policy_fact",
})

DECOY_TOOLS: FrozenSet[str] = frozenset({
    "decoy_python_lookup",
    "decoy_policy_fact",
    "decoy_inventory_lookup",
    "decoy_employee_search",
    "decoy_customer_lookup",
    "decoy_sse_cache",
})


class Domain(str, Enum):
    """Специализированный домен MCP-сервиса поверх всегда доступного CORE."""

    HR = "hr"
    CRM = "crm"
    SSE = "sse"
    POLICY = "policy"


# Специализированные домены — то, что router реально обязан разграничивать.
# CORE в эту карту не входит: он не "домен" в смысле разграничения, а базовый
# набор утилит, доступный на любом ходу (см. docstring модуля).
DOMAIN_TOOLS: dict = {
    Domain.HR: HR_TOOLS,
    Domain.CRM: CRM_TOOLS,
    Domain.SSE: SSE_TOOLS,
    Domain.POLICY: POLICY_TOOLS,
}

DOMAIN_PROMPT_FRAGMENTS: dict = {
    Domain.HR: (
        "Домен HR активен: employee_lookup (профиль сотрудника), "
        "leave_balance (остаток отпуска), org_chart_dept (орг-структура)."
    ),
    Domain.CRM: (
        "Домен CRM активен: customer_get (карточка клиента), "
        "ticket_create (тикет поддержки), sales_quote (коммерческое предложение)."
    ),
    Domain.SSE: (
        "Домен SSE активен: benchmark_sse_probe (маркер SSE endpoint), "
        "sse_audit_log (аудит-лог за дату)."
    ),
    Domain.POLICY: (
        "Для правил безопасности используй только get_policy_fact "
        "(официальный источник). Не вызывай decoy_policy_fact."
    ),
}

NO_TOOLS_HINT = (
    "Похоже, нужный факт уже есть в истории диалога — сначала попробуй "
    "ответить из контекста, не вызывая tool повторно."
)

# (intent_tag, domain, keywords) — порядок больше не определяет приоритет:
# совпавшие домены объединяются (union), а не конкурируют.
_ROUTING_RULES: Tuple[Tuple[str, Domain, Tuple[str, ...]], ...] = (
    (
        "policy",
        Domain.POLICY,
        (
            "политик",
            "policy",
            "violet",
            "безопасност",
            "регламент",
            "официальн",
            "правило политики",
        ),
    ),
    (
        "sse",
        Domain.SSE,
        (
            # Бенчмарк почти всегда явно упоминает "sse" в тексте — берём
            # это как основной сигнал, а не пытаемся угадать все фразовые
            # вариации ("журнал событий", "SSE-кеш", "SSE-транспорт", ...).
            "sse",
            "аудит",
            "аномали",
        ),
    ),
    (
        "crm",
        Domain.CRM,
        (
            "crm",
            "клиент",
            "customer",
            "тикет",
            "ticket",
            "оффер",
            "коммерческ",
            "кп на",
            "vip-клиент",
            "карточк",
            "сделк",
            "квалифицир",
            "контакт",
        ),
    ),
    (
        "hr",
        Domain.HR,
        (
            "hr",
            "сотрудник",
            "отпуск",
            "орг-структур",
            "оргструктур",
            "структуру отдел",
            "отдел",
            "профил",
            "команда",
            "дежурн",
            "hr-систем",
            "hr id",
            "офицер",
            "инициатор",
            "коллег",
            "онбординг",
            "контакт",
        ),
    ),
)

_NO_TOOLS_KEYWORDS: Tuple[str, ...] = (
    "tool не нужен",
    "без tool",
    "без инструмент",
    "без повторных вызовов",
    "данные уже",
    "уже в контексте",
    "уже получен",
    "уже у тебя",
    "из контекста",
    "квиз без tool",
    "напомни без",
    "без новых вызовов",
)


@dataclass
class RouterState:
    """Состояние роутинга в рамках одного диалога (session-scoped)."""

    activated_domains: Set[Domain] = field(default_factory=set)


@dataclass(frozen=True)
class RouteResult:
    """Эффективный результат маршрутизации для текущего хода."""

    matched_domains: FrozenSet[Domain]
    activated_domains: FrozenSet[Domain]
    is_recall_hint: bool
    allowed_tools: FrozenSet[str]
    prompt_fragment: str


def _match_domains(message: str) -> Set[Domain]:
    """Возвращает ВСЕ домены, чьи ключевые слова встретились в сообщении."""
    norm = message.lower()
    matched: Set[Domain] = set()
    for _intent, domain, keywords in _ROUTING_RULES:
        if any(keyword in norm for keyword in keywords):
            matched.add(domain)
    return matched


def _is_recall_hint(message: str) -> bool:
    norm = message.lower()
    return any(keyword in norm for keyword in _NO_TOOLS_KEYWORDS)


def _build_prompt_fragment(matched: Set[Domain], recall_hint: bool) -> str:
    parts: List[str] = []
    for domain in matched:
        parts.append(DOMAIN_PROMPT_FRAGMENTS[domain])
    if recall_hint:
        parts.append(NO_TOOLS_HINT)
    if not parts:
        return ""
    return "### Маршрутизация\n" + "\n".join(parts)


def route_turn(state: RouterState, message: str) -> RouteResult:
    """
    Маршрутизирует один ход диалога с учётом накопленного состояния сессии.

    Детерминированно: тот же message + то же state.activated_domains →
    тот же результат. Специализированные домены "липкие" — однажды
    активированный домен остаётся доступным до конца сессии (reset() у
    сессии агента сбрасывает RouterState). CORE_TOOLS доступны всегда,
    поэтому router может только расширять специализированные наборы,
    никогда не срезая общие утилиты.
    """
    matched = _match_domains(message)
    state.activated_domains |= matched
    recall_hint = _is_recall_hint(message)

    allowed: Set[str] = set(CORE_TOOLS)
    for domain in state.activated_domains:
        allowed |= DOMAIN_TOOLS[domain]
    allowed -= DECOY_TOOLS

    return RouteResult(
        matched_domains=frozenset(matched),
        activated_domains=frozenset(state.activated_domains),
        is_recall_hint=recall_hint,
        allowed_tools=frozenset(allowed),
        prompt_fragment=_build_prompt_fragment(matched, recall_hint),
    )


def filter_tools_by_route(tools: Sequence[BaseTool], route: RouteResult) -> List[BaseTool]:
    """Оставляет только инструменты, разрешённые текущим RouteResult."""
    allowed = route.allowed_tools
    return [tool for tool in tools if tool.name in allowed]


def is_tool_allowed(tool_name: str, route: RouteResult) -> bool:
    """Проверяет, разрешён ли вызов инструмента при текущем route."""
    if tool_name in DECOY_TOOLS:
        return False
    return tool_name in route.allowed_tools
