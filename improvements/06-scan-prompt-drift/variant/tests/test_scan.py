"""Unit-тесты модуля SCAN."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_core import render_system_prompt
from scan import (
    ScanLevel,
    build_check_trigger,
    build_scan_trigger,
    format_scan_block,
    is_trivial_turn,
    max_tokens_hint,
    parse_check_output,
    parse_scan_markers,
    parse_scan_output,
    resolve_scan_level,
    select_markers_for_level,
)

SAMPLE_PROMPT = """
### ЧЕСТНОСТЬ:
Правила честности.
@@SCAN_1: Какие данные затронет задача?

### ИНСТРУМЕНТЫ:
Используй tools.
@@SCAN_2: Какой tool выбрать?
@@SCAN_3: Какой tool запрещён?

### ОШИБКИ:
STOP при ошибке.
@@SCAN_4: Вероятный сбой?

@@SCAN_5: HR/CRM ограничения?
@@SCAN_6: Формат ответа?
"""


def test_parse_scan_markers_returns_sorted_unique():
    markers = parse_scan_markers(SAMPLE_PROMPT)
    assert [m.index for m in markers] == [1, 2, 3, 4, 5, 6]
    assert "данные" in markers[0].prompt


def test_parse_scan_markers_empty_for_no_markers():
    assert parse_scan_markers("Промпт без маркеров") == []


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Привет!", True),
        ("спасибо за помощь", True),
        ("Hello there", True),
        ("Какие типы отчётов есть на платформе?", False),
        ("Вызови get_policy_fact для violet-42", False),
    ],
)
def test_is_trivial_turn(message: str, expected: bool):
    assert is_trivial_turn(message) is expected


@pytest.mark.parametrize(
    ("tags", "level"),
    [
        (["critical"], ScanLevel.FULL),
        (["long_horizon", "instruction"], ScanLevel.MINI),
        (["multi-turn"], ScanLevel.ANCHOR),
        ([], ScanLevel.MINI),
    ],
)
def test_resolve_scan_level_by_tags(tags: list[str], level: ScanLevel):
    config = {"scan": {"enabled": True, "default_level": "MINI"}}
    assert resolve_scan_level("Сложный вопрос про CRM", tags, config) == level


def test_resolve_scan_level_skip_for_trivial():
    config = {"scan": {"enabled": True}}
    assert resolve_scan_level("Привет", ["critical"], config) == ScanLevel.SKIP


def test_resolve_scan_level_disabled_in_config():
    config = {"scan": {"enabled": False}}
    assert resolve_scan_level("Сложный вопрос", ["critical"], config) == ScanLevel.SKIP


def test_resolve_scan_level_explicit_override():
    config = {"scan": {"enabled": True}}
    assert resolve_scan_level("Вопрос", ["critical"], config, explicit_level="ANCHOR") == ScanLevel.ANCHOR


def test_select_markers_for_level_full():
    markers = parse_scan_markers(SAMPLE_PROMPT)
    selected = select_markers_for_level(markers, ScanLevel.FULL)
    assert len(selected) == 6


def test_select_markers_for_level_anchor():
    markers = parse_scan_markers(SAMPLE_PROMPT)
    selected = select_markers_for_level(markers, ScanLevel.ANCHOR)
    assert len(selected) == 1
    assert selected[0].index == 1


def test_select_markers_for_level_skip():
    markers = parse_scan_markers(SAMPLE_PROMPT)
    assert select_markers_for_level(markers, ScanLevel.SKIP) == []


def test_build_scan_trigger_contains_markers_and_user_task():
    markers = parse_scan_markers(SAMPLE_PROMPT)
    trigger = build_scan_trigger(markers, "Найди политику violet-42", ScanLevel.MINI)
    assert "@@SCAN_1" in trigger
    assert "видимом output" in trigger
    assert "violet-42" in trigger
    assert "MINI" in trigger


def test_parse_scan_output_extracts_lines():
    text = "SCAN_1: Правила честности важны.\nSCAN_2: Нужен knowledge_base_search."
    parsed = parse_scan_output(text)
    assert parsed[1] == "Правила честности важны."
    assert parsed[2].startswith("Нужен")


def test_format_scan_block_preserves_order():
    block = format_scan_block({2: "два", 1: "один"}, [1, 2])
    assert block.splitlines() == ["SCAN_1: один", "SCAN_2: два"]


def test_build_check_trigger_includes_scan_and_answer():
    markers = parse_scan_markers(SAMPLE_PROMPT)[:2]
    trigger = build_check_trigger(markers, "SCAN_1: ok", "Основной ответ")
    assert "CHECK:" in trigger
    assert "MISSED:" in trigger
    assert "SCAN_1: ok" in trigger


def test_parse_check_output():
    text = "CHECK: честность ✓, источники ✓\nMISSED: нет"
    result = parse_check_output(text)
    assert "честность ✓" in result.checked[0]
    assert result.missed == ["нет"]


def test_max_tokens_hint_levels():
    assert max_tokens_hint(ScanLevel.FULL) == 300
    assert max_tokens_hint(ScanLevel.MINI) == 120
    assert max_tokens_hint(ScanLevel.ANCHOR) == 20
    assert max_tokens_hint(ScanLevel.SKIP) == 0


def test_system_master_contains_six_markers():
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "system_master.txt"
    config = {"agent": {"is_default_bot": True, "is_strict_mode": False, "custom_base_role": ""}}
    rendered = render_system_prompt(config, prompt_path, has_tools=True)
    markers = parse_scan_markers(rendered)
    assert 5 <= len(markers) <= 7
    assert markers[0].index == 1


def test_compose_visible_answer_order():
    from agent_core import BasicLoopSession

    combined = BasicLoopSession._compose_visible_answer(
        "SCAN_1: test",
        "Основной ответ",
        "CHECK: ok",
    )
    assert combined.index("SCAN_1") < combined.index("Основной")
    assert combined.index("Основной") < combined.index("CHECK")


# --- Тесты инлайн-API (v2) ---

def test_build_inline_user_message_skip_returns_original():
    from scan import build_inline_user_message, parse_scan_markers
    markers = parse_scan_markers(SAMPLE_PROMPT)
    msg = "Найди сотрудника James Park"
    assert build_inline_user_message(markers, msg, ScanLevel.SKIP) == msg


def test_build_inline_user_message_contains_markers_and_task():
    from scan import build_inline_user_message, parse_scan_markers
    markers = parse_scan_markers(SAMPLE_PROMPT)
    msg = "Найди политику violet-42"
    result = build_inline_user_message(markers, msg, ScanLevel.MINI)
    # Маркеры должны быть в тексте
    assert "@@SCAN_1" in result
    # Задача должна быть в тексте
    assert "violet-42" in result
    # Модель видит одно сообщение, а не два вызова
    assert result != msg


def test_build_inline_user_message_anchor_has_single_marker():
    from scan import build_inline_user_message, parse_scan_markers
    markers = parse_scan_markers(SAMPLE_PROMPT)
    result = build_inline_user_message(markers, "Вопрос", ScanLevel.ANCHOR)
    # ANCHOR → только первый маркер
    assert "@@SCAN_1" in result
    assert "@@SCAN_2" not in result


def test_extract_scan_from_response_returns_block():
    from scan import extract_scan_from_response, parse_scan_markers
    markers = parse_scan_markers(SAMPLE_PROMPT)
    response = "SCAN_1: Правила честности важны.\nSCAN_2: Нужен tool X.\n\nОтвет пользователю."
    block = extract_scan_from_response(response, markers, ScanLevel.MINI)
    assert "SCAN_1" in block
    assert "SCAN_2" in block


def test_extract_scan_from_response_empty_if_no_scan_lines():
    from scan import extract_scan_from_response, parse_scan_markers
    markers = parse_scan_markers(SAMPLE_PROMPT)
    response = "Просто ответ без SCAN-блока."
    assert extract_scan_from_response(response, markers, ScanLevel.MINI) == ""


def test_inline_message_task_comes_after_markers():
    from scan import build_inline_user_message, parse_scan_markers
    markers = parse_scan_markers(SAMPLE_PROMPT)
    msg = "Задача-маркер"
    result = build_inline_user_message(markers, msg, ScanLevel.FULL)
    # Задача должна идти ПОСЛЕ последнего маркера
    last_marker_pos = max(result.find(f"@@SCAN_{m.index}") for m in markers)
    assert result.find("Задача-маркер") > last_marker_pos
