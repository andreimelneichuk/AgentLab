"""Фабрика доменных субагентов с урезанным набором tools.

v2: сессии кэшируются на домен в рамках диалога (persist_session) вместо
создания с нуля на каждый вызов — субагент того же домена видит свою
предыдущую историю внутри одной беседы (не только терпкий JSON handoff).
Recall-сессия остаётся stateless (синтез без tools не требует истории).
"""
from __future__ import annotations

from typing import Dict

from agent_core import AgentResources, BasicLoopSession, tools_to_openai
from subagents.domain_tools import DomainName, filter_tools_for_domain
from subagents.prompts import domain_system_prompt


class SubagentFactory:
  """Создаёт изолированные сессии субагентов по домену MCP."""

  def __init__(self, base_resources: AgentResources, model_alias: str | None = None):
    self._base = base_resources
    self._model_alias = model_alias
    self._domain_resources: Dict[DomainName, AgentResources] = {}
    # v2: кэш живых сессий по домену — persist в рамках одного диалога.
    self._domain_sessions: Dict[DomainName, BasicLoopSession] = {}

  def reset(self) -> None:
    """Сбросить все кэшированные сессии доменов (новый диалог)."""
    self._domain_sessions = {}

  def resources_for(self, domain: DomainName) -> AgentResources:
    """Возвращает AgentResources только с tools домена."""
    if domain not in self._domain_resources:
      tools = filter_tools_for_domain(self._base.tools, domain)
      self._domain_resources[domain] = AgentResources(
        config=self._base.config,
        tools=tools,
        tool_map={tool.name: tool for tool in tools},
        openai_tools=tools_to_openai(tools),
        system_prompt=domain_system_prompt(domain),
        run_limit=self._base.run_limit,
        thread_limit=self._base.thread_limit,
        tool_exec_fail_retries=self._base.tool_exec_fail_retries,
        prompt_path=self._base.prompt_path,
      )
    return self._domain_resources[domain]

  def spawn_session(self, domain: DomainName) -> BasicLoopSession:
    """
    Возвращает сессию субагента домена, кэшированную на диалог.

    v2: раньше создавала новую сессию (пустая история) на КАЖДЫЙ вызов —
    второй HR-ход в том же диалоге не видел raw-историю первого, только
    то, что попало в терпкий JSON handoff. Теперь сессия на домен
    переиспользуется до explicit reset() (новый диалог).
    """
    if domain not in self._domain_sessions:
      self._domain_sessions[domain] = BasicLoopSession(
        self.resources_for(domain), model_alias=self._model_alias
      )
    return self._domain_sessions[domain]

  def spawn_recall_session(self) -> BasicLoopSession:
    """
    Сессия без tools — синтез ответа из накопленных фактов ИЛИ обычный
    ответ для general-knowledge/small-talk ходов.

    v2: раньше промпт жёстко требовал "отвечай ТОЛЬКО из фактов", что
    ломало ходы типа "Что такое блокчейн?" — общий вопрос без привязки
    к prior facts, для которого агент обязан был либо отказаться, либо
    что-то придумать про "факты", хотя корректный ответ — обычные знания.
    """
    empty = AgentResources(
      config=self._base.config,
      tools=[],
      tool_map={},
      openai_tools=[],
      system_prompt=(
        "Ты — субагент синтеза. У тебя нет инструментов. "
        "Если вопрос про факты из предыдущих шагов диалога (structured "
        "handoffs, которые будут переданы в запросе) — отвечай СТРОГО на "
        "основе этих данных, не выдумывай значения полей (ID, коды, суммы), "
        "которых там нет. "
        "Если это общий вопрос, small talk или вопрос не связан с "
        "накопленными фактами — отвечай как обычный ассистент своими "
        "знаниями. Не вызывай tools (у тебя их нет)."
      ),
      run_limit=self._base.run_limit,
      thread_limit=self._base.thread_limit,
      tool_exec_fail_retries=self._base.tool_exec_fail_retries,
      prompt_path=self._base.prompt_path,
    )
    return BasicLoopSession(empty, model_alias=self._model_alias)
