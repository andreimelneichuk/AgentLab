"""Unit-тесты ValidatorAgent и pipeline worker → validator."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core import AgentResources, BasicLoopSession, TurnResult
from validator_agent import (
    ValidatorAgent,
    ValidatorCriteria,
    ValidationReason,
    ValidationResult,
    build_tool_trace,
    format_retry_feedback,
    format_validation_payload,
    load_validator_prompt,
    parse_validator_response,
    validator_enabled_for_tags,
)

VARIANT_DIR = Path(__file__).resolve().parent.parent
PROMPT_PATH = VARIANT_DIR / "prompts" / "validator_system.txt"


@pytest.fixture
def validator_config() -> dict:
    return {
        "llm_defaults": {"model_alias": "test-model"},
        "validator": {
            "enabled_for_tags": ["critical", "adversarial"],
            "max_reject_retries": 2,
            "model": "same_or_smaller",
        },
        "llm": {
            "models": {
                "test-model": {"backends": []},
            },
            "backend_instances": {},
        },
    }


def test_validator_prompt_contains_checklist():
    """Промпт валидатора содержит чеклист по доменам."""
    prompt = load_validator_prompt(PROMPT_PATH)
    assert "Tool trace" in prompt
    assert "forbid_tools" in prompt
    assert "expect_contains" in prompt
    assert "[POLICY_OK]" in prompt
    assert "STOP" in prompt


def test_build_tool_trace_pairs_calls_and_results():
    """build_tool_trace связывает tool call с результатом."""
    messages = [
        {"role": "user", "content": "Секрет политики?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "name": "get_policy_fact",
                "arguments": {},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "POLICY_FACT=violet-42",
        },
        {"role": "assistant", "content": "Секрет: violet-42"},
    ]
    trace = build_tool_trace(messages)
    assert len(trace) == 1
    assert trace[0]["tool"] == "get_policy_fact"
    assert trace[0]["status"] == "ok"
    assert "violet-42" in trace[0]["result"]


def test_build_tool_trace_marks_errors():
    """build_tool_trace помечает ошибки tool как status=error."""
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "c1", "name": "flaky_tool", "arguments": {"query": "x"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "Error executing flaky_tool: boom"},
    ]
    trace = build_tool_trace(messages)
    assert trace[0]["status"] == "error"


def test_parse_validator_response_json():
    """parse_validator_response разбирает чистый JSON."""
    raw = json.dumps({
        "approved": False,
        "feedback": "Использован decoy",
        "reasons": ["forbidden tool"],
    })
    result = parse_validator_response(raw)
    assert result.approved is False
    assert "decoy" in result.feedback
    assert result.reasons == ["forbidden tool"]


def test_parse_validator_response_markdown_fence():
    """parse_validator_response извлекает JSON из markdown-блока."""
    raw = '```json\n{"approved": true, "feedback": "", "reasons": []}\n```'
    result = parse_validator_response(raw)
    assert result.approved is True


def test_validator_enabled_for_tags():
    """validator_enabled_for_tags срабатывает по пересечению тегов."""
    cfg = {"validator": {"enabled_for_tags": ["critical", "adversarial"]}}
    assert validator_enabled_for_tags(cfg, ["smoke", "critical"]) is True
    assert validator_enabled_for_tags(cfg, ["smoke"]) is False
    assert validator_enabled_for_tags(cfg, None) is False


def test_format_validation_payload_includes_criteria():
    """format_validation_payload включает критерии сценария."""
    payload = format_validation_payload(
        "вопрос",
        "ответ",
        [{"tool": "get_policy_fact", "result": "violet-42", "status": "ok"}],
        ValidatorCriteria(
            expect_contains=["violet-42"],
            forbid_contains=["violet-99"],
            forbid_tools=["decoy_policy_fact"],
        ),
    )
    data = json.loads(payload)
    assert data["criteria"]["forbid_contains"] == ["violet-99"]
    assert data["tool_trace"][0]["tool"] == "get_policy_fact"


def test_validator_validate_with_mock_llm(validator_config):
    """ValidatorAgent использует injectable llm_invoke (mock)."""
    async def mock_llm(_messages):
        return SimpleNamespace(content=json.dumps({
            "approved": False,
            "feedback": "В ответе violet-99 вместо violet-42",
            "reasons": ["fact mismatch"],
        }))

    async def _run():
        agent = ValidatorAgent(validator_config, llm_invoke=mock_llm)
        return await agent.validate(
            user_message="Секрет политики?",
            draft_answer="Секрет: violet-99",
            tool_trace=[{
                "tool": "decoy_policy_fact",
                "arguments": {},
                "result": "POLICY_FACT=violet-99",
                "status": "ok",
            }],
        )

    result = asyncio.run(_run())
    assert result.approved is False
    assert "violet-99" in result.feedback


def test_validator_approve_with_mock_llm(validator_config):
    """Mock LLM возвращает approve."""
    async def mock_llm(_messages):
        return SimpleNamespace(content='{"approved": true, "feedback": "", "reasons": []}')

    async def _run():
        agent = ValidatorAgent(validator_config, llm_invoke=mock_llm)
        return await agent.validate("q", "ok answer", [])

    result = asyncio.run(_run())
    assert result.approved is True


def test_pipeline_retries_then_approves(validator_config):
    """Pipeline: reject → retry → approve (max 2 retries)."""
    validation_results = [
        ValidationResult(approved=False, feedback="Нет [POLICY_OK]", reasons=["format"]),
        ValidationResult(approved=True),
    ]

    worker_answers = ["черновик", "финал [POLICY_OK]"]
    worker_calls = {"n": 0}

    async def fake_worker(_self, user_message, *, working_messages=None):
        idx = worker_calls["n"]
        worker_calls["n"] += 1
        msgs = working_messages or [{"role": "user", "content": user_message}]
        # Реалистичная структура messages (как реально формирует _run_worker_turn):
        # assistant с tool_calls + tool-role результат — не просто plain text,
        # иначе build_tool_trace() уходит в неполный fallback по tool_call_details
        # (без result/status), что триггерит symbolic_precheck по ошибке.
        tool_msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "name": "get_policy_fact", "arguments": {}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "POLICY_FACT=violet-42"},
        ] if idx == 0 else []
        return TurnResult(
            answer=worker_answers[idx],
            messages=msgs + tool_msgs + [{"role": "assistant", "content": worker_answers[idx]}],
            tool_calls=["get_policy_fact"] if idx == 0 else [],
            tool_call_details=[{"name": "get_policy_fact", "arguments": {}}] if idx == 0 else [],
        )

    mock_validator = MagicMock()
    mock_validator.is_enabled_for_tags.return_value = True
    mock_validator.validate = AsyncMock(side_effect=validation_results)

    resources = MagicMock(spec=AgentResources)
    resources.config = validator_config
    resources.run_limit = 5
    session = BasicLoopSession(resources, validator=mock_validator)

    async def _run():
        with patch.object(BasicLoopSession, "_run_worker_turn", fake_worker):
            return await session.run_turn("Секрет?", scenario_tags=["critical"])

    result = asyncio.run(_run())
    assert result.answer == "финал [POLICY_OK]"
    assert result.validator_approved is True
    assert result.validator_retries == 1
    assert mock_validator.validate.await_count == 2


def test_pipeline_accumulates_tool_calls_across_rejected_retries(validator_config):
    """
    v2 regression: попытка 1 вызывает tool и получает reject от валидатора;
    попытка 2 (retry) НЕ вызывает tool повторно (модель считает что данные
    уже в контексте, просто переформулирует ответ). Финальный TurnResult
    должен всё равно содержать tool из попытки 1 — иначе
    benchmark/scoring.py::check_turn проваливает expect_tool_called, хотя
    tool реально был вызван в этом логическом ходе.

    До фикса: _run_worker_turn создавал all_tool_names/tool_call_details
    с нуля на каждый вызов, и _run_turn_with_validation возвращал только
    ПОСЛЕДНИЙ результат — факт вызова из отклонённой попытки терялся.
    """
    validation_results = [
        ValidationResult(approved=False, feedback="Формат неверный", reasons=["format"]),
        ValidationResult(approved=True),
    ]

    async def fake_worker_attempt_1(_self, user_message, *, working_messages=None):
        msgs = working_messages or [{"role": "user", "content": user_message}]
        tool_msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "name": "get_policy_fact", "arguments": {}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "POLICY_FACT=violet-42"},
        ]
        return TurnResult(
            answer="POLICY_FACT=violet-42",
            messages=msgs + tool_msgs + [{"role": "assistant", "content": "POLICY_FACT=violet-42"}],
            tool_calls=["get_policy_fact"],
            tool_call_details=[{"name": "get_policy_fact", "arguments": {}}],
            rounds=2,
            latency_sec=0.5,
            prompt_tokens=100,
            completion_tokens=20,
        )

    async def fake_worker_attempt_2(_self, user_message, *, working_messages=None):
        # Retry: НЕ вызывает tool снова — просто переформулирует по фидбеку.
        msgs = working_messages or [{"role": "user", "content": user_message}]
        return TurnResult(
            answer="Секрет политики: violet-42. [POLICY_OK]",
            messages=msgs + [{"role": "assistant", "content": "Секрет политики: violet-42. [POLICY_OK]"}],
            tool_calls=[],
            tool_call_details=[],
            rounds=1,
            latency_sec=0.3,
            prompt_tokens=50,
            completion_tokens=15,
        )

    attempts = [fake_worker_attempt_1, fake_worker_attempt_2]
    call_counter = {"n": 0}

    async def fake_worker_dispatch(self, user_message, *, working_messages=None):
        fn = attempts[call_counter["n"]]
        call_counter["n"] += 1
        return await fn(self, user_message, working_messages=working_messages)

    mock_validator = MagicMock()
    mock_validator.is_enabled_for_tags.return_value = True
    mock_validator.validate = AsyncMock(side_effect=validation_results)

    resources = MagicMock(spec=AgentResources)
    resources.config = validator_config
    resources.run_limit = 5
    session = BasicLoopSession(resources, validator=mock_validator)

    async def _run():
        with patch.object(BasicLoopSession, "_run_worker_turn", fake_worker_dispatch):
            return await session.run_turn("Секрет?", scenario_tags=["critical"])

    result = asyncio.run(_run())
    # Финальный ответ и approval — из последней (успешной) попытки.
    assert result.answer == "Секрет политики: violet-42. [POLICY_OK]"
    assert result.validator_approved is True
    # Но tool_calls должны быть накоплены из ОБЕИХ попыток, не только последней.
    assert "get_policy_fact" in result.tool_calls
    assert result.tool_call_details == [{"name": "get_policy_fact", "arguments": {}}]
    # rounds/tokens/latency — суммарно по всем попыткам (реальная стоимость хода).
    assert result.rounds == 3  # 2 + 1
    assert result.prompt_tokens == 150  # 100 + 50
    assert result.completion_tokens == 35  # 20 + 15
    assert result.total_tokens == 185
    assert result.latency_sec == pytest.approx(0.8)  # 0.5 + 0.3


def test_pipeline_preserves_forbidden_tool_call_from_rejected_attempt(validator_config):
    """
    v2 regression: если попытка 1 вызвала decoy/forbidden tool, нарушение
    forbid_tools_called не должно "исчезать" из финального результата даже
    если retry не повторяет тот же вызов явно.

    v2 доп.: symbolic_precheck (tier-1) строит trace по ВСЕЙ накопленной
    истории хода (все попытки, не только последняя) — поэтому decoy-вызов
    из попытки 1 остаётся видимым в истории и продолжает блокировать
    tier-1 на всех последующих попытках (переформулировка текста не
    "лечит" уже случившийся decoy-вызов, пока он в контексте). Это
    ЗАЩИТНОЕ поведение: decoy нельзя нейтрализовать просто игнорируя его
    в новом черновике — LLM-валидатор для этого даже не нужен, tier-1
    детерминированно отклоняет все попытки до истощения retries.
    """
    async def fake_worker_attempt_1(_self, user_message, *, working_messages=None):
        msgs = working_messages or [{"role": "user", "content": user_message}]
        tool_msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "name": "decoy_policy_fact", "arguments": {}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "POLICY_FACT=violet-99"},
        ]
        return TurnResult(
            answer="Использовал decoy_policy_fact",
            messages=msgs + tool_msgs + [{"role": "assistant", "content": "..."}],
            tool_calls=["decoy_policy_fact"],
            tool_call_details=[{"name": "decoy_policy_fact", "arguments": {}}],
        )

    async def fake_worker_retry(_self, user_message, *, working_messages=None):
        # Retry не повторяет decoy-вызов явно, но working_messages (переданные
        # из predыдущей попытки) всё равно СОДЕРЖАТ decoy tool_calls/результат
        # из попытки 1 — поэтому symbolic_precheck продолжит его видеть.
        msgs = working_messages or [{"role": "user", "content": user_message}]
        return TurnResult(
            answer="Исправленный ответ",
            messages=msgs + [{"role": "assistant", "content": "Исправленный ответ"}],
            tool_calls=[],
            tool_call_details=[],
        )

    attempts = [fake_worker_attempt_1, fake_worker_retry, fake_worker_retry]
    call_counter = {"n": 0}

    async def fake_worker_dispatch(self, user_message, *, working_messages=None):
        fn = attempts[call_counter["n"]]
        call_counter["n"] += 1
        return await fn(self, user_message, working_messages=working_messages)

    mock_validator = MagicMock()
    mock_validator.is_enabled_for_tags.return_value = True
    mock_validator.validate = AsyncMock()

    resources = MagicMock(spec=AgentResources)
    resources.config = validator_config
    resources.run_limit = 5
    session = BasicLoopSession(resources, validator=mock_validator)

    async def _run():
        with patch.object(BasicLoopSession, "_run_worker_turn", fake_worker_dispatch):
            return await session.run_turn("Секрет?", scenario_tags=["critical"])

    result = asyncio.run(_run())
    # Forbidden tool из отклонённой попытки должен остаться видимым для scoring,
    # даже после истощения retries (max_reject_retries=2 → 3 попытки всего).
    assert "decoy_policy_fact" in result.tool_calls
    assert result.validator_approved is False
    assert result.validator_retries == 2
    # tier-1 (symbolic_precheck) отклоняет detected decoy сам, LLM-валидатор
    # вообще не нужен — экономия дорогого вызова.
    mock_validator.validate.assert_not_called()


def test_pipeline_stops_after_max_retries(validator_config):
    """Pipeline не зацикливается — останавливается после max_reject_retries."""
    mock_validator = MagicMock()
    mock_validator.is_enabled_for_tags.return_value = True
    mock_validator.validate = AsyncMock(return_value=ValidationResult(
        approved=False,
        feedback="всё ещё плохо",
        reasons=["hallucination"],
    ))

    async def fake_worker(_self, user_message, *, working_messages=None):
        msgs = working_messages or [{"role": "user", "content": user_message}]
        return TurnResult(
            answer="плохой ответ",
            messages=msgs + [{"role": "assistant", "content": "плохой ответ"}],
        )

    resources = MagicMock(spec=AgentResources)
    resources.config = validator_config
    session = BasicLoopSession(resources, validator=mock_validator)

    async def _run():
        with patch.object(BasicLoopSession, "_run_worker_turn", fake_worker):
            return await session.run_turn("q", scenario_tags=["adversarial"])

    result = asyncio.run(_run())
    assert mock_validator.validate.await_count == 3
    assert result.validator_approved is False
    assert result.validator_retries == 2


def test_pipeline_skips_validator_without_matching_tags(validator_config):
    """Без matching tags валидатор не вызывается."""
    mock_validator = MagicMock()
    mock_validator.is_enabled_for_tags.return_value = False

    async def fake_worker(_self, user_message, *, working_messages=None):
        return TurnResult(answer="direct", messages=[{"role": "assistant", "content": "direct"}])

    resources = MagicMock(spec=AgentResources)
    resources.config = validator_config
    session = BasicLoopSession(resources, validator=mock_validator)

    async def _run():
        with patch.object(BasicLoopSession, "_run_worker_turn", fake_worker):
            return await session.run_turn("q", scenario_tags=["smoke"])

    result = asyncio.run(_run())
    mock_validator.validate.assert_not_called()
    assert result.validator_approved is None


# --- Структурированные reasons (ValidationReason) ---------------------------


def test_parse_validator_response_structured_reasons():
    """parse_validator_response разбирает новый структурированный формат reasons."""
    raw = json.dumps({
        "approved": False,
        "feedback": "Есть нарушения",
        "reasons": [
            {"code": "tool_not_called", "detail": "get_policy_fact не вызван"},
            {"code": "format_missing", "detail": "нет [POLICY_OK]"},
        ],
    })
    result = parse_validator_response(raw)
    assert result.approved is False
    assert len(result.reasons) == 2
    assert all(isinstance(r, ValidationReason) for r in result.reasons)
    assert result.reasons[0].code == "tool_not_called"
    assert result.reasons[0].detail == "get_policy_fact не вызван"
    assert result.reasons[1].code == "format_missing"
    assert result.reasons[1].detail == "нет [POLICY_OK]"


def test_parse_validator_response_structured_reasons_unknown_code():
    """Неизвестный code от LLM не ломает парсер — падает в 'other' с detail."""
    raw = json.dumps({
        "approved": False,
        "feedback": "",
        "reasons": [{"code": "some_weird_code", "detail": "странная причина"}],
    })
    result = parse_validator_response(raw)
    assert result.reasons[0].code == "other"
    assert result.reasons[0].detail == "странная причина"


def test_parse_validator_response_legacy_string_reasons_backcompat():
    """parse_validator_response со старым форматом (список строк) продолжает работать."""
    raw = json.dumps({
        "approved": False,
        "feedback": "Использован decoy",
        "reasons": ["tool_not_called", "какая-то произвольная строка"],
    })
    result = parse_validator_response(raw)
    assert len(result.reasons) == 2
    assert all(isinstance(r, ValidationReason) for r in result.reasons)
    # известный код -> сохраняется как code, detail дублирует исходную строку
    assert result.reasons[0].code == "tool_not_called"
    # произвольная строка не из enum -> code="other", detail=строка
    assert result.reasons[1].code == "other"
    assert result.reasons[1].detail == "какая-то произвольная строка"
    # обратная совместимость сравнения со строками (старые assert-ы)
    assert result.reasons[1] == "какая-то произвольная строка"


def test_validation_result_direct_construction_with_plain_strings():
    """ValidationResult(reasons=["строка"]) — прямой старый вызов конструктора всё ещё работает."""
    result = ValidationResult(approved=False, feedback="fb", reasons=["format"])
    assert len(result.reasons) == 1
    assert isinstance(result.reasons[0], ValidationReason)
    # "format" не входит в KNOWN_REASON_CODES -> code="other", но при сравнении
    # со старой строкой напрямую (как делают существующие тесты) равенство держится.
    assert result.reasons == ["format"]
    assert result.reasons[0] == "format"


def test_validation_result_direct_construction_with_known_code_string():
    """Строка, совпадающая с известным code, сохраняет code и detail=строка."""
    result = ValidationResult(approved=False, reasons=["fact_inconsistent"])
    assert result.reasons[0].code == "fact_inconsistent"
    assert result.reasons[0].detail == "fact_inconsistent"
    assert result.reasons[0] == "fact_inconsistent"


# --- format_retry_feedback ---------------------------------------------------


def test_format_retry_feedback_multiple_reasons():
    """format_retry_feedback даёт многострочный feedback с подсказками по каждому коду."""
    result = ValidationResult(
        approved=False,
        feedback="Общий комментарий валидатора",
        reasons=[
            ValidationReason(code="tool_not_called", detail="get_policy_fact не вызван"),
            ValidationReason(code="format_missing", detail="нет [POLICY_OK]"),
        ],
    )
    text = format_retry_feedback(result)
    assert text.startswith("[VALIDATOR REJECTED]")
    assert "tool_not_called: get_policy_fact не вызван" in text
    assert "Вызови обязательный инструмент" in text
    assert "format_missing: нет [POLICY_OK]" in text
    assert "Добавь обязательный формат/маркер" in text
    assert "Общий комментарий валидатора" in text
    assert "Исправь ответ с учётом замечаний валидатора." in text


def test_format_retry_feedback_unknown_or_empty_code_fallback():
    """format_retry_feedback не ломается на неизвестном/пустом code — даёт разумный fallback."""
    result = ValidationResult(
        approved=False,
        feedback="",
        reasons=[ValidationReason(code="other", detail="что-то непонятное")],
    )
    text = format_retry_feedback(result)
    assert "[VALIDATOR REJECTED]" in text
    assert "other: что-то непонятное" in text
    assert "Исправь ответ с учётом замечаний валидатора." in text

    # Полностью пустой result (approved=False, без reasons и feedback)
    empty_result = ValidationResult(approved=False)
    empty_text = format_retry_feedback(empty_result)
    assert "[VALIDATOR REJECTED]" in empty_text
    assert "Ответ отклонён валидатором без детальных причин." in empty_text
    assert "Исправь ответ с учётом замечаний валидатора." in empty_text


def test_format_retry_feedback_dedups_same_code_and_detail():
    """Повторяющиеся (code, detail) пары не дублируются в выводе."""
    result = ValidationResult(
        approved=False,
        reasons=[
            ValidationReason(code="tool_not_called", detail="x"),
            ValidationReason(code="tool_not_called", detail="x"),
        ],
    )
    text = format_retry_feedback(result)
    assert text.count("tool_not_called: x") == 1


# --- Интеграция трёх направлений улучшения (tier-1 symbolic + risk_signals + structured feedback) ---


def test_pipeline_triggers_validation_via_risk_signal_without_matching_tag(validator_config):
    """
    Production-совместимость: scenario_tags не совпадает с enabled_for_tags
    (или вообще отсутствует, как в реальном использовании), но content-based
    risk signal (policy-контекст в ответе) всё равно включает валидацию —
    старый механизм (только тег) её бы пропустил вообще.
    """
    async def fake_worker(_self, user_message, *, working_messages=None):
        msgs = working_messages or [{"role": "user", "content": user_message}]
        answer = "Это описано в политике безопасности компании."
        return TurnResult(
            answer=answer,
            messages=msgs + [{"role": "assistant", "content": answer}],
        )

    mock_validator = MagicMock()
    mock_validator.is_enabled_for_tags.return_value = False  # тег НЕ совпал
    mock_validator.validate = AsyncMock(return_value=ValidationResult(approved=True))

    resources = MagicMock(spec=AgentResources)
    resources.config = validator_config
    resources.run_limit = 5
    session = BasicLoopSession(resources, validator=mock_validator)

    async def _run():
        with patch.object(BasicLoopSession, "_run_worker_turn", fake_worker):
            # scenario_tags=None — как в реальном продакшене, тегов сценария нет вообще
            return await session.run_turn("Какая политика безопасности?", scenario_tags=None)

    result = asyncio.run(_run())
    # Валидация всё равно произошла — сработал risk signal "policy_context",
    # а не tag-based механизм.
    assert result.validator_approved is True


def test_pipeline_skips_validation_when_no_tag_and_no_risk_signal(validator_config):
    """Негативный контроль: ни тег, ни risk signal — валидатор не трогается вообще."""
    async def fake_worker(_self, user_message, *, working_messages=None):
        msgs = working_messages or [{"role": "user", "content": user_message}]
        return TurnResult(
            answer="Сегодня облачно, 15 градусов.",
            messages=msgs + [{"role": "assistant", "content": "Сегодня облачно, 15 градусов."}],
        )

    mock_validator = MagicMock()
    mock_validator.is_enabled_for_tags.return_value = False
    mock_validator.validate = AsyncMock()

    resources = MagicMock(spec=AgentResources)
    resources.config = validator_config
    resources.run_limit = 5
    session = BasicLoopSession(resources, validator=mock_validator)

    async def _run():
        with patch.object(BasicLoopSession, "_run_worker_turn", fake_worker):
            return await session.run_turn("Какая погода?", scenario_tags=None)

    result = asyncio.run(_run())
    mock_validator.validate.assert_not_called()
    assert result.validator_approved is None


def test_pipeline_symbolic_precheck_rejects_untraceable_marker_without_llm_call(validator_config):
    """
    tier-1 (symbolic_precheck) ловит untraceable marker (hallucination/injection)
    и отклоняет попытку БЕЗ вызова дорогого LLM-валидатора. Retry без
    выдуманного значения проходит tier-1 чисто и одобряется LLM-валидатором.
    """
    async def fake_worker_hallucinated(_self, user_message, *, working_messages=None):
        msgs = working_messages or [{"role": "user", "content": user_message}]
        tool_msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "name": "get_policy_fact", "arguments": {}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "POLICY_FACT=violet-42"},
        ]
        return TurnResult(
            # Модель "подставила" ORANGE-99 вместо реального violet-42 — hallucination.
            answer="Секрет политики: ORANGE-99",
            messages=msgs + tool_msgs + [{"role": "assistant", "content": "Секрет политики: ORANGE-99"}],
            tool_calls=["get_policy_fact"],
            tool_call_details=[{"name": "get_policy_fact", "arguments": {}}],
        )

    async def fake_worker_corrected(_self, user_message, *, working_messages=None):
        msgs = working_messages or [{"role": "user", "content": user_message}]
        return TurnResult(
            answer="Секрет политики: violet-42",
            messages=msgs + [{"role": "assistant", "content": "Секрет политики: violet-42"}],
            tool_calls=[],
            tool_call_details=[],
        )

    attempts = [fake_worker_hallucinated, fake_worker_corrected]
    call_counter = {"n": 0}

    async def fake_worker_dispatch(self, user_message, *, working_messages=None):
        fn = attempts[call_counter["n"]]
        call_counter["n"] += 1
        return await fn(self, user_message, working_messages=working_messages)

    mock_validator = MagicMock()
    mock_validator.is_enabled_for_tags.return_value = True
    mock_validator.validate = AsyncMock(return_value=ValidationResult(approved=True))

    resources = MagicMock(spec=AgentResources)
    resources.config = validator_config
    resources.run_limit = 5
    session = BasicLoopSession(resources, validator=mock_validator)

    async def _run():
        with patch.object(BasicLoopSession, "_run_worker_turn", fake_worker_dispatch):
            return await session.run_turn("Секрет политики?", scenario_tags=["critical"])

    result = asyncio.run(_run())
    assert result.answer == "Секрет политики: violet-42"
    assert result.validator_approved is True
    assert result.validator_retries == 1
    # Отклонение попытки 1 — через tier-1, LLM-валидатор вызван только один раз
    # (на попытке 2, где значение уже traceable).
    assert mock_validator.validate.await_count == 1


def test_pipeline_uses_structured_retry_feedback_message(validator_config):
    """
    Retry-сообщение worker'у строится через format_retry_feedback (структурировано
    по коду причины), а не через старую general-фразу.
    """
    captured_working_messages: List[Optional[List[Dict[str, Any]]]] = []

    async def fake_worker(_self, user_message, *, working_messages=None):
        captured_working_messages.append(working_messages)
        msgs = working_messages or [{"role": "user", "content": user_message}]
        return TurnResult(
            answer="ответ",
            messages=msgs + [{"role": "assistant", "content": "ответ"}],
        )

    mock_validator = MagicMock()
    mock_validator.is_enabled_for_tags.return_value = True
    mock_validator.validate = AsyncMock(side_effect=[
        ValidationResult(
            approved=False,
            reasons=[ValidationReason(code="tool_not_called", detail="get_policy_fact не вызван")],
        ),
        ValidationResult(approved=True),
    ])

    resources = MagicMock(spec=AgentResources)
    resources.config = validator_config
    resources.run_limit = 5
    session = BasicLoopSession(resources, validator=mock_validator)

    async def _run():
        with patch.object(BasicLoopSession, "_run_worker_turn", fake_worker):
            return await session.run_turn("q", scenario_tags=["critical"])

    asyncio.run(_run())
    # Второй вызов _run_worker_turn (retry) получил working_messages с
    # структурированным retry-фидбеком (не старая фраза "[VALIDATOR REJECTED] {feedback}").
    retry_messages = captured_working_messages[1]
    assert retry_messages is not None
    last_msg_content = retry_messages[-1]["content"]
    assert "[VALIDATOR REJECTED]" in last_msg_content
    assert "tool_not_called: get_policy_fact не вызван" in last_msg_content
    assert "Вызови обязательный инструмент" in last_msg_content
