"""Тесты для recall_refine: Tier-1 детектор молчаливого пропуска факта (omission)."""
from __future__ import annotations

from recall_refine import (
    OmissionSignal,
    build_refine_prompt,
    detect_likely_omission,
)


def _hist(role: str, content: str) -> dict:
    return {"role": role, "content": content}


class TestPositiveRecallOmission:
    """Ходы, где detect_likely_omission() ДОЛЖЕН сработать."""

    def test_explicit_recall_phrase_with_marker(self):
        history = [
            _hist("user", "Найди сотрудника Alice Johnson"),
            _hist("assistant", "Нашёл: EMP_ID=HR-1234, Alice Johnson, Engineering."),
        ]
        draft = "Alice Johnson работает в отделе Engineering."
        signal = detect_likely_omission(
            "Напомни EMP_ID Alice Johnson — мы её искали в начале разговора. Не вызывай employee_lookup снова.",
            history,
            draft,
        )
        assert signal.triggered
        assert signal.expected_value == "EMP_ID=HR-1234"

    def test_pronoun_plus_question_with_marker(self):
        history = [
            _hist("assistant", "Клиент CUSTOMER=C-GOLD-001, TIER=gold, REGION=EMEA."),
        ]
        draft = "Клиент CUSTOMER=C-GOLD-001, но подробности сейчас не под рукой."
        signal = detect_likely_omission(
            "Клиент C-GOLD-001 — какой у него тир и регион?",
            history,
            draft,
        )
        assert signal.triggered
        assert signal.expected_value in ("TIER=gold", "REGION=EMEA")

    def test_preference_recall(self):
        history = [
            _hist("user", "Я предпочитаю Python, не Java."),
            _hist("assistant", "Хорошо, буду использовать Python."),
        ]
        draft = "Окей, учту твои пожелания по языку."
        signal = detect_likely_omission(
            "Какой язык программирования я предпочитаю?",
            history,
            draft,
        )
        assert signal.triggered
        assert signal.expected_value.lower() == "python"

    def test_calc_result_recall(self):
        history = [
            _hist("user", "Посчитай 100 * 42"),
            _hist("assistant", "100 * 42 = 4200"),
        ]
        draft = "Результат вычисления был получен ранее."
        signal = detect_likely_omission(
            "Результат расчёта 100 * 42 из начала беседы, шаг 10?",
            history,
            draft,
        )
        assert signal.triggered
        assert signal.expected_value == "4200"


class TestNegativeControls:
    """Ходы, которые ТОЛЬКО ПОХОЖИ на recall, но не должны триггерить."""

    def test_value_already_present_in_draft_not_flagged(self):
        history = [_hist("assistant", "EMP_ID=HR-1234, Alice Johnson.")]
        draft = "Напоминаю: EMP_ID=HR-1234."
        signal = detect_likely_omission(
            "Напомни EMP_ID Alice Johnson.", history, draft,
        )
        assert not signal.triggered

    def test_pronoun_new_entity_no_history_match(self):
        """Местоимение указывает на СОВСЕМ новую сущность — в истории нет
        относящегося к ней значения, overlap keywords не найдётся."""
        history = [_hist("assistant", "EMP_ID=HR-1234, Alice Johnson, Engineering.")]
        draft = "Не знаю, где он живёт."
        signal = detect_likely_omission(
            "Он живёт в Москве?", history, draft,
        )
        assert not signal.triggered

    def test_no_recall_intent_plain_new_question(self):
        history = [_hist("assistant", "EMP_ID=HR-1234, Alice Johnson.")]
        draft = "Bob Smith работает в отделе Sales."
        signal = detect_likely_omission(
            "Найди сотрудника Bob Smith", history, draft,
        )
        assert not signal.triggered

    def test_recall_phrase_but_empty_history(self):
        signal = detect_likely_omission("Напомни, что я говорил?", [], "Не помню.")
        assert not signal.triggered

    def test_pronoun_without_question_form_not_flagged(self):
        history = [_hist("assistant", "EMP_ID=HR-1234, Alice Johnson.")]
        # Местоимение есть, но это не вопрос — по условию (a) не должно
        # засчитываться как recall-намерение чисто по местоимению.
        signal = detect_likely_omission(
            "Это было интересно.", history, "Согласен, было интересно.",
        )
        assert not signal.triggered

    def test_smalltalk_not_flagged(self):
        history = [_hist("assistant", "EMP_ID=HR-1234, Alice Johnson.")]
        signal = detect_likely_omission("Привет! Как дела?", history, "Привет! Всё хорошо.")
        assert not signal.triggered


class TestBuildRefinePrompt:
    def test_includes_question_draft_and_expected_value(self):
        signal = OmissionSignal(
            triggered=True,
            expected_value="EMP_ID=HR-1234",
            source_context="Нашёл: EMP_ID=HR-1234, Alice Johnson.",
            reason="candidate_value_missing_from_draft",
        )
        prompt = build_refine_prompt("Напомни EMP_ID Alice Johnson", "Alice работает в Engineering.", signal)
        assert "Напомни EMP_ID Alice Johnson" in prompt
        assert "Alice работает в Engineering." in prompt
        assert "EMP_ID=HR-1234" in prompt
