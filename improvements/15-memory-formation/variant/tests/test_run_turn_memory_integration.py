"""Интеграционный тест: BasicLoopSession.run_turn() + memory (не MemoryManager изолированно).

Закрывает пробел из JUDGE.md ("Незакрытые пробелы"): юнит-тесты в
test_memory.py гоняют MemoryManager напрямую и никогда не проходят через
реальный agent_core.run_turn() — тот код path, который вызывает
_effective_system_prompt() ДО обращения к LLM и _persist_turn_memory() ПОСЛЕ.

LLM подменяется стабом (monkeypatch agent_core.build_llm), чтобы не требовать
живого backend'а — но весь остальной путь (AgentResources, BasicLoopSession,
MemoryManager, SqliteMemoryStore) реальный, включая messages_to_langchain()
и то, как system_prompt с инжектированной памятью попадает в LLM-вызов.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

import agent_core
from agent_core import AgentResources, BasicLoopSession


class _StubAIMessage:
    """Имитирует ответ LangChain AIMessage: есть .content, нет .tool_calls."""

    def __init__(self, content: str):
        self.content = content
        self.tool_calls = None
        self.response_metadata: Dict[str, Any] = {}


class _StubLLM:
    """Стаб ChatOpenAI: bind_tools() -> self, ainvoke() -> канонический ответ без tool_calls."""

    def __init__(self, reply: str):
        self._reply = reply
        self.seen_system_prompts: List[str] = []

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, lc_messages):
        # Первое сообщение — SystemMessage с (возможно) инжектированной памятью.
        if lc_messages:
            self.seen_system_prompts.append(str(lc_messages[0].content))
        return _StubAIMessage(self._reply)


@pytest.fixture
def anyio_backend() -> str:
    # trio не установлен в этом окружении — ограничиваем anyio только asyncio.
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
async def test_run_turn_persists_and_recalls_fact_across_reset(
    monkeypatch: pytest.MonkeyPatch, resources: AgentResources, tmp_path: Path,
) -> None:
    """Полный цикл run_turn(): turn 1 сохраняет факт, reset() чистит _messages,
    turn 2 всё равно видит факт в _effective_system_prompt() благодаря памяти,
    не короткосрочной истории.
    """
    stub = _StubLLM(reply="Хорошо, буду использовать Python.")

    async def fake_build_llm(_config: Dict[str, Any], _alias: str) -> _StubLLM:
        return stub

    monkeypatch.setattr(agent_core, "build_llm", fake_build_llm)

    from memory import MemoryManager, SqliteMemoryStore

    store = SqliteMemoryStore(tmp_path / "integration_memory.db")
    memory_manager = MemoryManager(store, top_k=5)

    session = BasicLoopSession(
        resources,
        model_alias="test-alias",
        user_id="integration_user",
        memory_manager=memory_manager,
        base_dir=tmp_path,
    )

    # --- Turn 1: пользователь сообщает предпочтение. ---
    result_1 = await session.run_turn("Я предпочитаю Python, не Java.")
    assert result_1.answer == "Хорошо, буду использовать Python."

    # run_turn() должен вызвать _persist_turn_memory() после ответа LLM —
    # проверяем, что факт реально осел в SQLite-хранилище через session.user_id.
    saved_facts = memory_manager.store.list_facts("integration_user")
    assert any("Python" in f.content for f in saved_facts), (
        "Ожидали, что _persist_turn_memory() сохранит факт о предпочтении "
        "Python в SqliteMemoryStore после turn 1."
    )

    # Короткосрочная история после turn 1 непуста.
    assert session._messages, "После turn 1 _messages должен быть непустым."

    # --- Новая сессия: reset() чистит короткосрочный контекст (как new_session в бенчмарке). ---
    session.reset()
    assert session._messages == [], "reset() должен полностью очистить _messages."

    # --- Turn 2: другой вопрос, без исходной фразы про Python в short-term контексте. ---
    result_2 = await session.run_turn("Какой язык программирования я предпочитаю?")
    assert result_2.answer  # стаб всегда отвечает, интересна не сама реплика.

    # Ключевая проверка: system prompt, который реально был передан в LLM во время
    # turn 2, содержит факт про Python — притом что _messages в этот момент
    # содержит ТОЛЬКО turn-2 реплики (короткосрочная история была стёрта).
    assert len(stub.seen_system_prompts) == 2, "Ожидали ровно 2 вызова ainvoke (turn 1 и turn 2)."
    system_prompt_turn_2 = stub.seen_system_prompts[1]
    assert "Python" in system_prompt_turn_2, (
        "_effective_system_prompt() для turn 2 должен содержать факт 'Python', "
        "восстановленный из долговременной памяти, а не из _messages "
        "(которые были очищены session.reset())."
    )

    # И убеждаемся, что короткосрочная история НЕ содержит исходную фразу про Python —
    # то есть recall действительно пришёл из памяти, а не из истории сообщений.
    short_term_contents = " ".join(
        str(m.get("content", "")) for m in session._messages
    )
    assert "Я предпочитаю Python" not in short_term_contents, (
        "Исходная фраза о предпочтении не должна быть в short-term _messages "
        "после reset() — если тест проходит, значит recall шёл через память."
    )


@pytest.mark.anyio
async def test_effective_system_prompt_reflects_memory_before_llm_call(
    monkeypatch: pytest.MonkeyPatch, resources: AgentResources, tmp_path: Path,
) -> None:
    """Более узкая проверка wiring: _effective_system_prompt() вызывается ДО LLM
    и включает факты, ранее сохранённые через _persist_turn_memory() в другой
    "сессии" (после reset()).
    """
    stub = _StubLLM(reply="EMP-4242 — ваш employee_id.")

    async def fake_build_llm(_config: Dict[str, Any], _alias: str) -> _StubLLM:
        return stub

    monkeypatch.setattr(agent_core, "build_llm", fake_build_llm)

    from memory import MemoryManager, SqliteMemoryStore

    store = SqliteMemoryStore(tmp_path / "integration_memory_2.db")
    memory_manager = MemoryManager(store, top_k=5)

    session = BasicLoopSession(
        resources,
        model_alias="test-alias",
        user_id="integration_user_2",
        memory_manager=memory_manager,
        base_dir=tmp_path,
    )

    await session.run_turn("Запомни: мой employee_id EMP-4242.")
    session.reset()

    prompt_before_llm_call = session._effective_system_prompt("Какой у меня employee_id?")
    assert "EMP-4242" in prompt_before_llm_call

    await session.run_turn("Какой у меня employee_id?")
    assert "EMP-4242" in stub.seen_system_prompts[-1]
