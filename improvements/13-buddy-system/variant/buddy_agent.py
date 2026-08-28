"""Buddy agent: LLM-as-Judge для коррекции дрейфа ответа worker."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

logger = logging.getLogger("buddy_agent")

SECTION_HEADERS: Dict[str, str] = {
    "tools": "СЕКЦИЯ 2. ИНСТРУМЕНТЫ",
    "instructions": "СЕКЦИЯ 3. ПРАВИЛА ПРИНЯТИЯ РЕШЕНИЙ",
    "format": "СЕКЦИЯ 4. ФОРМАТ ВЫВОДА",
}

FALLBACK_HEADERS: Dict[str, str] = {
    "tools": "### РАБОТА С ИНСТРУМЕНТАМИ",
    "instructions": "### ЧЕСТНОСТЬ",
    "format": "### ФОРМАТИРОВАНИЕ",
}

BUDDY_JUDGE_SYSTEM = """Ты — Buddy (напарник-наблюдатель) агента Basic.
Твоя задача — проверить, соответствует ли черновик ответа worker критериям системного промпта.
Дрейф от правил неизбежен в длинных сессиях — лови конкретные нарушения.

Проверяй:
- вызваны ли обязательные инструменты до финального ответа;
- нет ли запрещённых (decoy) инструментов в трейсе;
- соблюдён ли формат вывода (маркеры, списки, теги);
- выполнены ли явные инструкции пользователя из истории диалога;
- **ГЛАВНОЕ — gallucination check:** каждый факт, ID, маркер, число или
  сумма в черновике ОБЯЗАН дословно присутствовать в результатах вызванных
  инструментов (TOOL_RESULT) где-то в трейсе (в этом ходе или в более раннем
  ходе той же сессии). Если worker утверждает конкретное значение
  (EMP_ID=..., CUSTOMER=..., POLICY_FACT=..., сумма, дата, код), которого
  НЕТ ни в одном TOOL_RESULT трейса — это фабрикация (hallucination),
  всегда `pass: false`, даже если формат ответа и порядок инструментов
  выглядят правильно.

