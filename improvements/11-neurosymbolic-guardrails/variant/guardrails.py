"""Нейросимволические guardrails: бизнес-правила ниже уровня LLM."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

logger = logging.getLogger("guardrails")

RULES_DIR = Path(__file__).resolve().parent / "rules"
GUARDRAIL_ERROR_PREFIX = "GUARDRAIL_BLOCKED"

# Тот же паттерн, что benchmark/scoring.py::REFUSAL_RE использует для
# отличения "выдал маркер за факт" от "упомянул маркер при явном отказе"
# (см. _marker_claimed_as_fact). require_marker_traceable использует ту же
# логику, чтобы не флагать корректные adversarial-refusal ответы вида
# "код phantom-77 мне не известен" как violation.
_REFUSAL_RE = re.compile(
    r"не\s+знаю|не\s+могу|нет\s+доступ|не\s+имею|недоступ|не\s+вправе|"
    r"не\s+буду|не\s+располага|отсутств|выдумыв|догад|отказ|"
    r"нет\s+(подтвержд|информац|данн)|не\s+подтверж|контекст",
    re.IGNORECASE,
)


def _marker_mentioned_during_refusal(answer: str, marker: str) -> bool:
    """True если marker упомянут в контексте явного отказа ("код X мне не известен"), не как факт."""
    if not _REFUSAL_RE.search(answer):
        return False
    echo = re.search(
        rf"(?:код|code|маркер|секрет|значени|вопрос|запрос|назван)[^.?!]*{re.escape(marker)}",
        answer,
        re.IGNORECASE,
    )
    return bool(echo)


@dataclass
class GuardrailViolation:
    """Структурированная ошибка срабатывания guardrail."""

    rule_id: str
    phase: str
    message: str
    tool: Optional[str] = None

    def to_tool_content(self) -> str:
        return (
            f"{GUARDRAIL_ERROR_PREFIX}: [{self.rule_id}] {self.message} "
            f"(phase={self.phase})"
        )


@dataclass
class ToolExecutionRecord:
    """Запись об исполнении инструмента в рамках хода."""

    name: str
    args: Dict[str, Any]
    content: str
    ok: bool
    blocked: bool = False


@dataclass
class GuardrailSessionState:
    """
    Персистентное состояние guardrails НА ВСЮ СЕССИЮ (между ходами диалога).

    v2: выделено из GuardrailContext. Раньше successful_tools/confirmed_pairs
    жили в контексте, который пересоздавался на каждый run_turn() — из-за
    этого цепочки вида "employee_lookup в ходе 1 → leave_balance в ходе 2"
    (40+ таких пар в каталоге бенчмарка) ошибочно блокировались правилами
    require_prior_success/require_confirmation, хотя prior tool был вызван
    легитимно, просто в ПРЕДЫДУЩЕМ ходе. Это состояние живёт до explicit
    reset() сессии (новый диалог), не до конца хода.
    """

    successful_tools: List[str] = field(default_factory=list)
    _confirmed_pairs: Set[tuple[str, str]] = field(default_factory=set)
    # v2 доп.: полная история содержимого успешных tool-ответов за сессию —
    # для require_marker_traceable (проверка, что маркер в финальном ответе
    # реально был получен от какого-то tool, а не выдуман/внедрён инъекцией).
    tool_response_history: List[str] = field(default_factory=list)

    def mark_successful(self, tool: str, content: str = "") -> None:
        self.successful_tools.append(tool)
        if content:
            self.tool_response_history.append(content)

    def had_successful(self, tool: str) -> bool:
        return tool in self.successful_tools

    def confirm(self, tool: str, content: str) -> None:
        if tool != "employee_lookup":
            return
        match = re.search(r"EMP_ID=([^\s]+)", content)
        if match:
            self._confirmed_pairs.add(("employee_lookup", match.group(1)))

    def hr_confirmed_for(self, emp_id: str) -> bool:
        return ("employee_lookup", emp_id) in self._confirmed_pairs

    def marker_seen(self, marker: str) -> bool:
        """
        True если marker встречается в каком-то успешном tool-ответе сессии.

        Регистронезависимо: LLM может процитировать маркер в другом регистре
        в свободном тексте ответа (например "emp_id hr-111" вместо
        "EMP_ID=HR-111") — это не должно считаться untraceable/hallucination.
        """
        needle = marker.lower()
        return any(needle in content.lower() for content in self.tool_response_history)


@dataclass
class GuardrailContext:
    """
    Состояние guardrails на один пользовательский ход.

    v2: держит ссылку на GuardrailSessionState (persist между ходами) для
    had_successful()/hr_confirmed_for() — только turn_tool_counts (лимиты
    типа max_calls_per_turn) и tool_records/violations (репорт этого хода)
    остаются локальными для хода, как и было задумано их семантикой.
    """

    session: GuardrailSessionState = field(default_factory=GuardrailSessionState)
    tool_records: List[ToolExecutionRecord] = field(default_factory=list)
    violations: List[GuardrailViolation] = field(default_factory=list)
    turn_tool_counts: Dict[str, int] = field(default_factory=dict)

    def record_block(
        self,
        tool: str,
        args: Dict[str, Any],
        violation: GuardrailViolation,
    ) -> None:
        self.violations.append(violation)
        self.tool_records.append(
            ToolExecutionRecord(name=tool, args=args, content=violation.to_tool_content(), ok=False, blocked=True)
        )

    def record_execution(
        self,
        tool: str,
        args: Dict[str, Any],
        content: str,
        *,
        ok: bool,
    ) -> None:
        self.tool_records.append(
            ToolExecutionRecord(name=tool, args=args, content=content, ok=ok, blocked=False)
        )
        if ok:
            self.session.mark_successful(tool, content)
            self.turn_tool_counts[tool] = self.turn_tool_counts.get(tool, 0) + 1
            self.session.confirm(tool, content)

    def had_successful(self, tool: str) -> bool:
        return self.session.had_successful(tool)

    def had_tool_error(self) -> bool:
        return any(not rec.ok and not rec.blocked for rec in self.tool_records)

    def marker_seen(self, marker: str) -> bool:
        return self.session.marker_seen(marker)

    def hr_confirmed_for(self, emp_id: str) -> bool:
        return self.session.hr_confirmed_for(emp_id)


def tool_result_is_error(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return True
    if text.startswith("Error") or text.startswith(GUARDRAIL_ERROR_PREFIX):
        return True
    if "ERROR:" in text.upper() and "RESULT=" not in text:
        return True
    if re.search(r"\bstatus\s*=\s*error\b", text, re.IGNORECASE):
        return True
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            status = str(payload.get("status", "")).lower()
            if status in {"error", "failed", "failure"}:
                return True
    except (json.JSONDecodeError, TypeError):
        pass
    return False


class GuardrailEngine:
    """Движок бизнес-правил: pre-tool, post-tool, post-response."""

    def __init__(self, rules: List[Dict[str, Any]], *, version: str = "unknown"):
        self.rules = rules
        self.version = version

    @classmethod
    def load(cls, rules_dir: Path | None = None) -> "GuardrailEngine":
        base = rules_dir or RULES_DIR
        merged: List[Dict[str, Any]] = []
        versions: List[str] = []
        if not base.exists():
            logger.warning("Rules directory not found: %s", base)
            return cls([], version="0")

        for path in sorted(base.glob("*.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            versions.append(str(raw.get("version", path.stem)))
            domain = raw.get("domain", path.stem)
            for phase in ("pre_tool", "post_tool", "post_response"):
                for rule in raw.get(phase) or []:
                    merged.append({**rule, "phase": phase, "domain": domain, "source": path.name})
        version = "+".join(versions) if versions else "0"
        logger.info("Guardrails loaded: %s rules, version=%s", len(merged), version)
        return cls(merged, version=version)

    @classmethod
    def load_default(cls) -> "GuardrailEngine":
        return cls.load(RULES_DIR)

    def pre_tool(
        self,
        ctx: GuardrailContext,
        tool: str,
        args: Dict[str, Any],
    ) -> Optional[GuardrailViolation]:
        for rule in self._rules_for("pre_tool"):
            violation = self._check_pre_tool(ctx, rule, tool, args)
            if violation:
                return violation
        return None

    def post_tool(
        self,
        ctx: GuardrailContext,
        tool: str,
        args: Dict[str, Any],
        content: str,
    ) -> Optional[GuardrailViolation]:
        for rule in self._rules_for("post_tool"):
            violation = self._check_post_tool(ctx, rule, tool, args, content)
            if violation:
                return violation
        return None

    def post_response(self, ctx: GuardrailContext, answer: str) -> Optional[GuardrailViolation]:
        for rule in self._rules_for("post_response"):
            violation = self._check_post_response(ctx, rule, answer)
            if violation:
                return violation
        return None

    def _rules_for(self, phase: str) -> List[Dict[str, Any]]:
        return [r for r in self.rules if r.get("phase") == phase]

    def _check_pre_tool(
        self,
        ctx: GuardrailContext,
        rule: Dict[str, Any],
        tool: str,
        args: Dict[str, Any],
    ) -> Optional[GuardrailViolation]:
        rtype = rule.get("type", "")
        if rtype == "deny_name_prefix":
            prefix = rule.get("prefix", "")
            if tool.startswith(prefix):
                return self._violation(rule, "pre_tool", tool, prefix=prefix)

        if rtype == "max_calls_per_turn":
            tools = set(rule.get("tools") or [])
            if tool in tools:
                current = ctx.turn_tool_counts.get(tool, 0)
                maximum = int(rule.get("max", 1))
                if current >= maximum:
                    return self._violation(rule, "pre_tool", tool, max=maximum)

        if rtype == "require_prior_success":
            target = rule.get("tool", "")
            prior = rule.get("prior_tool", "")
            if tool == target and not ctx.had_successful(prior):
                return self._violation(rule, "pre_tool", tool)

        if rtype == "require_confirmation":
            target = rule.get("tool", "")
            if tool != target:
                return None
            confirm_tool = rule.get("confirmation_tool", "")
            if not ctx.had_successful(confirm_tool):
                return self._violation(rule, "pre_tool", tool)
            if target == "leave_balance":
                emp_id = str(args.get("emp_id", ""))
                if emp_id and not ctx.hr_confirmed_for(emp_id):
                    return self._violation(rule, "pre_tool", tool)

        return None

    def _check_post_tool(
        self,
        ctx: GuardrailContext,
        rule: Dict[str, Any],
        tool: str,
        _args: Dict[str, Any],
        content: str,
    ) -> Optional[GuardrailViolation]:
        rtype = rule.get("type", "")
        if rtype == "require_substrings":
            if tool != rule.get("tool"):
                return None
            required = rule.get("required") or []
            missing = [item for item in required if item not in content]
            if missing:
                return self._violation(rule, "post_tool", tool, missing=missing)

        if rtype == "reject_error_status":
            if tool_result_is_error(content):
                return self._violation(rule, "post_tool", tool)

        return None

    def _check_post_response(
        self,
        ctx: GuardrailContext,
        rule: Dict[str, Any],
        answer: str,
    ) -> Optional[GuardrailViolation]:
        rtype = rule.get("type", "")
        if rtype == "require_tool_before_marker":
            marker = rule.get("marker", "")
            required_tool = rule.get("required_tool", "")
            if marker and marker in answer and not ctx.had_successful(required_tool):
                return self._violation(rule, "post_response")

        if rtype == "no_success_on_tool_error":
            if not ctx.had_tool_error():
                return None
            patterns = rule.get("success_patterns") or []
            lower = answer.lower()
            if any(p.lower() in lower for p in patterns):
                return self._violation(rule, "post_response")

        if rtype == "require_marker_traceable":
            pattern = rule.get("pattern", "")
            if not pattern:
                return None
            for match in re.finditer(pattern, answer, re.IGNORECASE):
                marker = match.group(0)
                if ctx.marker_seen(marker):
                    continue
                if _marker_mentioned_during_refusal(answer, marker):
                    continue
                return self._violation(rule, "post_response", marker=marker)

        return None

    def _violation(
        self,
        rule: Dict[str, Any],
        phase: str,
        tool: Optional[str] = None,
        **fmt: Any,
    ) -> GuardrailViolation:
        template = rule.get("message", "Guardrail violation")
        try:
            message = template.format(tool=tool or "", **fmt)
        except (KeyError, ValueError):
            message = template
        return GuardrailViolation(
            rule_id=str(rule.get("id", "unknown")),
            phase=phase,
            message=message,
            tool=tool,
        )


def format_violation(violation: GuardrailViolation) -> str:
    return violation.to_tool_content()
