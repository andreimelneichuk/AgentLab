"""Метод SCAN: восстановление веса системного промпта через генерацию ответов на маркеры."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

MARKER_PATTERN = re.compile(r"@@SCAN_(\d+):\s*(.+)", re.MULTILINE)
SCAN_RESPONSE_PATTERN = re.compile(r"^SCAN_(\d+):\s*(.+)$", re.MULTILINE)
CHECK_PATTERN = re.compile(r"^CHECK:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
MISSED_PATTERN = re.compile(r"^MISSED:\s*(.+)$", re.MULTILINE | re.IGNORECASE)

TRIVIAL_PATTERNS = (
    re.compile(r"^(привет|здравствуй|добрый\s+(день|вечер|утро)|hello|hi|hey)\b", re.I),
    re.compile(r"^(спасибо|благодарю|thanks|thank\s+you)\b", re.I),
    re.compile(r"^(пока|до\s+свидания|goodbye|bye)\b", re.I),
)


class ScanLevel(str, Enum):
    """Уровень глубины SCAN перед ходом агента."""

    FULL = "FULL"
    MINI = "MINI"
    ANCHOR = "ANCHOR"
    SKIP = "SKIP"

    @classmethod
    def from_value(cls, value: Optional[str]) -> "ScanLevel":
        if not value:
            return cls.MINI
        normalized = str(value).strip().upper()
        for level in cls:
            if level.value == normalized:
                return level
        return cls.MINI


@dataclass(frozen=True)
class ScanMarker:
    """Маркер @@SCAN_N из системного промпта."""

    index: int
    prompt: str


@dataclass
class ScanResult:
    """Результат фазы SCAN."""

    level: ScanLevel
    output: str
    responses: Dict[int, str]
    marker_indices: List[int]


@dataclass
class CheckResult:
    """Результат постпроверки CHECK/MISSED."""

    output: str
    checked: List[str]
    missed: List[str]


def parse_scan_markers(system_prompt: str) -> List[ScanMarker]:
    """
    Извлекает маркеры @@SCAN_N из текста системного промпта.

    Args:
        system_prompt: Полный системный промпт агента.

    Returns:
        Отсортированный список маркеров по номеру.
    """
    markers: List[ScanMarker] = []
    for match in MARKER_PATTERN.finditer(system_prompt):
        markers.append(ScanMarker(index=int(match.group(1)), prompt=match.group(2).strip()))
    return sorted(markers, key=lambda m: m.index)


def is_trivial_turn(user_message: str) -> bool:
    """Определяет тривиальный ход (приветствие, благодарность, прощание)."""
    text = (user_message or "").strip()
    if not text:
        return True
    return any(pattern.search(text) for pattern in TRIVIAL_PATTERNS)


def resolve_scan_level(
    user_message: str,
    scenario_tags: Optional[Sequence[str]] = None,
    config: Optional[Dict[str, Any]] = None,
    explicit_level: Optional[str] = None,
) -> ScanLevel:
    """
    Выбирает уровень SCAN по тегам сценария, сложности сообщения и конфигу.

    critical → FULL; long_horizon/instruction → MINI; trivial → SKIP.
    """
    if explicit_level:
        return ScanLevel.from_value(explicit_level)

    scan_cfg = (config or {}).get("scan") or {}
    if not scan_cfg.get("enabled", True):
        return ScanLevel.SKIP

    if is_trivial_turn(user_message):
        return ScanLevel.SKIP

    tags = {str(t).lower() for t in (scenario_tags or [])}
    if "critical" in tags:
        return ScanLevel.FULL
    if tags.intersection({"long_horizon", "instruction", "r_nta", "r_dt", "compliance"}):
        return ScanLevel.MINI
    if tags.intersection({"multi-turn", "anchor"}):
        return ScanLevel.ANCHOR

    default = scan_cfg.get("default_level", "MINI")
    return ScanLevel.from_value(default)


def select_markers_for_level(
    markers: Sequence[ScanMarker],
    level: ScanLevel,
) -> List[ScanMarker]:
    """
    Возвращает подмножество маркеров для уровня SCAN.

    FULL — все; MINI — примерно половина; ANCHOR — первый; SKIP — пусто.
    """
    if level == ScanLevel.SKIP or not markers:
        return []

    ordered = sorted(markers, key=lambda m: m.index)
    if level == ScanLevel.FULL:
        return list(ordered)
    if level == ScanLevel.ANCHOR:
        return [ordered[0]]

    # MINI: чередуем маркеры, минимум 3 или половина списка.
    step = max(1, len(ordered) // 2)
    mini = [ordered[i] for i in range(0, len(ordered), step)]
    if len(mini) < min(3, len(ordered)):
        mini = ordered[: min(3, len(ordered))]
    return mini


def build_scan_trigger(
    markers: Sequence[ScanMarker],
    user_message: str,
    level: ScanLevel,
) -> str:
    """
    Формирует пользовательский триггер для фазы SCAN.

    Модель должна ответить в видимом output, не только в reasoning.
    """
    selected = select_markers_for_level(markers, level)
    if not selected:
        return ""

    lines = [
        "Перед выполнением задачи выполни SCAN.",
        "Ответь в **видимом output** (не только во внутреннем reasoning) строго в формате:",
        "SCAN_N: краткий ответ (1–2 предложения на маркер).",
        "",
        "Маркеры для этого хода:",
    ]
    for marker in selected:
        lines.append(f"- @@SCAN_{marker.index}: {marker.prompt}")
    lines.extend([
        "",
        f"Уровень SCAN: {level.value}.",
        f"Задача пользователя: {user_message.strip()}",
        "",
        "После блока SCAN_N не приступай к основной работе — только SCAN.",
    ])
    return "\n".join(lines)


def parse_scan_output(text: str) -> Dict[int, str]:
    """Парсит ответы SCAN_N из видимого текста модели."""
    responses: Dict[int, str] = {}
    for match in SCAN_RESPONSE_PATTERN.finditer(text or ""):
        responses[int(match.group(1))] = match.group(2).strip()
    return responses


def format_scan_block(responses: Dict[int, str], marker_indices: Sequence[int]) -> str:
    """Собирает видимый блок SCAN для ответа ассистента."""
    if not marker_indices:
        return ""
    lines = []
    for index in sorted(marker_indices):
        answer = responses.get(index, "—")
        lines.append(f"SCAN_{index}: {answer}")
    return "\n".join(lines)


def build_check_trigger(
    markers: Sequence[ScanMarker],
    scan_output: str,
    main_answer: str,
) -> str:
    """
    Формирует триггер постпроверки CHECK/MISSED после основного хода.
    """
    marker_lines = "\n".join(f"- @@SCAN_{m.index}: {m.prompt}" for m in markers)
    return (
        "Выполни постпроверку правил из системного промпта.\n"
        "Ответь в видимом output двумя строками:\n"
        "CHECK: перечисли соблюдённые правила с ✓\n"
        "MISSED: перечисли пропущенные или неактуальные правила (или «нет»)\n\n"
        f"Маркеры SCAN:\n{marker_lines}\n\n"
        f"Твой SCAN перед ходом:\n{scan_output}\n\n"
        f"Твой основной ответ:\n{main_answer}\n"
    )


def parse_check_output(text: str) -> CheckResult:
    """Парсит строки CHECK и MISSED из ответа модели."""
    check_match = CHECK_PATTERN.search(text or "")
    missed_match = MISSED_PATTERN.search(text or "")
    checked_raw = check_match.group(1).strip() if check_match else ""
    missed_raw = missed_match.group(1).strip() if missed_match else ""
    checked = [part.strip() for part in re.split(r"[,;]", checked_raw) if part.strip()]
    missed = [part.strip() for part in re.split(r"[,;]", missed_raw) if part.strip()]
    return CheckResult(output=text or "", checked=checked, missed=missed)


def max_tokens_hint(level: ScanLevel) -> int:
    """Ориентир max_tokens для фазы SCAN/CHECK."""
    return {
        ScanLevel.FULL: 300,
        ScanLevel.MINI: 120,
        ScanLevel.ANCHOR: 20,
        ScanLevel.SKIP: 0,
    }[level]


def build_inline_user_message(
    markers: Sequence[ScanMarker],
    user_message: str,
    level: ScanLevel,
) -> str:
    """
    Собирает user_message с инлайн SCAN-запросом.

    Вместо отдельного LLM-вызова SCAN-маркеры вставляются прямо в тело
    пользовательского сообщения. Модель генерирует SCAN-строки и сразу
    выполняет задачу (tool calls и финальный ответ) — в одном LLM-вызове,
    без дополнительного round-trip и без handoff-путаницы.

    Args:
        markers: Полный список маркеров из системного промпта.
        user_message: Оригинальный текст пользователя.
        level: Уровень SCAN для этого хода.

    Returns:
        Модифицированное сообщение или оригинал (если level == SKIP).
    """
    selected = select_markers_for_level(markers, level)
    if not selected:
        return user_message

    lines = [
        "Перед выполнением задачи кратко ответь на маркеры SCAN",
        f"(уровень {level.value}) в формате `SCAN_N: ответ` — по одной строке на маркер.",
        "Сразу после SCAN-блока выполни задачу (вызови инструменты / дай ответ).",
        "",
    ]
    for m in selected:
        lines.append(f"@@SCAN_{m.index}: {m.prompt}")
    lines.extend([
        "",
        "Задача:",
        user_message,
    ])
    return "\n".join(lines)


def extract_scan_from_response(
    response_text: str,
    markers: Sequence[ScanMarker],
    level: ScanLevel,
) -> str:
    """
    Извлекает SCAN-блок из текста ответа модели.

    Используется при инлайн-режиме, когда SCAN и основной ответ генерируются
    в одном вызове. Возвращает пустую строку, если SCAN-строк в ответе нет.
    """
    responses = parse_scan_output(response_text)
    if not responses:
        return ""
    indices = [m.index for m in select_markers_for_level(markers, level)]
    return format_scan_block(responses, indices)
