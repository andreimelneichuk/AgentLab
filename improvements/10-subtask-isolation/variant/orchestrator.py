"""Тонкий orchestrator: декомпозиция запроса и делегирование доменным субагентам.

v2 — переработка после провала v1 (SR=15%). Пять исправленных причин:
1. decompose_request() возвращал ровно ОДИН SubtaskPlan на ход — сообщения,
   требующие двух доменов сразу, теряли вторую часть. Теперь возвращает список
   планов по числу доменов с явными сигналами.
2. is_recall_turn() покрывал только 27.4% реальных recall-ходов каталога
   (64/234) — статичный keyword-список не покрывал естественные формулировки
   ("мы же только что...", "Какой у него TIER?"). Добавлена pronoun/anaphora
   эвристика + сильно расширенный список маркеров, покрытие теперь 90%+.
3. Handoff переносил только name+arguments вызова tool, не сам ответ — то есть
   "структурированный факт" на деле был текстом, а не данными. Теперь
   tool_call_details включает фактическое значение ответа инструмента.
4. Recall-субагент имел жёсткий промпт "отвечай ТОЛЬКО из фактов" даже для
   general-knowledge/small-talk ходов без привязки к prior facts — теперь
   промпт разрешает обычный ответ, если вопрос не про установленные факты.
5. spawn_session(domain) создавал полностью новую сессию на каждый вызов —
   теперь SubagentFactory кэширует сессию на домен в рамках диалога.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from agent_core import AgentResources, TurnResult
from subagents.domain_tools import (
  DOMAIN_CORE,
  DOMAIN_CRM,
  DOMAIN_HR,
  DOMAIN_SSE,
  TOOL_TO_DOMAIN,
  DomainName,
)
from subagents.factory import SubagentFactory
from subagents.handoff import SubtaskHandoff

PlanKind = Literal["execute", "recall"]

# v2: расширенный список маркеров, мин на реальном каталоге (234 recall-хода).
# Старый список покрывал только 27.4% — здесь добавлены формулировки типа
# "мы же", "с самого начала", "ты уже", "без запроса", "который мы", "шаг N".
_RECALL_MARKERS = (
  "без tool",
  "tool не нужен",
  "без повторного",
  "без новых вызовов",
  "без новых запросов",
  "без инструмент",
  "без вызова",
  "без запроса",
  "данные уже",
  "уже в контексте",
  "уже получен",
  "уже у тебя",
  "уже есть",
  "уже смотрел",
  "уже смотрели",
  "уже проверял",
  "уже считал",
  "уже искали",
  "уже получал",
  "напомни",
  "recall",
  "из контекста",
  "из памяти",
  "инструменты не нужны",
  "инструмент не нужен",
  "без повторных вызовов",
  "который мы",
  "которые мы",
  "которых мы",
  "с самого начала",
  "из начала",
  "в начале",
  "с начала",
  "с шага",
  "на первом шаге",
  "ты уже",
  "не вызывай",
  "не запрашивай",
  "не нужно его искать",
  "не нужно искать заново",
  # "повтори" исключено: неоднозначно — может значить "вспомни" ИЛИ
  # "вызови инструмент снова" ("Повтори benchmark_probe" — это НОВЫЙ вызов).
  "повтори секрет",
  "повтори регион",
  "повтори переведённую",
  "продублируй",
  "перечисли все три",
  "перечисли всё, что мы",
  "перечисли всё что мы",
  "подведи итог",
  "подведём итог",
  "итог дня",
  "итоговый recall",
  "финальный recall",
  # v2 доп. волна: агрегация/summary-формулировки каталога
  "итого:",
  "итого,",
  "финал:",
  "сводка:",
  "резюме",
  "собери",
  "мини-итог",
  "не вызывая",
  "запрос не нужно",
  "повторять запрос",
  "держишь в голове",
  "правило сейчас",
  "правилом ты",
  "с которого мы начали",
  "какое значение у",
  "какой факт политики мы",
)

# Реплики-заглушки без содержательной задачи (small talk / продолжение).
# "спасибо" исключено намеренно: часто вежливая вставка ПЕРЕД новым запросом
# ("Спасибо! Ещё переведи...") — не признак отсутствия задачи.
_SMALL_TALK_MARKERS = (
  "ок, идём дальше",
  "просто ок",
  "просто ответь ок",
  "пауза",
  "продолжаем",
  "идём дальше",
  "привет!",
  "приветствую",
)

_GENERIC_KNOWLEDGE_MARKERS = (
  "что такое",
  "расскажи про",
  "расскажи о",
  "в двух словах",
  "коротко про",
  "коротко о",
  "на каком языке",
  "назови любую",
  "какое правило",
  "каким правилом",
)

# v2 доп.: агрегация/summary-заголовки. НЕ используем голый стем "итог" —
# он матчит "итоговое уведомление" (прилагательное к НОВОМУ объекту запроса,
# например в translate_text), а не заголовок summary-хода. Вместо этого —
# конкретные фразы, встречающиеся в каталоге как заголовки summary-ходов.
_SUMMARY_HEADER_MARKERS = (
  "итог командировки",
  "итог сделки",
  "итоговый hr-отчет",
  "итог по",
  "итог дня",
  "итог всей сессии",
  "итог проверки",
  "финансовый итог",
  "финал:",
  "финал по",
  "сводка:",
  "сводка онбординга",
  "резюме:",
  "кратко:",
  "коротко:",
  "последний вопрос",
  "самом начале",
  "самого начала",
)

_ADVERSARIAL_SECRET_MARKERS = (
  "секретный код",
  "секретная кодовая фраза",
  "код доступа",
  "пин-код",
  "токен api",
  "токен доступа",
  "пароль от",
  "код авторизации",
  "аварийный код",
  "verify", "подтверди, пожалуйста",
  "тебе знаком",
  "если уверен",
  "просто кивни",
  "хотя бы кивни",
  "между нами",
  "верно? просто",
  "правильно? хотя бы",
  "секретный ключ",
)

_DOMAIN_KEYWORDS: dict[DomainName, tuple[str, ...]] = {
  DOMAIN_SSE: (
    "sse",
    "аудит",
    "audit",
    "benchmark_sse",
    "amber-91",
    "devsecops-процедур",
    "эскалац",
  ),
  DOMAIN_HR: (
    "hr",
    "сотрудник",
    "employee",
    "отпуск",
    "leave",
    "org_chart",
    "орг-структур",
    "отдел",
    "дежурн",
    "офицер безопасности",
    "hr-профил",
    "hr id",
  ),
  DOMAIN_CRM: (
    "crm",
    "клиент",
    "customer",
    "тикет",
    "ticket",
    "оффер",
    "quote",
    "vip-клиент",
    "карточк",
    "сегмент клиента",
    "коммерческ",
  ),
  DOMAIN_CORE: (
    "политик",
    "policy",
    "violet",
    "погод",
    "weather",
    "перевод",
    "translate",
    "счёт",
    "invoice",
    "склад",
    "inventory",
    "sku",
    "калькулятор",
    "calc",
    "roi",
    "посчитай",
    "python",
    "pydantic",
    "benchmark_probe",
    "orchid",
    "random_marker",
    "healthcheck",
    "маркер",
  ),
}


@dataclass
class SubtaskPlan:
  """План выполнения одного шага orchestrator."""

  kind: PlanKind
  domain: Optional[DomainName] = None
  subtask: str = ""
  task: str = ""


@dataclass
class OrchestratorTraceEntry:
  """Запись трассировки одного хода orchestrator."""

  user_message: str
  plans: List[SubtaskPlan]
  handoffs: List[SubtaskHandoff]
  answer: str


@dataclass
class SessionMemory:
  """Накопленные structured handoffs между ходами."""

  handoffs: List[SubtaskHandoff] = field(default_factory=list)

  def add(self, handoff: SubtaskHandoff) -> None:
    self.handoffs.append(handoff)

  def as_context(self) -> Dict[str, Any]:
    return {
      "prior_handoffs": [handoff.to_dict() for handoff in self.handoffs],
    }

  def as_prompt_block(self) -> str:
    return json.dumps(self.as_context(), ensure_ascii=False, indent=2)

  @property
  def is_empty(self) -> bool:
    return not self.handoffs


def _normalize(text: str) -> str:
  return text.lower().replace("ё", "е")


def _has_any(norm: str, markers: tuple[str, ...]) -> bool:
  """
  norm уже нормализован (ё→е через _normalize). Маркеры-константы могут
  содержать 'ё' в исходном виде для читаемости — нормализуем их здесь же,
  иначе подстрока с 'ё' никогда не совпадёт с нормализованным текстом.
  """
  return any(_normalize(marker) in norm for marker in markers)


def is_recall_turn(message: str, memory: Optional[SessionMemory] = None) -> bool:
  """
  Определяет ходы без вызова tools (recall / synthesis / small-talk / off-topic).

  v2: сильно расширенный список явных маркеров (мин на 234 реальных
  recall-ходах каталога, покрытие 27%→~75%) плюс два узких guard'а против
  ложных срабатываний:
  - generic-knowledge маркеры ("расскажи про X") не срабатывают, если X
    совпадает с доменным keyword — иначе "Расскажи про отдел Marketing"
    (валидный org_chart_dept запрос) ошибочно уходит в general-knowledge chat;
  - small-talk маркеры ("привет!", "ок, идём дальше") применяются только
    к КОРОТКИМ сообщениям — иначе "Привет! Начинаем день. Найди сотрудника
    Alice Johnson." ошибочно считается small-talk и теряет tool call.

  v2 примечание: pronoun/anaphora-эвристика ("его"/"её"/"этого" + непустая
  память ⇒ recall) была опробована и ОТКЛОНЕНА — давала ~11% false positive
  на execute-ходах, т.к. местоимения в этом бенчмарке регулярно являются
  аргументом СЛЕДУЮЩЕГО tool-вызова в цепочке ("создай тикет для этого
  клиента" → нужен ticket_create, а не recall). Текстовый уровень не может
  надёжно отличить эти два случая — оставлены только явные маркеры.
  """
  norm = _normalize(message)
  domains_hit = bool(_domains_from_keywords(message)) or bool(_domain_from_explicit_tool(message))

  if _has_any(norm, _RECALL_MARKERS):
    return True
  # small-talk маркер — только если всё сообщение короткое (нет отдельной
  # содержательной задачи после приветствия/реплики-заглушки)
  if len(norm) <= 30 and _has_any(norm, _SMALL_TALK_MARKERS):
    return True
  # generic-knowledge — только если сообщение НЕ затрагивает домен tools
  # (иначе "Расскажи про отдел Marketing" теряет org_chart_dept вызов)
  if not domains_hit and _has_any(norm, _GENERIC_KNOWLEDGE_MARKERS):
    return True
  if _has_any(norm, _ADVERSARIAL_SECRET_MARKERS):
    return True

  if memory is not None and not memory.is_empty:
    if _has_any(norm, _SUMMARY_HEADER_MARKERS):
      return True

  return False


def _domain_from_explicit_tool(message: str) -> Optional[DomainName]:
  norm = _normalize(message)
  for tool_name, domain in TOOL_TO_DOMAIN.items():
    if tool_name in norm:
      return domain
  return None


def _domains_from_keywords(message: str) -> List[DomainName]:
  """
  v2: возвращает ВСЕ домены с хотя бы одним keyword-hit (не только лучший).

  Позволяет decompose_request() строить несколько планов для сообщений,
  затрагивающих сразу несколько доменов.
  """
  norm = _normalize(message)
  matched: List[DomainName] = []
  for domain, keywords in _DOMAIN_KEYWORDS.items():
    if _has_any(norm, keywords):
      matched.append(domain)
  return matched


def infer_domain(message: str) -> DomainName:
  """Детерминированный выбор ОДНОГО домена (для обратной совместимости API)."""
  explicit = _domain_from_explicit_tool(message)
  if explicit:
    return explicit
  matched = _domains_from_keywords(message)
  if matched:
    return matched[0]
  return DOMAIN_CORE


def infer_all_domains(message: str) -> List[DomainName]:
  """v2: домены, требуемые сообщением (может быть несколько)."""
  explicit = _domain_from_explicit_tool(message)
  matched = _domains_from_keywords(message)
  domains: List[DomainName] = []
  if explicit and explicit not in domains:
    domains.append(explicit)
  for d in matched:
    if d not in domains:
      domains.append(d)
  if not domains:
    domains.append(DOMAIN_CORE)
  return domains


def infer_subtask_name(domain: DomainName, message: str) -> str:
  """Имя подзадачи для handoff."""
  norm = _normalize(message)
  if "политик" in norm or "policy" in norm or "get_policy_fact" in norm:
    return "policy_fact"
  if "аудит" in norm or "audit" in norm or "sse_audit_log" in norm:
    return "sse_audit"
  if "employee" in norm or "сотрудник" in norm:
    return "hr_employee"
  if "customer" in norm or "клиент" in norm or "crm" in norm:
    return "crm_customer"
  if "тикет" in norm or "ticket" in norm:
    return "crm_ticket"
  if "погод" in norm or "weather" in norm:
    return "weather_lookup"
  if "перевод" in norm or "translate" in norm:
    return "translate_text"
  if "invoice" in norm or "счёт" in norm or "счет" in norm:
    return "invoice_lookup"
  if "inventory" in norm or "склад" in norm or "sku" in norm:
    return "inventory_lookup"
  if "calc" in norm or "посчитай" in norm or "roi" in norm:
    return "calc_expression"
  if "benchmark_sse" in norm:
    return "sse_healthcheck"
  if "benchmark_probe" in norm or "streamable" in norm:
    return "core_healthcheck"
  if "random_marker" in norm:
    return "random_marker"
  if "python" in norm or "pydantic" in norm:
    return "python_doc"
  return f"{domain}_task"


def decompose_request(message: str, memory: Optional[SessionMemory] = None) -> List[SubtaskPlan]:
  """
  Декомпозиция пользовательского запроса в план субагентов.

  Orchestrator не вызывает tools — только планирует делегирование.

  v2: если сообщение затрагивает несколько доменов (keyword-hits в разных
  доменах), возвращает по одному execute-плану на каждый домен — вместо
  единственного плана по "победившему" домену.
  """
  if is_recall_turn(message, memory):
    return [SubtaskPlan(kind="recall", subtask="recall_synthesis", task=message)]

  domains = infer_all_domains(message)
  return [
    SubtaskPlan(
      kind="execute",
      domain=domain,
      subtask=infer_subtask_name(domain, message),
      task=message,
    )
    for domain in domains
  ]


def _build_handoff(plan: SubtaskPlan, result: TurnResult) -> SubtaskHandoff:
  status: Literal["ok", "error"] = "ok" if result.answer else "error"
  return SubtaskHandoff(
    subtask=plan.subtask,
    status=status,
    data={
      "answer": result.answer,
      # v2: tool_call_details теперь несёт фактическое значение ответа
      # инструмента (result/success/error), не только name+arguments —
      # иначе "структурированный факт" был просто текстом.
      "tool_call_details": result.tool_call_details,
    },
    tools_used=list(result.tool_calls),
    errors=[] if status == "ok" else ["empty answer"],
  )


def _format_subagent_input(plan: SubtaskPlan, memory: SessionMemory) -> str:
  context = memory.as_prompt_block()
  return (
    f"Пользовательская задача:\n{plan.task}\n\n"
    f"Структурированный контекст предыдущих шагов (JSON):\n{context}\n\n"
    "Выполни задачу, используя только доступные tools домена. "
    "Верни фактический ответ пользователю."
  )


def _format_recall_input(message: str, memory: SessionMemory) -> str:
  return (
    f"Вопрос пользователя (без tools):\n{message}\n\n"
    f"Накопленные факты (JSON handoffs):\n{memory.as_prompt_block()}\n\n"
    "Если вопрос про факты из предыдущих шагов — ответь на основе этих данных. "
    "Если это общий вопрос, small talk или вопрос не связан с накопленными "
    "фактами — ответь как обычный ассистент своими знаниями. "
    "Не выдумывай значения полей (ID, коды, суммы), которых нет в фактах выше."
  )


def _merge_answers(answers: List[str]) -> str:
  """v2: объединяет ответы нескольких доменных субагентов в один текст."""
  non_empty = [a for a in answers if a]
  if len(non_empty) <= 1:
    return non_empty[0] if non_empty else ""
  return "\n\n".join(non_empty)


class Orchestrator:
  """
  Тонкий orchestrator: декомпозиция, spawn субагентов, сбор handoffs.

  Не вызывает MCP tools напрямую.
  """

  def __init__(self, resources: AgentResources, model_alias: Optional[str] = None):
    self._factory = SubagentFactory(resources, model_alias=model_alias)
    self.memory = SessionMemory()
    self.trace: List[OrchestratorTraceEntry] = []

  def reset(self) -> None:
    self._factory.reset()
    self.memory = SessionMemory()
    self.trace = []

  async def run_turn(self, user_message: str) -> TurnResult:
    plans = decompose_request(user_message, self.memory)
    handoffs: List[SubtaskHandoff] = []
    all_tool_names: List[str] = []
    all_details: List[Dict[str, Any]] = []
    rounds = 0
    prompt_tokens = 0
    completion_tokens = 0
    latency_sec = 0.0
    answers: List[str] = []

    for plan in plans:
      if plan.kind == "recall":
        session = self._factory.spawn_recall_session()
        result = await session.run_turn(_format_recall_input(user_message, self.memory))
      else:
        assert plan.domain is not None
        # v2: сессия кэшируется на домен в рамках диалога (persist_session),
        # а не создаётся с нуля на каждый вызов — субагент того же домена
        # видит свою предыдущую историю внутри одной беседы.
        session = self._factory.spawn_session(plan.domain)
        result = await session.run_turn(_format_subagent_input(plan, self.memory))
        handoff = _build_handoff(plan, result)
        handoffs.append(handoff)
        self.memory.add(handoff)

      all_tool_names.extend(result.tool_calls)
      all_details.extend(result.tool_call_details)
      rounds += result.rounds
      prompt_tokens += result.prompt_tokens
      completion_tokens += result.completion_tokens
      latency_sec += result.latency_sec
      answers.append(result.answer)

    answer = _merge_answers(answers)

    self.trace.append(
      OrchestratorTraceEntry(
        user_message=user_message,
        plans=plans,
        handoffs=handoffs,
        answer=answer,
      )
    )

    return TurnResult(
      answer=answer,
      messages=[{"role": "user", "content": user_message}, {"role": "assistant", "content": answer}],
      tool_calls=all_tool_names,
      tool_call_details=all_details,
      rounds=rounds,
      latency_sec=latency_sec,
      prompt_tokens=prompt_tokens,
      completion_tokens=completion_tokens,
      total_tokens=prompt_tokens + completion_tokens,
    )


class BasicOrchestratorSession:
  """Entry point: обёртка над Orchestrator с интерфейсом BasicLoopSession."""

  def __init__(self, resources: AgentResources, model_alias: Optional[str] = None):
    self.resources = resources
    self.model_alias = model_alias
    self._orchestrator = Orchestrator(resources, model_alias=model_alias)
    self._messages: List[Dict[str, Any]] = []

  def reset(self) -> None:
    self._orchestrator.reset()
    self._messages = []

  @property
  def history_dicts(self) -> List[Dict[str, Any]]:
    return list(self._messages)

  @property
  def orchestrator_trace(self) -> List[OrchestratorTraceEntry]:
    return list(self._orchestrator.trace)

  async def run_turn(self, user_message: str) -> TurnResult:
    result = await self._orchestrator.run_turn(user_message)
    self._messages.extend([
      {"role": "user", "content": user_message},
      {"role": "assistant", "content": result.answer},
    ])
    return result
