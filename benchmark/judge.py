"""LLM-судья для ходов, где автоматический checker дал FAIL по содержимому."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from benchmark.llm_client import build_chat_llm
from benchmark.scoring import CITY_ALIASES, TurnCheckResult, TurnExpectation

logger = logging.getLogger("benchmark.judge")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JUDGE_PROMPT = Path(__file__).resolve().parent / "prompts" / "judge_system.txt"

HARD_FAILURE_PREFIXES = (
    "инструмент ",
    "запрещённый инструмент",
    "MCP delta",
    "R_NTA",
    "R_DT",
    "аргументы tool",
    "rounds ",
)

_CITY_TOOL_ARGS_RE = re.compile(
    r"^аргументы tool 'weather_city' не совпали с \{'city': '([^']+)'\}$",
)


def _is_city_alias_tool_args_failure(failure: str) -> bool:
    """Аргументы weather_city с кириллическим алиасом — мягкий провал для судьи."""
    match = _CITY_TOOL_ARGS_RE.match(failure)
    if not match:
        return False
    return match.group(1) in CITY_ALIASES


def _is_hard_failure(failure: str) -> bool:
    if any(failure.startswith(prefix) for prefix in HARD_FAILURE_PREFIXES):
        if failure.startswith("аргументы tool") and _is_city_alias_tool_args_failure(failure):
            return False
        return True
    return False


@dataclass
class JudgeVerdict:
    eligible: bool
    passed: bool
    reason: str = ""
    checker_failures: Optional[List[str]] = None


def is_judge_eligible(failures: List[str]) -> bool:
    """Судья только для «мягких» провалов (формулировка), не tool/MCP/NTA."""
    if not failures:
        return False
    return all(not _is_hard_failure(failure) for failure in failures)


def _format_expectations(exp: TurnExpectation) -> str:
    parts: List[str] = []
    if exp.expect_contains:
        parts.append(f"expect_contains ({exp.expect_contains_mode}): {exp.expect_contains}")
    if exp.expect_regex:
        parts.append(f"expect_regex: {exp.expect_regex}")
    if exp.forbid_contains:
        parts.append(f"forbid_contains: {exp.forbid_contains}")
    if exp.expect_tool_called:
        parts.append(f"expect_tool_called: {exp.expect_tool_called}")
    if exp.forbid_tool_called:
        parts.append(f"forbid_tool_called: {exp.forbid_tool_called}")
    if exp.forbid_tools_called:
        parts.append(f"forbid_tools_called: {exp.forbid_tools_called}")
    if exp.expect_tool_args:
        parts.append(f"expect_tool_args: {exp.expect_tool_args}")
    if exp.min_tool_calls_delta:
        parts.append(f"min_tool_calls_delta: {exp.min_tool_calls_delta}")
    if exp.max_tool_calls_delta is not None:
        parts.append(f"max_tool_calls_delta: {exp.max_tool_calls_delta}")
    if exp.hallucination_markers:
        parts.append(f"hallucination_markers: {exp.hallucination_markers}")
    return "\n".join(parts) or "(нет явных ожиданий)"


def _build_user_prompt(
    *,
    scenario_id: str,
    turn_index: int,
    exp: TurnExpectation,
    answer: str,
    tools_called: List[str],
    tool_delta: int,
    checker_failures: List[str],
) -> str:
    return (
        f"scenario_id: {scenario_id}\n"
        f"turn_index: {turn_index}\n\n"
        f"Вопрос пользователя:\n{exp.user}\n\n"
        f"Ответ ассистента:\n{answer}\n\n"
        f"Вызванные tools: {tools_called or '[]'}\n"
        f"MCP delta на ходе: {tool_delta}\n\n"
        f"Ожидания сценария:\n{_format_expectations(exp)}\n\n"
        f"Причины FAIL автоматического checker:\n"
        + "\n".join(f"- {f}" for f in checker_failures)
    )


def _parse_judge_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[^{}]*\"pass\"[^{}]*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    lowered = text.lower()
    if '"pass": true' in lowered or '"pass":true' in lowered:
        return {"pass": True, "reason": text[:200]}
    if '"pass": false' in lowered or '"pass":false' in lowered:
        return {"pass": False, "reason": text[:200]}
    return {"pass": False, "reason": f"не удалось разобрать ответ судьи: {text[:200]}"}


class TurnJudge:
    def __init__(self, llm: Any, system_prompt: str):
        self._llm = llm
        self._system_prompt = system_prompt

    @classmethod
    async def create(
        cls,
        config: Dict[str, Any],
        model_alias: str,
        judge_cfg: Optional[Dict[str, Any]] = None,
    ) -> "TurnJudge":
        cfg = judge_cfg or {}
        prompt_path = Path(cfg.get("prompt_path") or DEFAULT_JUDGE_PROMPT)
        if not prompt_path.is_absolute():
            prompt_path = REPO_ROOT / prompt_path
        system_prompt = prompt_path.read_text(encoding="utf-8").strip()
        llm = await build_chat_llm(
            config,
            cfg.get("model_alias") or model_alias,
            temperature=float(cfg.get("temperature", 0.1)),
            max_tokens=int(cfg.get("max_tokens", 512)),
        )
        return cls(llm, system_prompt)

    async def review_turn(
        self,
        *,
        scenario_id: str,
        turn_index: int,
        exp: TurnExpectation,
        answer: str,
        tools_called: List[str],
        tool_delta: int,
        check: TurnCheckResult,
    ) -> JudgeVerdict:
        failures = list(check.failures)
        if check.passed:
            return JudgeVerdict(eligible=False, passed=True)
        if not is_judge_eligible(failures):
            return JudgeVerdict(
                eligible=False,
                passed=False,
                reason="hard-fail: не отправлялось судье",
                checker_failures=failures,
            )

        user_prompt = _build_user_prompt(
            scenario_id=scenario_id,
            turn_index=turn_index,
            exp=exp,
            answer=answer,
            tools_called=tools_called,
            tool_delta=tool_delta,
            checker_failures=failures,
        )
        try:
            response = await self._llm.ainvoke([
                SystemMessage(content=self._system_prompt),
                HumanMessage(content=user_prompt),
            ])
            content = response.content if isinstance(response.content, str) else str(response.content)
            parsed = _parse_judge_json(content)
            passed = bool(parsed.get("pass"))
            reason = str(parsed.get("reason") or "").strip()
            return JudgeVerdict(
                eligible=True,
                passed=passed,
                reason=reason or ("судья: pass" if passed else "судья: fail"),
                checker_failures=failures,
            )
        except Exception as exc:
            logger.warning("Judge failed for %s turn %s: %s", scenario_id, turn_index, exc)
            return JudgeVerdict(
                eligible=True,
                passed=False,
                reason=f"ошибка судьи: {exc}",
                checker_failures=failures,
            )
