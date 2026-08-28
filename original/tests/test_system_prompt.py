"""Unit-тесты передачи system prompt в LLM."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, SystemMessage

from agent_core import (
    AgentResources,
    BasicLoopSession,
    messages_to_langchain,
)


def test_messages_to_langchain_prepends_system_prompt():
    """messages_to_langchain добавляет SystemMessage с полным промптом."""
    system = "Ты — тестовый ассистент."
    lc = messages_to_langchain([{"role": "user", "content": "Привет"}], system)
    assert isinstance(lc[0], SystemMessage)
    assert lc[0].content == system
    assert lc[1].type == "human"


def test_messages_to_langchain_skips_empty_system_prompt():
    """Пустой system prompt не добавляет SystemMessage."""
    lc = messages_to_langchain([{"role": "user", "content": "Привет"}], "")
    assert len(lc) == 1
    assert lc[0].type == "human"


def test_run_turn_passes_system_prompt_to_llm():
    """run_turn передаёт system prompt в каждый вызов LLM."""
    resources = AgentResources(
        config={"llm_defaults": {"attempts_per_model": 1}},
        tools=[],
        tool_map={},
        openai_tools=[],
        system_prompt="BASE_SYSTEM_PROMPT",
        run_limit=5,
        thread_limit=50,
        tool_exec_fail_retries=3,
        prompt_path=MagicMock(),
    )
    session = BasicLoopSession(resources, model_alias="test-model")

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="Ответ"))

    async def _run() -> None:
        with patch("agent_core.build_llm", new=AsyncMock(return_value=mock_llm)), patch(
            "agent_core.resolve_model_aliases",
            return_value=["test-model"],
        ):
            result = await session.run_turn("Вопрос")

        assert result.answer == "Ответ"
        mock_llm.ainvoke.assert_awaited_once()
        lc_msgs = mock_llm.ainvoke.await_args.args[0]
        assert isinstance(lc_msgs[0], SystemMessage)
        assert lc_msgs[0].content == "BASE_SYSTEM_PROMPT"

    asyncio.run(_run())
