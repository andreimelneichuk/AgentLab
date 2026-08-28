"""Unit-тесты 4-секционного system prompt и render_system_prompt."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_core import (
    PROMPT_SECTION_HEADERS,
    messages_to_langchain,
    render_system_prompt,
)
from prompt_tools import get_canonical_tool_names, is_decoy_tool, load_registry_tools

DECOY_TOOLS = (
    "decoy_policy_fact",
    "decoy_python_lookup",
    "decoy_inventory_lookup",
    "decoy_employee_search",
    "decoy_customer_lookup",
    "decoy_sse_cache",
)

VARIANT_DIR = Path(__file__).resolve().parent.parent
PROMPT_PATH = VARIANT_DIR / "prompts" / "system_master.txt"


@pytest.fixture
def default_bot_config() -> dict:
    return {
        "agent": {
            "is_default_bot": True,
            "is_strict_mode": False,
            "custom_base_role": "",
        }
    }


@pytest.fixture
def benchmark_config() -> dict:
    return {
        "agent": {
            "is_default_bot": False,
            "is_strict_mode": False,
            "custom_base_role": (
                "Ты — тестовый ассистент для проверки MCP и соблюдения инструкций."
            ),
        }
    }


def _section_positions(prompt: str) -> list[int]:
    return [prompt.index(header) for header in PROMPT_SECTION_HEADERS]


def test_template_contains_four_sections_in_order():
    """Шаблон system_master.txt содержит 4 секции в строгом порядке."""
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    positions = _section_positions(raw)
    assert positions == sorted(positions)
    assert len(positions) == 4


def test_render_default_bot_with_tools(default_bot_config):
    """render_system_prompt: default bot + tools — все секции и negation."""
    rendered = render_system_prompt(default_bot_config, PROMPT_PATH, has_tools=True)

    assert _section_positions(rendered) == sorted(_section_positions(rendered))
    assert "knowledge_base_search" in rendered
    assert "НЕ использовать" in rendered
    assert "STOP при ошибке" in rendered
    assert "[POLICY_OK]" in rendered
    assert "НЕ вызываешь инструменты вне списка Секции 2" in rendered


def test_render_benchmark_with_tools(benchmark_config):
    """render_system_prompt: бенчмарк — canonical tools, decoy только в «НЕ использовать»."""
    rendered = render_system_prompt(benchmark_config, PROMPT_PATH, has_tools=True)

    assert "get_policy_fact" in rendered
    assert "НЕ использовать: `decoy_policy_fact`" in rendered
    for decoy in DECOY_TOOLS:
        assert f"**{decoy}**" not in rendered
    assert "STOP при ошибке" in rendered
    assert benchmark_config["agent"]["custom_base_role"] in rendered


def test_registry_canonical_tools_match_benchmark_prompt(benchmark_config):
    """Имена canonical-инструментов из registry совпадают с рендером промпта."""
    registry = load_registry_tools()
    canonical = get_canonical_tool_names(registry)
    rendered = render_system_prompt(benchmark_config, PROMPT_PATH, has_tools=True)

    assert canonical, "registry must expose canonical tools"
    for name in canonical:
        assert f"**{name}**" in rendered, f"missing canonical tool section: {name}"

    for name in registry:
        if is_decoy_tool(name):
            assert f"**{name}**" not in rendered, f"decoy must not be a tool section: {name}"


def test_render_without_tools_strict_mode(default_bot_config):
    """render_system_prompt: режим без tools — контекст и strict."""
    default_bot_config["agent"]["is_strict_mode"] = True
    rendered = render_system_prompt(default_bot_config, PROMPT_PATH, has_tools=False)

    assert "Инструментов нет" in rendered
    assert "строгом режиме" in rendered
    assert "knowledge_base_search" not in rendered


def test_render_includes_additional_instructions(default_bot_config):
    """render_system_prompt подставляет additional_instructions_section."""
    default_bot_config["agent"]["additional_instructions_section"] = "EXTRA_RULE_XYZ"
    rendered = render_system_prompt(default_bot_config, PROMPT_PATH, has_tools=True)
    assert "EXTRA_RULE_XYZ" in rendered


def test_messages_to_langchain_prepends_system_prompt():
    """messages_to_langchain добавляет SystemMessage с полным промптом."""
    system = "СЕКЦИЯ 1. РОЛЬ И ОБЛАСТЬ ДЕЙСТВИЯ\nТест."
    lc = messages_to_langchain([{"role": "user", "content": "Привет"}], system)
    assert lc[0].type == "system"
    assert lc[0].content == system
    assert lc[1].type == "human"
