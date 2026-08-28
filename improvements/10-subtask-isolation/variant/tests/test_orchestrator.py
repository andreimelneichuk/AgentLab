"""Unit-тесты orchestrator и изоляции субагентов."""
from __future__ import annotations

import asyncio
import glob
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from agent_core import AgentResources, TurnResult
from orchestrator import (
  Orchestrator,
  SessionMemory,
  decompose_request,
  infer_all_domains,
  infer_domain,
  is_recall_turn,
)
from subagents.domain_tools import (
  DOMAIN_CORE,
  DOMAIN_CRM,
  DOMAIN_HR,
  DOMAIN_SSE,
  DOMAIN_TOOL_NAMES,
  TOOL_TO_DOMAIN,
  filter_tools_for_domain,
)
from subagents.factory import SubagentFactory
from subagents.handoff import SubtaskHandoff

CATALOG_DIR = Path(__file__).resolve().parents[4] / "benchmark" / "scenarios" / "catalog"


def _load_catalog_scenarios():
  scenarios = []
  for path in sorted(glob.glob(str(CATALOG_DIR / "*.yaml"))):
    with open(path, encoding="utf-8") as fh:
      data = yaml.safe_load(fh) or {}
    scenarios.extend(data.get("scenarios") or [])
  return scenarios


_CATALOG_SCENARIOS = _load_catalog_scenarios()


@dataclass
class _FakeTool:
  name: str
  description: str = ""
  args_schema: Optional[Dict[str, Any]] = None


def _all_benchmark_tool_names() -> List[str]:
  names: List[str] = []
  for domain_tools in DOMAIN_TOOL_NAMES.values():
    names.extend(sorted(domain_tools))
  return names


def _make_resources(tool_names: Optional[List[str]] = None) -> AgentResources:
  names = tool_names or _all_benchmark_tool_names()
  tools = [_FakeTool(name) for name in names]
  return AgentResources(
    config={"llm_defaults": {"model_alias": "test"}},
    tools=tools,
    tool_map={tool.name: tool for tool in tools},
    openai_tools=[{"name": t.name, "description": "", "parameters": {"type": "object", "properties": {}}} for t in tools],
    system_prompt="test",
    run_limit=5,
    thread_limit=50,
    tool_exec_fail_retries=3,
    prompt_path=__file__,
  )


def test_handoff_schema_roundtrip():
  handoff = SubtaskHandoff.ok(
    "policy_fact",
    data={"policy_code": "violet-42"},
    tools_used=["get_policy_fact"],
  )
  payload = handoff.to_dict()
  assert set(payload.keys()) == {"subtask", "status", "data", "tools_used", "errors"}
  assert payload["status"] == "ok"
  assert payload["data"]["policy_code"] == "violet-42"
  restored = SubtaskHandoff.from_dict(payload)
  assert restored.subtask == "policy_fact"
  assert restored.tools_used == ["get_policy_fact"]


def test_domain_tool_isolation_hr_vs_crm():
  resources = _make_resources()
  hr_tools = filter_tools_for_domain(resources.tools, DOMAIN_HR)
  crm_tools = filter_tools_for_domain(resources.tools, DOMAIN_CRM)
  hr_names = {tool.name for tool in hr_tools}
  crm_names = {tool.name for tool in crm_tools}

  assert "employee_lookup" in hr_names
  assert "customer_get" not in hr_names
  assert "customer_get" in crm_names
  assert "employee_lookup" not in crm_names
  assert "decoy_customer_lookup" not in hr_names
  assert "decoy_employee_search" not in crm_names


def test_subagent_factory_excludes_foreign_decoys():
  resources = _make_resources()
  factory = SubagentFactory(resources)
  sse_res = factory.resources_for(DOMAIN_SSE)
  assert "sse_audit_log" in sse_res.tool_map
  assert "decoy_sse_cache" in sse_res.tool_map
  assert "customer_get" not in sse_res.tool_map
  assert "get_policy_fact" not in sse_res.tool_map


def test_infer_domain_from_explicit_tool():
  assert infer_domain("Вызови sse_audit_log за 2024-06-15") == DOMAIN_SSE
  assert infer_domain("Найди employee_lookup для Anna") == DOMAIN_HR
  assert infer_domain("Загрузи customer_get CUST-100") == DOMAIN_CRM
  assert infer_domain("get_policy_fact из официального источника") == DOMAIN_CORE


def test_infer_domain_from_keywords():
  assert infer_domain("Проверь погоду в Берлине") == DOMAIN_CORE
  assert infer_domain("Орг-структура отдела Marketing") == DOMAIN_HR
  assert infer_domain("Коммерческое предложение для клиента") == DOMAIN_CRM
  assert infer_domain("Аудит-лог SSE за дату") == DOMAIN_SSE


def test_is_recall_turn_detection():
  assert is_recall_turn("Напомни данные — tool не нужен")
  assert is_recall_turn("Данные уже в контексте, без повторного вызова")
  assert not is_recall_turn("Найди сотрудника Elena Petrova в HR")


