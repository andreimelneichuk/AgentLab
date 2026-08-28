"""Границы доменов и фильтрация tools по MCP mount."""
from __future__ import annotations

from typing import Dict, Iterable, List, Set

from langchain_core.tools import BaseTool

DomainName = str

DOMAIN_CORE: DomainName = "core"
DOMAIN_HR: DomainName = "hr"
DOMAIN_CRM: DomainName = "crm"
DOMAIN_SSE: DomainName = "sse"

ALL_DOMAINS: tuple[DomainName, ...] = (DOMAIN_CORE, DOMAIN_HR, DOMAIN_CRM, DOMAIN_SSE)

DOMAIN_TOOL_NAMES: Dict[DomainName, Set[str]] = {
  DOMAIN_CORE: {
    "benchmark_probe",
    "get_policy_fact",
    "python_doc_lookup",
    "decoy_python_lookup",
    "decoy_policy_fact",
    "random_marker_probe",
    "flaky_tool",
    "empty_search",
    "inventory_lookup",
    "decoy_inventory_lookup",
    "invoice_get",
    "weather_city",
    "translate_text",
    "calc_expression",
  },
  DOMAIN_HR: {
    "employee_lookup",
    "leave_balance",
    "org_chart_dept",
    "decoy_employee_search",
  },
  DOMAIN_CRM: {
    "customer_get",
    "ticket_create",
    "sales_quote",
    "decoy_customer_lookup",
  },
  DOMAIN_SSE: {
    "benchmark_sse_probe",
    "sse_audit_log",
    "decoy_sse_cache",
  },
}

TOOL_TO_DOMAIN: Dict[str, DomainName] = {
  tool: domain
  for domain, tools in DOMAIN_TOOL_NAMES.items()
  for tool in tools
}


def domain_for_tool(tool_name: str) -> DomainName | None:
  """Возвращает домен инструмента или None."""
  return TOOL_TO_DOMAIN.get(tool_name)


def filter_tools_for_domain(tools: Iterable[BaseTool], domain: DomainName) -> List[BaseTool]:
  """Оставляет только tools домена (без cross-domain decoy)."""
  allowed = DOMAIN_TOOL_NAMES.get(domain, set())
  return [tool for tool in tools if tool.name in allowed]


def tools_belong_to_domain(tool_names: Iterable[str], domain: DomainName) -> bool:
  """Проверяет, что все tools принадлежат домену."""
  allowed = DOMAIN_TOOL_NAMES.get(domain, set())
  return all(name in allowed for name in tool_names)
