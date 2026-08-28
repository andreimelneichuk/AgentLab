"""Tier-1 детектор молчаливого пропуска факта (omission) + Tier-2 self-refine.

Контекст: техники 11/12/13 ловят FABRICATION — агент УТВЕРЖДАЕТ что-то
неверное (decoy tool, false success, untraceable marker, [POLICY_OK] без
факта). Этот модуль ловит другой режим отказа context degradation —
OMISSION: агент не говорит ничего неверного, он просто МОЛЧА не включает в
ответ конкретное значение, которое пользователь просит напомнить/уточнить
и которое было установлено раньше в разговоре (EMP_ID, POLICY_FACT=,
маркер вида violet-42, названное предпочтение и т.п.).

Источник техники: emergentmind.com, статья про context degradation,
раздел "Dynamic Prompting and Self-Refinement" — модель перепроверяет
свой черновик против исходного вопроса/контекста и переписывает ответ,
если обнаруживает, что не ответила по существу.

Tier-1 (этот файл, `detect_likely_omission`): бесплатная, детерминированная
проверка по трём условиям одновременно:
  (a) сообщение пользователя похоже на recall/reference-вопрос
      (местоимение + вопросительная форма, ИЛИ явная recall-фраза
      "напомни", "какой у меня", "что я говорил" и т.п.);
  (b) в истории диалога есть конкретное значение (marker/ID/число/
      предпочтение), которое по пересечению ключевых слов с текущим
      вопросом похоже на ожидаемый ответ;
  (c) это значение НЕ встречается в черновике ответа.

Только если все три условия выполнены — сигнал triggered=True, и
agent_core.py делает ОДИН дополнительный LLM-вызов (Tier-2,
`build_refine_prompt`), передавая модели её же черновик, вопрос
пользователя и явное указание на пропущенный факт, прося переписать
ответ полностью.

v1: откалибровано на реальном каталоге (см. verify_omission_recall.py) —
подробности и итоговые offline-числа в IMPLEMENTATION.md. Ориентир по
духу: improvements/10-subtask-isolation/variant/orchestrator.py::is_recall_turn()
— тот эксперимент явно ОТКЛОНИЛ чистую pronoun-эвристику (давала
~11-18% false positive на своей задаче — решении "нужен ли вообще tool
call"). Здесь задача другая (нужен ли ДОПОЛНИТЕЛЬНЫЙ LLM-проход после
черновика, а не блокировка tool call), поэтому местоимение разрешено —
но только как один из двух путей в условие (a), и только вместе с
жёсткими условиями (b)+(c), которые сами по себе редко выполняются
одновременно на не-recall ходах. Это и держит false positive rate низким
без отказа от местоимений вовсе.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Условие (a): сообщение похоже на recall/reference-вопрос.
# ---------------------------------------------------------------------------

# Явные recall-маркеры, извлечённые из реальных формулировок каталога
# (benchmark/scenarios/catalog/s14_drift_recall.yaml, memory_recall.yaml,
# s12_drift_multi.yaml) — сильный сигнал сам по себе, вопросительная форма
# не обязательна.
_EXPLICIT_RECALL_PHRASES = (
    "напомни",
    "напомните",
    "помнишь",
    "из памяти",
    "без инструмент",
    "без tool",
    "не вызывай",
    "не вызыва",
    "в начале разговора",
    "в самом начале",
    "с самого начала",
    "самого начала",
    "первой фазы",
    "что я говорил",
    "что я сказал",
    "какой у меня",
    "какая у меня",
    "мы говорили",
    "мы обсуждали",
    "мы искали",
    "мы получили",
    "мы смотрели",
    "мы запрашивали",
    "ты говорил",
    "вы говорили",
    "ранее мы",
    "мы ранее",
)

_STEP_REFERENCE_RE = re.compile(r"шаг[а-я]*\s*\d+|на первом шаге", re.IGNORECASE)

# Местоимения/анафора — слабый сигнал сам по себе (см. docstring выше),
# используется ТОЛЬКО в паре с вопросительной формой сообщения.
_PRONOUN_RE = re.compile(
    r"\b(он|она|оно|его|её|ее|него|неё|нее|ему|нему|ей|ней|них|их|"
    r"это|тот|та|то|такой|такая|такое|таком|такому|той|тому)\b",
    re.IGNORECASE,
)

# Определительный вопрос ("что такое SKU?", "что значит TIER?") — общая
# просьба объяснить термин, а не напомнить установленный факт. "такое"
# формально попадает в _PRONOUN_RE, поэтому без этого guard "Что такое
# SKU?" ошибочно читался бы как recall через pronoun+question путь.
_GENERIC_DEFINITION_RE = re.compile(r"\bчто\b.{0,15}\b(такое|значит)\b", re.IGNORECASE)

_QUESTION_WORD_RE = re.compile(
    r"\b(какой|какая|какое|каком|каков|сколько|кто|что|куда|где)\b",
    re.IGNORECASE,
)

# Первое лицо в вопросе ("какой У МЕНЯ...", "что Я говорил", "мой/моя X") —
# ещё один сигнал recall-намерения, отдельный от местоимений 3-го лица
# (_PRONOUN_RE), нужен для вопросов вида "Какой язык я предпочитаю?", где
# отсылка идёт не через местоимение-анафору, а через собственное лицо
# говорящего.
_SELF_REFERENCE_RE = re.compile(
    r"я\s+(предпочита|говорил|сказал|просил|указыва|называ)|"
    r"у\s+меня\b|\bмой\b|\bмоя\b|\bмоё\b|\bмои\b",
    re.IGNORECASE,
)


def _is_question_form(message: str) -> bool:
    return "?" in message or bool(_QUESTION_WORD_RE.search(message))


def _looks_like_recall_question(message: str) -> bool:
    """Условие (a): recall/reference-намерение сообщения пользователя."""
    if not message:
        return False
    if any(phrase in message.lower() for phrase in _EXPLICIT_RECALL_PHRASES):
        return True
    if _STEP_REFERENCE_RE.search(message):
        return True
    if _GENERIC_DEFINITION_RE.search(message):
        return False
    if _is_question_form(message) and (_PRONOUN_RE.search(message) or _SELF_REFERENCE_RE.search(message)):
        return True
    return False


# ---------------------------------------------------------------------------
# Условие (b): значение, установленное раньше в истории, похожее на ожидаемый
# ответ (по пересечению ключевых слов с текущим вопросом).
# ---------------------------------------------------------------------------

# KEY=VALUE / key=value маркеры (EMP_ID=, POLICY_FACT=, TIER=, REGION=,
# AMOUNT=, price_usd=, TICKET_ID=, BENCH_MARKER_STREAMABLE= и т.п.) —
# см. benchmark/mcp_tool_registry.py, тот же формат что symbolic_precheck.py
# использует для marker traceability.
_KEYVALUE_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]{1,30})\s*=\s*([^\s,;]+)")

# word-digit маркер вида violet-42, EMP-4242, C-GOLD-001, INV-ANCHOR-001.
_WORD_MARKER_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+){1,3}\b")

# "результат X = 4200" / "= 4200" — числовой результат вычисления.
_EQUALS_NUMBER_RE = re.compile(r"=\s*(-?\d[\d,]*\.?\d*)\b")

# Именованное предпочтение: "я предпочитаю Python, не Java."
_PREFERENCE_RE = re.compile(
    r"предпочита[юе]\s+([A-Za-zА-Яа-яЁё0-9_.+#-]+)", re.IGNORECASE,
)

# employee_id, названный явно текстом (не key=value формат).
_EMPLOYEE_ID_RE = re.compile(r"employee_id[:\s]+([A-Za-z0-9-]+)", re.IGNORECASE)

# Дата ISO-формата — часто сам является "ключевым словом" для overlap.
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

# Ключевые слова для проверки пересечения (proper nouns, ID-коды, даты,
# многозначные числа) — намеренно НЕ включает общие короткие слова, чтобы
# overlap не срабатывал на случайных совпадениях союзов/местоимений.
_KEYWORD_RE = re.compile(
    r"\b[A-ZА-Я][a-zA-Zа-яА-Я]{2,}\b"       # Alice, Johnson, Engineering...
    r"|\b[A-Za-z]{1,6}-[A-Za-z0-9-]{1,20}\b"  # C-GOLD-001, violet-42, EMP-4242
    r"|\b\d{4}-\d{2}-\d{2}\b"                 # 2024-01-01
    r"|\b\d{2,}\b"                              # 4200, 100, 42
)

_STOPWORD_KEYWORDS = {"это", "или", "если", "the", "and", "для", "что", "какой", "какая", "какое"}

# Смысловые "якоря" для случаев без общего proper noun/ID между вопросом и
# контекстом (например "Я предпочитаю Python" → "Какой язык я предпочитаю?" —
# нет общего капитализированного слова/ID, но есть общий смысловой якорь
# "предпочита"). Список намеренно короткий и специфичный — не общий NLP
# semantic-similarity, просто явный root-overlap для двух известных паттернов
# из benchmark/scenarios/memory_recall.yaml.
_ANCHOR_ROOTS = ("предпочита", "employee_id")


@dataclass
class OmissionCandidate:
    value: str
    source_context: str


def _extract_keywords(text: str) -> set:
    return {
        m.group(0).lower()
        for m in _KEYWORD_RE.finditer(text or "")
        if m.group(0).lower() not in _STOPWORD_KEYWORDS
    }


def _message_text(msg: Dict[str, Any]) -> str:
    return str(msg.get("content") or "")


def _strip_trailing_punct(value: str) -> str:
    return value.rstrip(".,;:!?)»\"'")


def _extract_candidates_from_text(text: str) -> List[str]:
    """Возвращает список кандидатов-значений, найденных в одном сообщении."""
    values: List[str] = []
    for m in _KEYVALUE_RE.finditer(text):
        values.append(f"{m.group(1)}={_strip_trailing_punct(m.group(2))}")
    for m in _WORD_MARKER_RE.finditer(text):
        values.append(_strip_trailing_punct(m.group(0)))
    for m in _EQUALS_NUMBER_RE.finditer(text):
        values.append(_strip_trailing_punct(m.group(1)))
    for m in _PREFERENCE_RE.finditer(text):
        values.append(_strip_trailing_punct(m.group(1)))
    for m in _EMPLOYEE_ID_RE.finditer(text):
        values.append(_strip_trailing_punct(m.group(1)))
    for m in _DATE_RE.finditer(text):
        values.append(m.group(0))
    return values


def _has_overlap(user_message: str, text: str) -> bool:
    user_keywords = _extract_keywords(user_message)
    context_keywords = _extract_keywords(text)
    if user_keywords & context_keywords:
        return True
    lowered_user = user_message.lower()
    lowered_text = text.lower()
    return any(root in lowered_user and root in lowered_text for root in _ANCHOR_ROOTS)


def _find_candidates(user_message: str, history: List[Dict[str, Any]]) -> List[OmissionCandidate]:
    """Условие (b): кандидаты из истории, чей контекст пересекается по
    ключевым словам (или явному смысловому якорю) с текущим вопросом."""
    candidates: List[OmissionCandidate] = []
    seen_values = set()
    for msg in history:
        role = msg.get("role")
        if role not in ("assistant", "tool", "user"):
            continue
        text = _message_text(msg)
        if not text:
            continue
        if not _has_overlap(user_message, text):
            continue
        for value in _extract_candidates_from_text(text):
            if not value or len(value) < 2:
                continue
            key = value.lower()
            if key in seen_values:
                continue
            seen_values.add(key)
            candidates.append(OmissionCandidate(value=value, source_context=text))
    return candidates


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------


@dataclass
class OmissionSignal:
    """Результат Tier-1 проверки на молчаливый пропуск факта."""

    triggered: bool
    expected_value: Optional[str] = None
    source_context: Optional[str] = None
    reason: str = ""


def detect_likely_omission(
    user_message: str,
    history: List[Dict[str, Any]],
    draft_answer: str,
) -> OmissionSignal:
    """Tier-1: True-подобный сигнал, если похоже что draft_answer молча
    пропустил факт, который пользователь просит напомнить.

    Возвращает OmissionSignal(triggered=False) в подавляющем большинстве
    ходов — срабатывание требует ОДНОВРЕМЕННО recall-намерения (a),
    найденного в истории кандидата, релевантного вопросу по пересечению
    ключевых слов (b), и отсутствия этого значения в черновике (c).
    """
    if not _looks_like_recall_question(user_message):
        return OmissionSignal(triggered=False, reason="not_a_recall_question")

    candidates = _find_candidates(user_message, history or [])
    if not candidates:
        return OmissionSignal(triggered=False, reason="no_candidate_value_in_history")

    draft_lower = (draft_answer or "").lower()
    for candidate in candidates:
        if _candidate_present_in_draft(candidate.value, draft_lower):
            continue
        return OmissionSignal(
            triggered=True,
            expected_value=candidate.value,
            source_context=candidate.source_context,
            reason="candidate_value_missing_from_draft",
        )

    return OmissionSignal(triggered=False, reason="all_candidate_values_present")


def _candidate_present_in_draft(value: str, draft_lower: str) -> bool:
    """True если значение (или, для KEY=VALUE кандидатов, хотя бы его
    значимая часть после '=') уже встречается в черновике.

    KEY=VALUE-кандидаты (TIER=gold, EMP_ID=HR-1234) приходят из служебного
    формата tool-ответов; реальный связный ответ модели часто пересказывает
    голое значение без исходного ключа ("у него тир gold"), это НЕ пропуск
    факта. Проверяем обе формы, чтобы не дёргать Tier-2 впустую.
    """
    lowered = value.lower()
    if lowered in draft_lower:
        return True
    if "=" in value:
        _, _, bare_value = value.partition("=")
        bare_value = bare_value.strip().lower()
        # Word-boundary match, excluding hyphen-adjacency -- a plain \b
        # check would still let "gold" match inside "c-gold-001" (hyphens
        # count as word boundaries too).
        if bare_value and re.search(
            rf"(?<![\w-]){re.escape(bare_value)}(?![\w-])", draft_lower,
        ):
            return True
    return False


def build_refine_prompt(user_message: str, draft_answer: str, signal: OmissionSignal) -> str:
    """Tier-2 re-prompt: просит модель переписать её же черновик, указывая
    явно на конкретный факт/контекст, который похоже был пропущен."""
    return (
        "Ты уже подготовил(а) черновик ответа на вопрос пользователя, но, "
        "похоже, забыл(а) упомянуть в нём конкретное значение, установленное "
        "раньше в этом разговоре.\n\n"
        f"Вопрос пользователя: {user_message}\n\n"
        f"Твой черновик ответа:\n{draft_answer}\n\n"
        "Вот фрагмент более раннего сообщения этого диалога, где нужное "
        "значение было установлено:\n"
        f"{signal.source_context}\n\n"
        f"Обрати особое внимание на значение: {signal.expected_value!r} — "
        "похоже, черновик его не содержит, хотя пользователь именно его и "
        "просит напомнить/уточнить.\n\n"
        "Перепиши ответ полностью с учётом этого — включи пропущенное "
        "значение явно, сохранив остальной смысл черновика. Ответь только "
        "финальным текстом ответа пользователю, без пояснений о том, что "
        "ты его исправил(а)."
    )