def test_decompose_execute_plan():
  plans = decompose_request("Найди профиль сотрудника Maria Sidorova в HR")
  assert len(plans) == 1
  assert plans[0].kind == "execute"
  assert plans[0].domain == DOMAIN_HR
  assert plans[0].subtask == "hr_employee"


def test_decompose_recall_plan():
  plans = decompose_request("Recall: имя сотрудника и регион клиента — данные уже получены")
  assert len(plans) == 1
  assert plans[0].kind == "recall"
  assert plans[0].domain is None


def test_all_benchmark_tools_mapped_to_domain():
  expected = set(_all_benchmark_tool_names())
  assert set(TOOL_TO_DOMAIN.keys()) == expected


def test_orchestrator_delegates_without_direct_tools(monkeypatch):
  resources = _make_resources()
  orchestrator = Orchestrator(resources, model_alias="test")

  fake_result = TurnResult(
    answer="EMP_ID=HR-ABC123",
    tool_calls=["employee_lookup"],
    tool_call_details=[{"name": "employee_lookup", "arguments": {"name": "Anna"}}],
    rounds=2,
  )

  mock_session = MagicMock()
  mock_session.run_turn = AsyncMock(return_value=fake_result)
  monkeypatch.setattr(
    orchestrator._factory,
    "spawn_session",
    MagicMock(return_value=mock_session),
  )

  result = asyncio.run(orchestrator.run_turn("Найди сотрудника Anna Smith в HR"))

  orchestrator._factory.spawn_session.assert_called_once_with(DOMAIN_HR)
  mock_session.run_turn.assert_awaited_once()
  assert result.tool_calls == ["employee_lookup"]
  assert orchestrator.trace[-1].handoffs[0].subtask == "hr_employee"
  assert orchestrator.trace[-1].handoffs[0].status == "ok"
  assert not hasattr(orchestrator, "tool_map")


def test_orchestrator_recall_uses_recall_session(monkeypatch):
  resources = _make_resources()
  orchestrator = Orchestrator(resources, model_alias="test")

  fake_result = TurnResult(answer="Fatima Al-Hassan, TIER=gold", tool_calls=[], rounds=1)
  mock_session = MagicMock()
  mock_session.run_turn = AsyncMock(return_value=fake_result)
  monkeypatch.setattr(
    orchestrator._factory,
    "spawn_recall_session",
    MagicMock(return_value=mock_session),
  )

  result = asyncio.run(
    orchestrator.run_turn("Recall: сотрудник и клиент — данные уже в контексте")
  )

  orchestrator._factory.spawn_recall_session.assert_called_once()
  assert result.tool_calls == []
  assert orchestrator.trace[-1].handoffs == []


def test_basic_orchestrator_session_reset():
  from orchestrator import BasicOrchestratorSession

  resources = _make_resources()
  session = BasicOrchestratorSession(resources, model_alias="test")
  session._orchestrator.memory.add(
    SubtaskHandoff.ok("hr_employee", data={"name": "Anna"}, tools_used=["employee_lookup"])
  )
  assert session._orchestrator.memory.handoffs

  session.reset()
  assert session._orchestrator.memory.handoffs == []
  assert session.history_dicts == []


# --- v2: regression-тесты пяти исправленных причин провала (SR=15%) ---


def test_decompose_returns_multiple_plans_for_multi_domain_message():
  """v2 fix #1: сообщение с keyword-hits в 2 доменах даёт 2 execute-плана."""
  plans = decompose_request("Найди сотрудника Alice Johnson и создай тикет для клиента CUST-100")
  domains = {p.domain for p in plans if p.kind == "execute"}
  assert DOMAIN_HR in domains
  assert DOMAIN_CRM in domains
  assert len(plans) >= 2


def test_infer_all_domains_single_domain_message():
  """Однодоменное сообщение всё ещё даёт один домен (без регрессии)."""
  domains = infer_all_domains("Найди сотрудника Anna Smith в HR")
  assert domains == [DOMAIN_HR]


@pytest.mark.skipif(not _CATALOG_SCENARIOS, reason="benchmark/scenarios/catalog не найден")
def test_recall_detection_coverage_on_real_catalog():
  """
  v2 fix #2: is_recall_turn() покрывал только 27.4% реальных recall-ходов
  каталога (max_tool_calls_delta=0) статичными keyword-маркерами v1.

  Регрессионный порог: coverage не должен упасть ниже достигнутого в v2
  (66%) — если упадёт, значит будущая правка сломала покрытие.
  """
  total = hits = 0
  for scenario in _CATALOG_SCENARIOS:
    mem = SessionMemory()
    for turn in scenario.get("turns", []):
      is_recall_expected = turn.get("max_tool_calls_delta") == 0
      if is_recall_expected:
        total += 1
        if is_recall_turn(turn["user"], mem):
          hits += 1
      parts = [turn["user"]] + [str(x) for x in (turn.get("expect_contains") or [])]
      mem.add(SubtaskHandoff.ok("turn", data={"answer": " ".join(parts)}))

  coverage = hits / total
  assert coverage >= 0.60, f"Recall coverage regressed: {hits}/{total} = {coverage:.1%}"


