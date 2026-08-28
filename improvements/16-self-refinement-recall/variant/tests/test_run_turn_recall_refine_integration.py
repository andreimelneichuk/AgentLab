"""Интеграционный тест: BasicLoopSession.run_turn() + recall_refine.

Проверяет реальный wiring в agent_core.py (не detect_likely_omission()
изолированно, как в test_recall_refine.py): что дополнительный LLM-вызов
(_call_self_refine_llm) происходит СТРОГО когда Tier-1 detect_likely_omission()
срабатывает, и НЕ происходит когда не срабатывает — через подсчёт реальных
вызовов build_llm()/ainvoke() (мок, как в
improvements/15-memory-formation/variant/tests/test_run_turn_memory_integration.py).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

import agent_core
from agent_core import AgentResources, BasicLoopSession


class _StubAIMessage:
    def __init__(self, content: str):
        self.content = content
        self.tool_calls = None
        self.response_metadata: Dict[str, Any] = {}


class _StubLLM:
    """Возвращает реплики по очереди (одна на каждый ainvoke), без tool_calls."""

    def __init__(self, replies: List[str]):
        self._replies = list(replies)
        self.ainvoke_calls: List[List[Any]] = []

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, lc_messages):
        self.ainvoke_calls.append(list(lc_messages))
        reply = self._replies.pop(0) if self._replies else "..."
        return _StubAIMessage(reply)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def resources() -> AgentResources:
    return AgentResources(
        config={"tools": {"call_limit": {}}, "llm_defaults": {}},
        tools=[],
        tool_map={},
        openai_tools=[],
        system_prompt="Ты — ассистент Basic.",
        run_limit=5,
        thread_limit=50,
        tool_exec_fail_retries=3,
        prompt_path=Path("prompts/system_master.txt"),
    )


@pytest.mark.anyio
async def test_refine_fires_when_draft_omits_established_fact(
    monkeypatch: pytest.MonkeyPatch, resources: AgentResources,
) -> None:
    """Turn 1 устанавливает EMP-4242 в истории. Turn 2 — recall-вопрос, worker
    отвечает БЕЗ значения → Tier-1 должен сработать и Tier-2 переписать ответ."""
    stub = _StubLLM(replies=[
        "Записал.",                              # turn 1 worker draft
        "Не могу сейчас точно сказать.",          # turn 2 worker draft (omission)
        "Ваш employee_id: EMP-4242.",             # tier-2 self-refine rewrite
    ])

    async def fake_build_llm(_config: Dict[str, Any], _alias: str) -> _StubLLM:
        return stub

    monkeypatch.setattr(agent_core, "build_llm", fake_build_llm)

    session = BasicLoopSession(resources, model_alias="test-alias")

    result_1 = await session.run_turn("Мой employee_id EMP-4242.")
    assert result_1.omission_refined is False

    result_2 = await session.run_turn("Какой у меня employee_id?")

    assert result_2.omission_refined is True
    assert "EMP-4242" in result_2.answer
    assert result_2.answer == "Ваш employee_id: EMP-4242."
    # Ровно 3 ainvoke: turn1 worker, turn2 worker (omission draft), tier-2 refine.
    assert len(stub.ainvoke_calls) == 3


@pytest.mark.anyio
async def test_refine_does_not_fire_on_non_recall_turn(
    monkeypatch: pytest.MonkeyPatch, resources: AgentResources,
) -> None:
    """Обычный, не-recall ход НЕ должен вызывать дополнительный LLM-проход —
    Tier-2 гарантированно gated Tier-1."""
    stub = _StubLLM(replies=[
        "Записал.",
        "Bob Smith работает в отделе Sales.",
    ])

    async def fake_build_llm(_config: Dict[str, Any], _alias: str) -> _StubLLM:
        return stub

    monkeypatch.setattr(agent_core, "build_llm", fake_build_llm)

    session = BasicLoopSession(resources, model_alias="test-alias")

    await session.run_turn("Мой employee_id EMP-4242.")
    result_2 = await session.run_turn("Найди сотрудника Bob Smith")

    assert result_2.omission_refined is False
    assert result_2.answer == "Bob Smith работает в отделе Sales."
    # Только 2 ainvoke (по одному на ход) — Tier-2 НЕ вызывался.
    assert len(stub.ainvoke_calls) == 2


@pytest.mark.anyio
async def test_refine_does_not_fire_when_draft_already_has_value(
    monkeypatch: pytest.MonkeyPatch, resources: AgentResources,
) -> None:
    """Recall-вопрос, но черновик УЖЕ содержит нужное значение — Tier-1 не
    должен срабатывать (условие (c) не выполнено), Tier-2 не вызывается."""
    stub = _StubLLM(replies=[
        "Записал.",
        "Ваш employee_id: EMP-4242.",
    ])

    async def fake_build_llm(_config: Dict[str, Any], _alias: str) -> _StubLLM:
        return stub

    monkeypatch.setattr(agent_core, "build_llm", fake_build_llm)

    session = BasicLoopSession(resources, model_alias="test-alias")

    await session.run_turn("Мой employee_id EMP-4242.")
    result_2 = await session.run_turn("Какой у меня employee_id?")

    assert result_2.omission_refined is False
    assert result_2.answer == "Ваш employee_id: EMP-4242."
    assert len(stub.ainvoke_calls) == 2
