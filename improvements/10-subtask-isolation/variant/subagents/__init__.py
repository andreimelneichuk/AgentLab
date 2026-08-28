"""Доменные субагенты с изолированным контекстом и tools."""
from subagents.domain_tools import (
  ALL_DOMAINS,
  DOMAIN_CORE,
  DOMAIN_CRM,
  DOMAIN_HR,
  DOMAIN_SSE,
  TOOL_TO_DOMAIN,
  domain_for_tool,
  filter_tools_for_domain,
)
from subagents.factory import SubagentFactory
from subagents.handoff import SubtaskHandoff

__all__ = [
  "ALL_DOMAINS",
  "DOMAIN_CORE",
  "DOMAIN_CRM",
  "DOMAIN_HR",
  "DOMAIN_SSE",
  "TOOL_TO_DOMAIN",
  "SubagentFactory",
  "SubtaskHandoff",
  "domain_for_tool",
  "filter_tools_for_domain",
]
