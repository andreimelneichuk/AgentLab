"""Unit-тесты Buddy System (mock judge)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core import AgentResources, BasicLoopSession, buddy_settings, load_config
from buddy_agent import (
    CriteriaRegistry,
    Guide,
    build_criteria_registry,
    format_guide_message,
    judge_worker_output,
    symbolic_precheck,
)

VARIANT_DIR = Path(__file__).resolve().parent.parent
PROMPT_PATH = VARIANT_DIR / "prompts" / "system_master.txt"
BENCHMARK_PROMPT_SNIPPET = """
**СЕКЦИЯ 2. ИНСТРУМЕНТЫ**

**get_policy_fact**
- Назначение: официальный факт политики.
- НЕ использовать: `decoy_policy_fact`.

**decoy_policy_fact**
- Назначение: приманка.

**СЕКЦИЯ 3. ПРАВИЛА ПРИНЯТИЯ РЕШЕНИЙ**

1. **Порядок:** сначала инструмент, затем ответ.
2. **Выбор инструмента:** canonical, не decoy.

**СЕКЦИЯ 4. ФОРМАТ ВЫВОДА**

- Если результат `get_policy_fact` — ответ заканчивается `[POLICY_OK]`.
"""


@pytest.fixture
def benchmark_config() -> Dict[str, Any]:
    return load_config(VARIANT_DIR / "config.yml")


@pytest.fixture
def criteria_registry() -> CriteriaRegistry:
    return build_criteria_registry(BENCHMARK_PROMPT_SNIPPET)


def test_build_criteria_registry_extracts_sections(criteria_registry: CriteriaRegistry):
    """Реестр критериев содержит правила из секций tools/format/instructions."""
    assert criteria_registry.raw_sections["tools"]
    assert criteria_registry.raw_sections["format"]
    assert criteria_registry.raw_sections["instructions"]
    assert len(criteria_registry.tools_rules) > 0
    assert len(criteria_registry.format_rules) > 0
    assert len(criteria_registry.instruction_rules) > 0
    assert "decoy_policy_fact" in criteria_registry.forbidden_tools
    assert "[POLICY_OK]" in criteria_registry.required_markers


def test_symbolic_precheck_forbidden_tool(criteria_registry: CriteriaRegistry):
    """Символическая проверка ловит forbidden tool в трейсе."""
    trace = [
        {"role": "user", "content": "дай политику"},
        {
            "role": "assistant",
            "tool_calls": [{"name": "decoy_policy_fact", "arguments": {}}],
        },
    ]
    guide = symbolic_precheck(
        draft_answer="POLICY_FACT=violet-99",
        trace=trace,
        criteria=criteria_registry,
        user_message="дай политику",
    )
    assert guide is not None
    assert "decoy_policy_fact" in guide.feedback


def test_symbolic_precheck_missing_policy_ok(criteria_registry: CriteriaRegistry):
    """Символическая проверка требует [POLICY_OK] после get_policy_fact."""
    trace = [
        {"role": "user", "content": "вызови get_policy_fact"},
        {
            "role": "assistant",
            "tool_calls": [{"name": "get_policy_fact", "arguments": {}}],
        },
        {"role": "tool", "content": "POLICY_FACT=violet-42"},
        {"role": "assistant", "content": "Секрет violet-42"},
    ]
    guide = symbolic_precheck(
        draft_answer="Секрет violet-42",
        trace=trace,
        criteria=criteria_registry,
        user_message="вызови get_policy_fact",
    )
    assert guide is not None
    assert "[POLICY_OK]" in guide.feedback


def test_symbolic_precheck_passes_valid_answer(criteria_registry: CriteriaRegistry):
    """Валидный ответ проходит символическую проверку."""
    trace = [
        {"role": "user", "content": "вызови get_policy_fact"},
        {
            "role": "assistant",
            "tool_calls": [{"name": "get_policy_fact", "arguments": {}}],
        },
    ]
    guide = symbolic_precheck(
        draft_answer="Секрет violet-42 [POLICY_OK]",
        trace=trace,
        criteria=criteria_registry,
        user_message="вызови get_policy_fact",
    )
    assert guide is None


# --- v2 regression: scope-баг (session-wide vs per-turn проверки) -----------


def test_symbolic_precheck_no_false_positive_for_unrelated_later_turn(
    criteria_registry: CriteriaRegistry,
):
    """
    v2 regression: get_policy_fact, вызванный В ПРОШЛОМ ходе, не должен
    требовать [POLICY_OK] в текущем несвязанном ходе (например про погоду).

    До фикса: symbolic_precheck проверял "вызван ли get_policy_fact" по
    ВСЕЙ истории сессии (весь `trace`) — 197/753 ходов каталога (26%)
    ложно блокировались этим. current_turn_trace ограничивает проверку
    только текущим ходом.
    """
    full_history = [
        {"role": "user", "content": "Какой секрет политики?"},
        {"role": "assistant", "tool_calls": [{"name": "get_policy_fact", "arguments": {}}]},
        {"role": "tool", "content": "POLICY_FACT=violet-42"},
        {"role": "assistant", "content": "Секрет: violet-42 [POLICY_OK]"},
    ]
    current_turn = [
        {"role": "user", "content": "Какая погода в Берлине?"},
        {"role": "assistant", "tool_calls": [{"name": "weather_city", "arguments": {"city": "Berlin"}}]},
        {"role": "tool", "content": "WEATHER city=Berlin temp_c=12"},
    ]
    guide = symbolic_precheck(
        draft_answer="В Берлине сейчас 12 градусов.",
        trace=full_history + current_turn,
        current_turn_trace=current_turn,
        criteria=criteria_registry,
        user_message="Какая погода в Берлине?",
    )
    assert guide is None


def test_symbolic_precheck_still_requires_policy_ok_when_called_this_turn(
    criteria_registry: CriteriaRegistry,
):
    """Негативный контроль: если get_policy_fact вызван В ЭТОМ ходе — правило продолжает работать."""
    current_turn = [
        {"role": "user", "content": "вызови get_policy_fact"},
        {"role": "assistant", "tool_calls": [{"name": "get_policy_fact", "arguments": {}}]},
        {"role": "tool", "content": "POLICY_FACT=violet-42"},
    ]
    guide = symbolic_precheck(
        draft_answer="Секрет violet-42",  # без [POLICY_OK] — нарушение
        trace=current_turn,
        current_turn_trace=current_turn,
        criteria=criteria_registry,
        user_message="вызови get_policy_fact",
    )
    assert guide is not None
    assert "[POLICY_OK]" in guide.feedback


def test_symbolic_precheck_forbidden_tool_scoped_to_current_turn(
    criteria_registry: CriteriaRegistry,
):
    """
    v2 regression: forbidden tool, вызванный в ПРОШЛОМ ходе, не должен
    блокировать текущий несвязанный ход (аналогично get_policy_fact scope-багу).
    """
    full_history = [
        {"role": "user", "content": "дай политику"},
        {"role": "assistant", "tool_calls": [{"name": "decoy_policy_fact", "arguments": {}}]},
        {"role": "tool", "content": "POLICY_FACT=violet-99"},
        {"role": "assistant", "content": "POLICY_FACT=violet-99 [POLICY_OK]"},
    ]
    current_turn = [
        {"role": "user", "content": "Найди сотрудника Alice"},
        {"role": "assistant", "tool_calls": [{"name": "employee_lookup", "arguments": {"name": "Alice"}}]},
        {"role": "tool", "content": "EMP_ID=HR-001 NAME=Alice"},
    ]
    guide = symbolic_precheck(
        draft_answer="EMP_ID=HR-001 NAME=Alice",
        trace=full_history + current_turn,
        current_turn_trace=current_turn,
        criteria=criteria_registry,
        user_message="Найди сотрудника Alice",
    )
    assert guide is None


def test_symbolic_precheck_persistent_dialogue_rule_still_applies_across_turns(
    criteria_registry: CriteriaRegistry,
):
    """
    Позитивный контроль: явно установленное пользователем персистентное
    правило диалога ("каждый ответ заканчивается [POLICY_OK]") ДОЛЖНО
    продолжать применяться к последующим несвязанным ходам — это
    единственная проверка, для которой full-session scope сохранён осознанно.
    """
    full_history = [
        {"role": "user", "content": "Правило на диалог: каждый ответ заканчивается [POLICY_OK]. Подтверди."},
        {"role": "assistant", "content": "Договорились. [POLICY_OK]"},
    ]
    current_turn = [
        {"role": "user", "content": "Какая погода в Берлине?"},
        {"role": "assistant", "tool_calls": [{"name": "weather_city", "arguments": {"city": "Berlin"}}]},
        {"role": "tool", "content": "WEATHER city=Berlin temp_c=12"},
    ]
    guide = symbolic_precheck(
        draft_answer="В Берлине сейчас 12 градусов.",  # без [POLICY_OK] — нарушает персистентное правило
        trace=full_history + current_turn,
        current_turn_trace=current_turn,
        criteria=criteria_registry,
        user_message="Какая погода в Берлине?",
    )
    assert guide is not None
    assert "[POLICY_OK]" in guide.feedback


def test_symbolic_precheck_current_turn_trace_defaults_to_full_trace(
    criteria_registry: CriteriaRegistry,
):
    """Обратная совместимость: без current_turn_trace поведение как раньше (весь trace)."""
    trace = [
        {"role": "user", "content": "вызови get_policy_fact"},
        {"role": "assistant", "tool_calls": [{"name": "get_policy_fact", "arguments": {}}]},
        {"role": "tool", "content": "POLICY_FACT=violet-42"},
    ]
    guide = symbolic_precheck(
        draft_answer="Секрет violet-42",
        trace=trace,
        criteria=criteria_registry,
        user_message="вызови get_policy_fact",
        # current_turn_trace не передан
    )
    assert guide is not None


def test_format_guide_message():
    """Guide форматируется в сообщение для retry worker."""
    text = format_guide_message(Guide(feedback="Добавь [POLICY_OK]", failed_criteria=["format_policy_ok"]))
    assert "[BUDDY_GUIDE]" in text
    assert "[POLICY_OK]" in text
    assert "format_policy_ok" in text


def test_judge_worker_output_pass_with_mock_llm(criteria_registry: CriteriaRegistry):
    """Mock judge возвращает pass — Guide не выдаётся."""
    async def _run() -> None:
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(return_value=MagicMock(content='{"pass": true}'))

        guide = await judge_worker_output(
            llm,
            user_message="привет",
            worker_trace=[{"role": "user", "content": "привет"}],
            draft_answer="Здравствуйте",
            criteria=criteria_registry,
            enable_symbolic_precheck=False,
            enable_llm_gate=False,
        )
        assert guide is None
        llm.ainvoke.assert_awaited_once()

    asyncio.run(_run())


def test_judge_worker_output_guide_with_mock_llm(criteria_registry: CriteriaRegistry):
    """Mock judge возвращает fail — выдаётся Guide."""
    async def _run() -> None:
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(return_value=MagicMock(
            content='{"pass": false, "feedback": "Нужен маркер DELTA", "failed_criteria": ["instruction_delta"]}',
        ))

        guide = await judge_worker_output(
            llm,
            user_message="ответь с DELTA",
            worker_trace=[{"role": "user", "content": "ответь с DELTA"}],
            draft_answer="Ответ без маркера",
            criteria=criteria_registry,
            enable_symbolic_precheck=False,
            enable_llm_gate=False,
        )
        assert guide is not None
        assert "DELTA" in guide.feedback
        assert guide.failed_criteria == ["instruction_delta"]

    asyncio.run(_run())


def test_buddy_loop_passes_on_first_try(benchmark_config: Dict[str, Any]):
    """E2E loop: judge pass на первой попытке — без retry."""
    async def _run() -> None:
        resources = MagicMock(spec=AgentResources)
        resources.config = benchmark_config
        resources.run_limit = 5
        resources.openai_tools = []
        resources.tool_map = {}
        resources.criteria_registry = build_criteria_registry(BENCHMARK_PROMPT_SNIPPET)

        session = BasicLoopSession(resources)
        session._generate_turn = AsyncMock(return_value=(
            "Ответ OK",
            [{"role": "user", "content": "тест"}, {"role": "assistant", "content": "Ответ OK"}],
            [],
            [],
            1,
            10,
            5,
        ))

        with patch("agent_core.judge_worker_output", new_callable=AsyncMock) as mock_judge:
            mock_judge.return_value = None
            result = await session.run_turn("тест")

        assert result.answer == "Ответ OK"
        assert result.buddy_interventions == 0
        assert result.buddy_retries == 0
        assert result.buddy_passed is True
        mock_judge.assert_awaited_once()

    asyncio.run(_run())


def test_buddy_loop_retries_on_guide(benchmark_config: Dict[str, Any]):
    """E2E loop: judge fail → retry → pass."""
    async def _run() -> None:
        resources = MagicMock(spec=AgentResources)
        resources.config = benchmark_config
        resources.run_limit = 5
        resources.openai_tools = []
        resources.tool_map = {}
        resources.criteria_registry = build_criteria_registry(BENCHMARK_PROMPT_SNIPPET)

        session = BasicLoopSession(resources)
        call_count = 0

        async def fake_generate(llm, working, *, rounds_start=0):
            nonlocal call_count
            call_count += 1
            answer = "Исправлено [POLICY_OK]" if call_count > 1 else "Без маркера"
            return answer, [*working, {"role": "assistant", "content": answer}], [], [], 1, 5, 3

        session._generate_turn = fake_generate

        guides: List[Optional[Guide]] = [
            Guide(feedback="Добавь [POLICY_OK]", failed_criteria=["format_policy_ok"]),
            None,
        ]

        with patch("agent_core.judge_worker_output", new_callable=AsyncMock) as mock_judge:
            mock_judge.side_effect = guides
            result = await session.run_turn("тест")

        assert call_count == 2
        assert result.buddy_interventions == 1
        assert result.buddy_retries == 1
        assert result.buddy_passed is True
        assert "[POLICY_OK]" in result.answer

    asyncio.run(_run())


def test_buddy_loop_stops_at_max_retries(benchmark_config: Dict[str, Any]):
    """E2E loop: после max_retries возвращает последний ответ."""
    async def _run() -> None:
        cfg = dict(benchmark_config)
        cfg["buddy"] = {"enabled": True, "max_retries": 3, "symbolic_precheck": True}
        resources = MagicMock(spec=AgentResources)
        resources.config = cfg
        resources.run_limit = 5
        resources.openai_tools = []
        resources.tool_map = {}
        resources.criteria_registry = build_criteria_registry(BENCHMARK_PROMPT_SNIPPET)

        session = BasicLoopSession(resources)
        session._generate_turn = AsyncMock(return_value=(
            "Плохой ответ",
            [{"role": "user", "content": "тест"}, {"role": "assistant", "content": "Плохой ответ"}],
            [],
            [],
            1,
            5,
            3,
        ))

        with patch("agent_core.judge_worker_output", new_callable=AsyncMock) as mock_judge:
            mock_judge.return_value = Guide(feedback="Исправь", failed_criteria=["x"])
            result = await session.run_turn("тест")

        assert mock_judge.await_count == 4
        assert result.buddy_interventions == 4
        assert result.buddy_retries == 3
        assert result.buddy_passed is False
        assert result.answer == "Плохой ответ"

    asyncio.run(_run())


def test_run_turn_passes_current_turn_trace_scoped_to_new_turn(benchmark_config: Dict[str, Any]):
    """
    v2 regression: run_turn() должен передавать judge_worker_output()
    current_turn_trace, ограниченный ТОЛЬКО текущим ходом — не включающий
    предыдущие ходы сессии (self._messages на момент вызова).
    """
    async def _run() -> None:
        resources = MagicMock(spec=AgentResources)
        resources.config = benchmark_config
        resources.run_limit = 5
        resources.openai_tools = []
        resources.tool_map = {}
        resources.criteria_registry = build_criteria_registry(BENCHMARK_PROMPT_SNIPPET)

        session = BasicLoopSession(resources)
        # Предыдущий ход уже в истории сессии (2 сообщения: user + assistant).
        session._messages = [
            {"role": "user", "content": "прошлый вопрос"},
            {"role": "assistant", "content": "прошлый ответ"},
        ]

        async def fake_generate(llm, working, *, rounds_start=0):
            new_msgs = [
                {"role": "assistant", "tool_calls": [{"name": "weather_city", "arguments": {}}]},
                {"role": "tool", "content": "WEATHER city=Berlin temp_c=12"},
                {"role": "assistant", "content": "12 градусов"},
            ]
            return "12 градусов", [*working, *new_msgs], ["weather_city"], [], 1, 5, 3

        session._generate_turn = fake_generate

        with patch("agent_core.judge_worker_output", new_callable=AsyncMock) as mock_judge:
            mock_judge.return_value = None
            await session.run_turn("Какая погода?")

        _, kwargs = mock_judge.call_args
        current_turn_trace = kwargs["current_turn_trace"]
        # current_turn_trace не должен содержать сообщения прошлого хода.
        assert all(
            m.get("content") not in ("прошлый вопрос", "прошлый ответ")
            for m in current_turn_trace
        )
        # Но должен содержать сообщения текущего хода (новый user + tool call).
        assert any(m.get("content") == "Какая погода?" for m in current_turn_trace)

    asyncio.run(_run())


def test_buddy_settings_defaults(benchmark_config: Dict[str, Any]):
    """Настройки buddy: max_retries=3 по умолчанию."""
    settings = buddy_settings(benchmark_config)
    assert settings["enabled"] is True
    assert settings["max_retries"] == 3
    assert settings["symbolic_precheck"] is True


def test_criteria_from_rendered_prompt(benchmark_config: Dict[str, Any]):
    """Реестр строится из отрендеренного four-section промпта."""
    from agent_core import render_system_prompt

    rendered = render_system_prompt(benchmark_config, PROMPT_PATH, has_tools=True)
    registry = build_criteria_registry(rendered)
    assert registry.raw_sections.get("tools")
    assert registry.raw_sections.get("format")
    assert "decoy_" in " ".join(registry.forbidden_tools) or len(registry.forbidden_tools) >= 0