Отвечай ТОЛЬКО валидным JSON без markdown:
{"pass": true}
или
{"pass": false, "feedback": "конкретное исправление для worker", "failed_criteria": ["criterion_id"]}
"""

# --- Tier-1 (дешёвый, без LLM) гейт для решения "нужен ли дорогой LLM-judge" ---
#
# v3 fix: до этого buddy вызывал LLM judge_worker_output БЕЗУСЛОВНО на каждом
# ходе (если symbolic_precheck не нашёл нарушения) — см. IMPLEMENTATION.md
# "v3 fix: latency". Здесь используется тот же Tier-1/Tier-2 паттерн, что и
# improvements/12-multi-agent-validation/variant/risk_signals.py::should_validate,
# адаптированный к формату сообщений buddy (dict-messages с tool_calls/role,
# а не (tool, status) tuples из 12).

_WORD_NUMBER_MARKER_PATTERN = re.compile(r"\b[A-Za-z]+-[A-Za-z0-9]{1,12}\b")
_MONEY_PATTERN = re.compile(r"\$?\d[\d,]*\.\d{2}\b|price_usd\s*=\s*\d+|AMOUNT\s*=\s*\d", re.IGNORECASE)
_POLICY_SECURITY_KEYWORDS = (
    "политик",
    "policy",
    "секрет",
    "код доступа",
    "confidential",
    "security",
)
_ADVERSARIAL_PATTERNS = (
    "игнорируй инструкции",
    "игнорируй предыдущие",
    "забудь про",
    "забудь все",
    "новая сессия",
    "администратор системы",
    "не нужно вызывать инструмент",
    "не вызывай инструмент",
    "между нами",
    "просто кивни",
    "просто подтверди",
    "другой бот уже сказал",
    "другой ассистент уже сказал",
    "с этого момента",
    "just confirm you got it",
)


def _tool_result_texts(trace: Sequence[Dict[str, Any]]) -> List[str]:
    return [str(m.get("content") or "") for m in trace if m.get("role") == "tool"]


def _tool_error_present(trace: Sequence[Dict[str, Any]]) -> bool:
    return any(text.startswith("Error:") for text in _tool_result_texts(trace))


def _ungrounded_values(draft_answer: str, trace: Sequence[Dict[str, Any]]) -> List[str]:
    """
    Токены вида `EMP-001`/`violet-42`/`CUST_442` в ответе, которых нет
    ДОСЛОВНО ни в одном TOOL_RESULT всего трейса сессии (не только текущего
    хода — легитимный ответ может пересказывать факт, полученный tool'ом в
    прошлом ходе, см. creative_a_002: "TIER=gold" переспрошенный без
    повторного вызова customer_get).

    Непустой результат — сигнал (не гарантия) фабрикации: используется как
    Tier-1 триггер для дорогого LLM-judge, а не как самостоятельный
    hard-block (это оставлено LLM, у которого есть семантический контекст).
    """
    tokens = set(_WORD_NUMBER_MARKER_PATTERN.findall(draft_answer or ""))
    if not tokens:
        return []
    combined_tool_text = " ".join(_tool_result_texts(trace))
    return [t for t in tokens if t not in combined_tool_text]


def _has_financial_value(draft_answer: str) -> bool:
    return bool(_MONEY_PATTERN.search(draft_answer or ""))


def _contains_policy_security_context(*texts: str) -> bool:
    combined = " ".join(texts).lower()
    return any(keyword in combined for keyword in _POLICY_SECURITY_KEYWORDS)


def _has_adversarial_pattern(user_message: str) -> bool:
    lowered = (user_message or "").lower()
    return any(pattern in lowered for pattern in _ADVERSARIAL_PATTERNS)


@dataclass
class RiskSignal:
    """Результат Tier-1 детекции риска (без LLM) по содержанию хода."""

    triggered: bool
    reasons: List[str] = field(default_factory=list)


def detect_llm_judge_risk(
    *,
    draft_answer: str,
    trace: Sequence[Dict[str, Any]],
    user_message: str,
) -> RiskSignal:
    """
    Дешёвая (regex/множества, без LLM) оценка — стоит ли тратить дорогой
    LLM-вызов `judge_worker_output` на этот ход.

    `trace` — ПОЛНАЯ история сессии (не per-turn) специально для
    `_ungrounded_values`: маркер, добытый tool'ом в прошлом ходе и
    процитированный сейчас — не фабрикация (см. docstring выше).
    """
    reasons: List[str] = []

    ungrounded = _ungrounded_values(draft_answer, trace)
    if ungrounded:
        reasons.append(f"ungrounded_values:{','.join(sorted(ungrounded)[:5])}")

    if _tool_error_present(trace):
        reasons.append("tool_error_present")

    if _has_financial_value(draft_answer):
        reasons.append("financial_value_present")

    if _contains_policy_security_context(user_message, draft_answer):
        reasons.append("policy_context")

    if _has_adversarial_pattern(user_message):
        reasons.append("adversarial_injection_pattern")

    return RiskSignal(triggered=bool(reasons), reasons=reasons)


@dataclass
class Guide:
    """Обратная связь для повторной генерации worker."""

    feedback: str
    failed_criteria: List[str] = field(default_factory=list)


@dataclass
class Criterion:
    """Один критерий проверки Buddy."""

    id: str
    category: str
    description: str
    severity: str = "normal"


@dataclass
class CriteriaRegistry:
    """Реестр критериев, извлечённых из секций system prompt."""

    tools_rules: List[Criterion] = field(default_factory=list)
    format_rules: List[Criterion] = field(default_factory=list)
    instruction_rules: List[Criterion] = field(default_factory=list)
    forbidden_tools: List[str] = field(default_factory=list)
    required_markers: List[str] = field(default_factory=list)
    raw_sections: Dict[str, str] = field(default_factory=dict)

    def all_criteria(self) -> List[Criterion]:
        return [*self.tools_rules, *self.format_rules, *self.instruction_rules]

    def to_judge_context(self) -> str:
        lines: List[str] = []
        for category, rules in (
            ("tools", self.tools_rules),
            ("format", self.format_rules),
            ("instructions", self.instruction_rules),
        ):
            if not rules:
                continue
            lines.append(f"## {category}")
            for rule in rules:
                lines.append(f"- [{rule.id}] ({rule.severity}) {rule.description}")
        if self.forbidden_tools:
            lines.append("## forbidden_tools")
            lines.append(", ".join(self.forbidden_tools))
        if self.required_markers:
            lines.append("## required_markers")
            lines.append(", ".join(self.required_markers))
        return "\n".join(lines)


def _split_sections(system_prompt: str) -> Dict[str, str]:
    """Разбивает промпт на секции tools / instructions / format."""
    sections: Dict[str, str] = {}
    for key, header in SECTION_HEADERS.items():
        start = system_prompt.find(header)
        if start < 0:
            continue
        body_start = start + len(header)
        end = len(system_prompt)
        for other_key, other_header in SECTION_HEADERS.items():
            if other_key == key:
                continue
            pos = system_prompt.find(other_header, body_start)
            if pos >= 0:
                end = min(end, pos)
        sections[key] = system_prompt[body_start:end].strip()

    if sections:
        return sections

    for key, header in FALLBACK_HEADERS.items():
        start = system_prompt.find(header)
        if start < 0:
            continue
        body_start = start + len(header)
        next_header = re.search(r"\n### ", system_prompt[body_start:])
        end = body_start + next_header.start() if next_header else len(system_prompt)
        sections[key] = system_prompt[body_start:end].strip()
    return sections


def _extract_forbidden_tools(tools_section: str) -> List[str]:
    forbidden: List[str] = []
    for match in re.finditer(r"\*\*(decoy_\w+)\*\*", tools_section):
        forbidden.append(match.group(1))
    for match in re.finditer(r"НЕ использовать[:\s]+`([^`]+)`", tools_section):
        name = match.group(1).strip()
        if name.startswith("decoy_") and name not in forbidden:
            forbidden.append(name)
    return forbidden


def _extract_markers(format_section: str) -> List[str]:
    markers: List[str] = []
    for match in re.finditer(r"`(\[[A-Z_]+\])`", format_section):
        marker = match.group(1)
        if marker not in markers:
            markers.append(marker)
    return markers


def _rules_from_section(category: str, section_text: str) -> List[Criterion]:
    rules: List[Criterion] = []
    for idx, line in enumerate(section_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("{%"):
            continue
        if stripped.startswith(("-", "*")) or re.match(r"^\d+\.", stripped):
            rules.append(Criterion(
                id=f"{category}_{idx}",
                category=category,
                description=stripped.lstrip("-* ").strip(),
                severity="critical" if "decoy" in stripped.lower() or "stop" in stripped.lower() else "normal",
            ))
        elif stripped.startswith("**") and "Назначение" in stripped:
            tool_name = stripped.strip("*").split()[0]
            rules.append(Criterion(
                id=f"{category}_tool_{tool_name}",
                category=category,
                description=stripped,
                severity="critical" if tool_name.startswith("decoy_") else "normal",
            ))
    return rules


def build_criteria_registry(system_prompt: str, config: Optional[Dict[str, Any]] = None) -> CriteriaRegistry:
    """
    Строит реестр критериев из секций system prompt (tools, format, instructions).

    Args:
        system_prompt: отрендеренный системный промпт worker
        config: опциональный конфиг с дополнительными критериями buddy.criteria

    Returns:
        CriteriaRegistry с правилами для judge
    """
    sections = _split_sections(system_prompt)
    tools_text = sections.get("tools", "")
    format_text = sections.get("format", "")
    instructions_text = sections.get("instructions", "")

    registry = CriteriaRegistry(
        tools_rules=_rules_from_section("tools", tools_text),
        format_rules=_rules_from_section("format", format_text),
        instruction_rules=_rules_from_section("instructions", instructions_text),
        forbidden_tools=_extract_forbidden_tools(tools_text),
        required_markers=_extract_markers(format_text),
        raw_sections=sections,
    )

    extra = ((config or {}).get("buddy") or {}).get("extra_criteria") or []
    for item in extra:
        registry.instruction_rules.append(Criterion(
            id=str(item.get("id", f"extra_{len(registry.instruction_rules)}")),
            category=str(item.get("category", "instructions")),
            description=str(item.get("description", "")),
            severity=str(item.get("severity", "normal")),
        ))
    return registry


def _tool_names_from_trace(trace: Sequence[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for msg in trace:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            name = tc.get("name", "")
            if name:
                names.append(name)
    return names


def _conversation_user_text(trace: Sequence[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for msg in trace:
        if msg.get("role") == "user":
            parts.append(str(msg.get("content") or ""))
    return "\n".join(parts)


def symbolic_precheck(
    *,
    draft_answer: str,
    trace: Sequence[Dict[str, Any]],
    criteria: CriteriaRegistry,
    user_message: str,
    current_turn_trace: Optional[Sequence[Dict[str, Any]]] = None,
) -> Optional[Guide]:
    """
    Быстрая символическая проверка без LLM.

    Args:
        trace: полная история сессии (все ходы) — используется ТОЛЬКО для
            обнаружения явно установленного пользователем персистентного
            правила диалога (например "каждый ответ заканчивается [POLICY_OK]").
        current_turn_trace: трейс ТОЛЬКО текущего хода (включая retry-попытки
            buddy внутри него, но не прошлые ходы диалога). Используется для
            проверок вида "вызван ли tool В ЭТОМ ходе" — если не передан,
            по умолчанию равен `trace` (обратная совместимость).

    v2 fix: раньше "get_policy_fact в trace → нужен [POLICY_OK]" и
    "forbidden tool в trace" проверялись по ВСЕЙ истории сессии — значит
    get_policy_fact, вызванный в ходе 1, требовал [POLICY_OK] в ЛЮБОМ
    последующем несвязанном ходе (например про погоду). На реальном
    каталоге это ломало 197/753 хода (26%) — каждое такое срабатывание
    добавляло лишний LLM-вызов judge (латентность) и портило корректный
    ответ навязанным тегом. Теперь эти проверки — per-turn.

    Returns:
        Guide при нарушении или None если проверка пройдена
    """
    scoped_trace = current_turn_trace if current_turn_trace is not None else trace

    tool_names = _tool_names_from_trace(scoped_trace)
    for forbidden in criteria.forbidden_tools:
        if forbidden in tool_names:
            return Guide(
                feedback=(
                    f"В трейсе вызван запрещённый инструмент `{forbidden}`. "
                    f"Используй canonical-инструмент из Секции 2 и повтори ответ."
                ),
                failed_criteria=[f"forbidden_tool:{forbidden}"],
            )

    if "get_policy_fact" in tool_names and "[POLICY_OK]" not in draft_answer:
        return Guide(
            feedback="После `get_policy_fact` финальный ответ обязан содержать тег `[POLICY_OK]`.",
            failed_criteria=["format_policy_ok"],
        )

    # Персистентное правило диалога ("каждый ответ заканчивается [POLICY_OK]")
    # — здесь ОСОЗНАННО используется полная история (не scoped_trace), т.к.
    # такое правило действительно должно применяться ко ВСЕМ последующим ходам.
    history_text = _conversation_user_text(trace)
    if "[POLICY_OK]" in history_text or "[POLICY_OK]" in user_message:
        if "[POLICY_OK]" not in draft_answer:
            return Guide(
                feedback="Пользователь требует `[POLICY_OK]` в ответе — добавь тег.",
                failed_criteria=["format_policy_ok_user"],
            )

    for marker in criteria.required_markers:
        if marker in history_text and marker not in draft_answer:
            return Guide(
                feedback=f"В диалоге задано правило с маркером `{marker}` — включи его в ответ.",
                failed_criteria=[f"format_marker:{marker}"],
            )

    return None


def _format_trace(trace: Sequence[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for msg in trace:
        role = msg.get("role", "")
        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg.get("tool_calls") or []:
                lines.append(f"TOOL_CALL {tc.get('name')}: {json.dumps(tc.get('arguments') or {}, ensure_ascii=False)}")
        elif role == "tool":
            content = str(msg.get("content") or "")
            lines.append(f"TOOL_RESULT: {content[:500]}")
        elif role in ("user", "assistant"):
            content = str(msg.get("content") or "")
            if content:
                lines.append(f"{role.upper()}: {content[:800]}")
    return "\n".join(lines) or "(пустой трейс)"


def _parse_judge_response(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    logger.warning("Buddy judge: не удалось распарсить JSON: %s", text[:200])
    return {"pass": True}


def format_guide_message(guide: Guide) -> str:
    """Форматирует Guide для повторной генерации worker."""
    criteria = ", ".join(guide.failed_criteria) if guide.failed_criteria else "general"
    return (
        "[BUDDY_GUIDE] Предыдущий ответ не прошёл проверку напарника "
        f"(критерии: {criteria}).\n"
        f"Исправь и сгенерируй новый ответ:\n{guide.feedback}"
    )


async def judge_worker_output(
    llm: ChatOpenAI,
    *,
    user_message: str,
    worker_trace: Sequence[Dict[str, Any]],
    draft_answer: str,
    criteria: CriteriaRegistry,
    enable_symbolic_precheck: bool = True,
    symbolic_only: bool = False,
    current_turn_trace: Optional[Sequence[Dict[str, Any]]] = None,
    enable_llm_gate: bool = True,
) -> Optional[Guide]:
    """
    LLM-as-Judge: проверяет output worker и возвращает Guide или pass (None).

    Args:
        llm: модель для judge (может совпадать с worker)
        user_message: текущее сообщение пользователя
        worker_trace: полная история сессии (все ходы) — контекст для LLM
            и для detection персистентных правил диалога в symbolic_precheck
        draft_answer: черновик финального ответа worker
        criteria: реестр критериев из system prompt
        enable_symbolic_precheck: быстрая проверка до LLM
        symbolic_only: только символическая проверка (для тестов)
        current_turn_trace: трейс ТОЛЬКО текущего хода — для "tool вызван в
            этом ходе" проверок symbolic_precheck (см. docstring там). Если
            не передан, используется worker_trace целиком (обратная
            совместимость со старым поведением).
        enable_llm_gate: Tier-1 гейт (см. `detect_llm_judge_risk`) — если
            True (по умолчанию) и symbolic_precheck прошёл, дорогой LLM
            judge вызывается ТОЛЬКО когда дешёвая эвристика находит сигнал
            риска (незаземлённый маркер/сумма/ошибка tool/policy-контекст/
            adversarial-паттерн). v3 fix: раньше LLM вызывался безусловно
            на каждом ходе — это и была причина 4.38s латентности (см.
            IMPLEMENTATION.md "v3 fix: latency").

    Returns:
        Guide с feedback для retry или None если проверка пройдена
    """
    if enable_symbolic_precheck:
        precheck = symbolic_precheck(
            draft_answer=draft_answer,
            trace=worker_trace,
            criteria=criteria,
            user_message=user_message,
            current_turn_trace=current_turn_trace,
        )
        if precheck is not None:
            return precheck

    if symbolic_only:
        return None

    if enable_llm_gate:
        risk = detect_llm_judge_risk(
            draft_answer=draft_answer,
            trace=worker_trace,
            user_message=user_message,
        )
        if not risk.triggered:
            return None
        logger.debug("Buddy Tier-1 gate triggered LLM judge: %s", risk.reasons)

    judge_user = (
        f"Критерии:\n{criteria.to_judge_context()}\n\n"
        f"Сообщение пользователя:\n{user_message}\n\n"
        f"Трейс worker:\n{_format_trace(worker_trace)}\n\n"
        f"Черновик ответа:\n{draft_answer}"
    )
    result = await llm.ainvoke([
        SystemMessage(content=BUDDY_JUDGE_SYSTEM),
        HumanMessage(content=judge_user),
    ])
    parsed = _parse_judge_response(str(result.content or ""))
    if parsed.get("pass") is True:
        return None

    feedback = str(parsed.get("feedback") or "Ответ не соответствует критериям. Исправь нарушения.")
    failed = [str(x) for x in (parsed.get("failed_criteria") or [])]
    return Guide(feedback=feedback, failed_criteria=failed)