@pytest.mark.skipif(not _CATALOG_SCENARIOS, reason="benchmark/scenarios/catalog не найден")
def test_recall_detection_low_false_positive_on_real_catalog():
  """
  v2 fix #2: agressивная anaphora-эвристика давала 91 false positive (18.6%)
  на execute-ходах — каждый FP означает пропущенный обязательный tool call.

  Регрессионный порог: false positive rate не должен подняться выше 2%.
  """
  total = false_positives = 0
  for scenario in _CATALOG_SCENARIOS:
    mem = SessionMemory()
    for turn in scenario.get("turns", []):
      is_recall_expected = turn.get("max_tool_calls_delta") == 0
      is_execute_expected = bool(turn.get("expect_tool_called")) and not is_recall_expected
      if is_execute_expected:
        total += 1
        if is_recall_turn(turn["user"], mem):
          false_positives += 1
      parts = [turn["user"]] + [str(x) for x in (turn.get("expect_contains") or [])]
      mem.add(SubtaskHandoff.ok("turn", data={"answer": " ".join(parts)}))

  fp_rate = false_positives / total
  assert fp_rate <= 0.02, f"False positive rate regressed: {false_positives}/{total} = {fp_rate:.1%}"


def test_handoff_carries_actual_tool_response_value():
  """
  v2 fix #3: turn_details раньше несли только name+arguments вызова, без
  самого ответа инструмента — "структурированный факт" был текстом, не
  данными. Теперь tool_call_details должен содержать поле result со
  значением ответа.
  """
  fake_result = TurnResult(
    answer="EMP_ID=HR-ABC123 NAME=Anna Smith DEPT=Engineering",
    tool_calls=["employee_lookup"],
    tool_call_details=[{
      "name": "employee_lookup",
      "arguments": {"name": "Anna Smith"},
      "success": True,
      "result": "EMP_ID=HR-ABC123 NAME=Anna Smith DEPT=Engineering",
      "error": None,
    }],
    rounds=2,
  )
  plan = decompose_request("Найди сотрудника Anna Smith")[0]
  from orchestrator import _build_handoff
  handoff = _build_handoff(plan, fake_result)

  details = handoff.data["tool_call_details"]
  assert details[0]["result"] == "EMP_ID=HR-ABC123 NAME=Anna Smith DEPT=Engineering"
  assert details[0]["success"] is True


def test_recall_session_prompt_allows_general_knowledge():
  """
  v2 fix #4: recall-сессия раньше жёстко требовала "отвечай ТОЛЬКО из
  фактов" — ломало general-knowledge ходы вроде "Что такое блокчейн?".
  Промпт теперь должен разрешать обычный ответ для таких вопросов.
  """
  resources = _make_resources()
  factory = SubagentFactory(resources)
  session = factory.spawn_recall_session()

  prompt = session.resources.system_prompt
  assert "общий вопрос" in prompt or "обычный ассистент" in prompt
  assert "ТОЛЬКО" not in prompt or "СТРОГО" in prompt  # уточнённое условие, не абсолютный запрет


def test_factory_persists_session_per_domain_across_calls():
  """
  v2 fix #5: spawn_session(domain) раньше создавал НОВУЮ сессию (пустая
  история) на каждый вызов — второй HR-ход в диалоге не видел raw-историю
  первого. Теперь сессия на домен кэшируется до explicit reset().
  """
  resources = _make_resources()
  factory = SubagentFactory(resources)

  session1 = factory.spawn_session(DOMAIN_HR)
  session2 = factory.spawn_session(DOMAIN_HR)
  assert session1 is session2  # та же сессия, не пересоздана

  session_crm = factory.spawn_session(DOMAIN_CRM)
  assert session_crm is not session1  # другой домен — другая сессия


def test_factory_reset_clears_cached_sessions():
  """v2 fix #5: SubagentFactory.reset() очищает кэш сессий (новый диалог)."""
  resources = _make_resources()
  factory = SubagentFactory(resources)

  session1 = factory.spawn_session(DOMAIN_HR)
  factory.reset()
  session2 = factory.spawn_session(DOMAIN_HR)
  assert session1 is not session2


def test_orchestrator_reset_clears_factory_sessions():
  """v2 fix #5: Orchestrator.reset() пробрасывает reset в SubagentFactory."""
  resources = _make_resources()
  orchestrator = Orchestrator(resources, model_alias="test")

  session1 = orchestrator._factory.spawn_session(DOMAIN_HR)
  orchestrator.reset()
  session2 = orchestrator._factory.spawn_session(DOMAIN_HR)
  assert session1 is not session2


def test_agent_resources_tool_exec_fail_retries_propagated_to_domain_resources():
  """
  Regression: resources_for()/spawn_recall_session() раньше не передавали
  tool_exec_fail_retries в AgentResources, что ломало создание доменных
  ресурсов (TypeError: missing required positional argument).
  """
  resources = _make_resources()
  factory = SubagentFactory(resources)

  domain_res = factory.resources_for(DOMAIN_HR)
  assert domain_res.tool_exec_fail_retries == resources.tool_exec_fail_retries

  recall_session = factory.spawn_recall_session()
  assert recall_session.resources.tool_exec_fail_retries == resources.tool_exec_fail_retries
