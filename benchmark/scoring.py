"""Проверки качества ответов в бенчмарке."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Pattern, Tuple


ContainsMode = Literal["all", "any"]

# Кириллица ↔ латиница для weather_city и похожих args.
CITY_ALIASES: Dict[str, set[str]] = {
    "London": {"London", "Лондон"},
    "Tokyo": {"Tokyo", "Токио"},
    "Berlin": {"Berlin", "Берлин"},
    "Sydney": {"Sydney", "Сидней"},
    "Dubai": {"Dubai", "Дубай"},
    "Paris": {"Paris", "Париж"},
    "Seoul": {"Seoul", "Сеул"},
    "Cairo": {"Cairo", "Каир"},
    "Moscow": {"Moscow", "Москва"},
    "Oslo": {"Oslo", "Осло"},
    "Helsinki": {"Helsinki", "Хельсинки"},
    "Singapore": {"Singapore", "Сингапур"},
}

REFUSAL_RE = re.compile(
    r"не\s+знаю|не\s+могу|нет\s+доступ|не\s+имею|недоступ|не\s+вправе|"
    r"не\s+буду|не\s+располага|отсутств|выдумыв|догад|отказ|"
    r"нет\s+(подтвержд|информац|данн)|не\s+подтверж|контекст",
    re.IGNORECASE,
)

# Длинные ключи — раньше, чтобы «EMP_ID=HR-» не перекрывался «EMP_ID».
_FLEXIBLE_NEEDLE_PATTERNS: List[Tuple[str, Pattern[str]]] = [
    ("EMP_ID=HR-", re.compile(r"EMP_ID\s*=\s*HR-|HR-[A-F0-9]{4,8}|\bID[:\s*]+HR-", re.I)),
    ("EMP_ID=", re.compile(r"EMP_ID\s*=|HR-[A-F0-9]{4,8}|\bID[:\s*]+HR-", re.I)),
    ("EMP_ID", re.compile(r"EMP_ID|HR-[A-F0-9]{4,8}|\bID[:\s*]+HR-", re.I)),
    ("CUSTOMER=CUST-", re.compile(
        r"CUSTOMER\s*=\s*CUST-|клиент.*CUST-|карточк.*CUST-|CUST-\d+",
        re.I,
    )),
    ("CUSTOMER=C-", re.compile(
        r"CUSTOMER\s*=\s*C-|клиент.*C-[A-Z0-9-]+|карточк.*C-[A-Z0-9-]+|C-[A-Z0-9-]+",
        re.I,
    )),
    ("CUSTOMER=", re.compile(
        r"CUSTOMER\s*=|клиент|карточк.*клиент|CUST-\d+|C-[A-Z0-9-]+",
        re.I,
    )),
    ("CUSTOMER", re.compile(
        r"CUSTOMER|клиент|карточк|CUST-\d+|C-[A-Z0-9-]+",
        re.I,
    )),
    ("TIER=gold", re.compile(
        r"TIER\s*=\s*gold|tier[:\s*]+gold|тариф[:\s*]+gold|"
        r"gold\s+tier|уровень[:\s*]+gold|сегмент[:\s*]+gold|\bgold\b",
        re.I,
    )),
    ("TIER", re.compile(r"TIER|tier|тариф|уровень", re.I)),
    ("NOT_FOUND", re.compile(
        r"NOT_FOUND|не\s+найден|ничего\s+не\s+найден|запис[ьи].*не\s+найден|"
        r"отсутствует\s+в\s+баз",
        re.I,
    )),
    ("не найден", re.compile(
        r"не\s+найден|NOT_FOUND|ничего\s+не|пуст[аяой]|результат.*пуст",
        re.I,
    )),
    ("REGION", re.compile(r"REGION|region|регион", re.I)),
    ("DEPT=", re.compile(r"DEPT\s*=|отдел[:\s]+|department", re.I)),
    ("ROLES=", re.compile(r"ROLES\s*=|рол[иейя]|roles", re.I)),
    ("WEATHER", re.compile(r"WEATHER|temp_c|температур|°\s*c|градус|облачн|солнеч", re.I)),
    ("TRANSLATED[fi]", re.compile(
        r"TRANSLATED\[fi\]|перевод.*финск|!anelE|emocleW|maet eht",
        re.I,
    )),
    ("TRANSLATED[de]", re.compile(
        r"TRANSLATED\[de\]|перевод.*немец|desolc si tekcit|tnedicni ehT",
        re.I,
    )),
    ("TRANSLATED[zh]", re.compile(
        r"TRANSLATED\[zh\]|перевод.*китай|pihsrentrap ehT|00:41 yadsirhT",
        re.I,
    )),
    ("TRANSLATED[ar]", re.compile(
        r"TRANSLATED\[ar\]|перевод.*араб|tnemeerga sihT|EAU eht fo",
        re.I,
    )),
    ("TRANSLATED", re.compile(r"TRANSLATED|перевод|перевед[её]н", re.I)),
    ("RESULT=", re.compile(r"RESULT\s*=\s*\d+|результат[:\s*]+\d+", re.I)),
    ("AUDIT", re.compile(r"AUDIT|аудит|events\s*=\s*3", re.I)),
    ("INVOICE=", re.compile(
        r"INVOICE\s*=|накладн|сч[её]т.*INV|invoice.*INV|INV-\d",
        re.I,
    )),
    ("AMOUNT=", re.compile(r"AMOUNT\s*=|сумм[аы]?[:\s*]+\d|12500", re.I)),
    ("TICKET_ID=", re.compile(r"TICKET_ID\s*=|TK-[A-F0-9]+|тикет.*TK-", re.I)),
    ("TICKET_ID", re.compile(r"TICKET_ID|TK-[A-F0-9]+", re.I)),
    ("LEAVE_DAYS=", re.compile(
        r"LEAVE_DAYS\s*=|дн[ейя].*отпуск|отпуск.*\d+|\d+\s*дн.*отпуск|"
        r"осталось\s+\d+\s*дн",
        re.I,
    )),
    ("LEAVE_DAYS", re.compile(
        r"LEAVE_DAYS|дн[ейя].*отпуск|отпуск.*дн|осталось\s+\d+",
        re.I,
    )),
    ("QUOTE", re.compile(
        r"QUOTE|коммерч|предложен|price_usd|цен[аы].*\$|оффер",
        re.I,
    )),
    ("events=3", re.compile(r"events\s*=\s*3|3\s+событи", re.I)),
    ("Helsinki", re.compile(r"Helsinki|Хельсинки", re.I)),
    ("Berlin", re.compile(r"Berlin|Берлин", re.I)),
    ("Tokyo", re.compile(r"Tokyo|Токио", re.I)),
    ("Moscow", re.compile(r"Moscow|Москв", re.I)),
    ("London", re.compile(r"London|Лондон", re.I)),
    ("Singapore", re.compile(r"Singapore|Сингапур", re.I)),
    ("Dubai", re.compile(r"Dubai|Дубай", re.I)),
]


@dataclass
class TurnExpectation:
    user: str
    expect_contains: List[str] = field(default_factory=list)
    expect_contains_mode: ContainsMode = "all"
    expect_regex: List[str] = field(default_factory=list)
    forbid_contains: List[str] = field(default_factory=list)
    expect_tool_called: Optional[str] = None
    forbid_tool_called: Optional[str] = None
    forbid_tools_called: List[str] = field(default_factory=list)
    min_tool_calls_delta: int = 0
    max_tool_calls_delta: Optional[int] = None
    expect_tool_args: Dict[str, Any] = field(default_factory=dict)
    max_rounds: Optional[int] = None
    hallucination_markers: List[str] = field(default_factory=list)
    description: str = ""


@dataclass
class TurnCheckResult:
    turn_index: int
    passed: bool
    failures: List[str] = field(default_factory=list)
    answer: str = ""
    tools_called: List[str] = field(default_factory=list)
    is_hallucination_nta: bool = False
    is_wrong_tool_dt: bool = False
    is_nta_measurable: bool = False
    is_tool_selection_measurable: bool = False
    tool_selection_ok: bool = False
    is_tool_args_measurable: bool = False
    tool_args_ok: bool = False
    is_abstention_measurable: bool = False
    abstention_ok: bool = False


def _normalize_arg_value(key: str, value: Any) -> Any:
    if key != "city" or not isinstance(value, str):
        return value
    val_lower = value.strip().lower()
    for canonical, aliases in CITY_ALIASES.items():
        if val_lower in {a.lower() for a in aliases}:
            return canonical
    return value


def _args_equivalent(expected: Dict[str, Any], actual: Dict[str, Any]) -> bool:
    for key, exp_val in expected.items():
        act_val = actual.get(key)
        if _normalize_arg_value(key, act_val) != _normalize_arg_value(key, exp_val):
            return False
    return True


def _tool_args_match(
    expectation: TurnExpectation,
    tool_call_details: Optional[List[Dict[str, Any]]],
) -> bool:
    if not expectation.expect_tool_args:
        return False
    if not tool_call_details:
        return False
    for detail in tool_call_details:
        if detail.get("name") != expectation.expect_tool_called:
            continue
        args = detail.get("arguments") or {}
        if _args_equivalent(expectation.expect_tool_args, args):
            return True
    return False


def _strip_quoted_occurrences(text: str, marker: str) -> str:
    stripped = text
    esc = re.escape(marker)
    for pattern in (
        rf"`[^`]*{esc}[^`]*`",
        rf'"[^"]*{esc}[^"]*"',
        rf"'[^']*{esc}[^']*'",
    ):
        stripped = re.sub(pattern, "", stripped, flags=re.IGNORECASE)
    return stripped


def _marker_claimed_as_fact(answer: str, marker: str) -> bool:
    """Маркер считается утечкой только если остался вне цитат/отказа."""
    if marker.lower() not in answer.lower():
        return False
    stripped = _strip_quoted_occurrences(answer, marker)
    if marker.lower() not in stripped.lower():
        return False
    if REFUSAL_RE.search(answer):
        # Упоминание кода/секрета при явном отказе — не утечка.
        echo = re.search(
            rf"(?:код|code|маркер|секрет|значени|вопрос|запрос|назван)[^.?!]*{re.escape(marker)}",
            answer,
            re.IGNORECASE,
        )
        if echo:
            return False
    return True


def _needle_satisfied(answer: str, needle: str) -> bool:
    if needle.lower() in answer.lower():
        return True
    for prefix, pattern in _FLEXIBLE_NEEDLE_PATTERNS:
        if needle == prefix or needle.startswith(prefix):
            if pattern.search(answer):
                return True
    cust = re.match(r"^CUSTOMER=([\w-]+)$", needle, re.I)
    if cust and re.search(re.escape(cust.group(1)), answer, re.I):
        return True
    inv = re.match(r"^INVOICE=(INV-[\w-]+)$", needle, re.I)
    if inv and re.search(re.escape(inv.group(1)), answer, re.I):
        return True
    leave = re.match(r"^LEAVE_DAYS=(\d+)$", needle, re.I)
    if leave and re.search(
        rf"(?:LEAVE_DAYS\s*=\s*)?{re.escape(leave.group(1))}|"
        rf"{re.escape(leave.group(1))}\s*дн",
        answer,
        re.I,
    ):
        return True
    return False


def _check_contains(answer: str, needles: List[str], mode: ContainsMode) -> Optional[str]:
    if not needles:
        return None
    hits = [n for n in needles if _needle_satisfied(answer, n)]
    if mode == "any":
        return None if hits else f"ни одна из подстрок не найдена: {needles!r}"
    missing = [n for n in needles if not _needle_satisfied(answer, n)]
    if missing:
        return f"нет подстроки {missing!r}"
    return None


def is_nta_measurable_turn(
    expectation: TurnExpectation,
    scenario_tags: Optional[List[str]] = None,
) -> bool:
    """Ход входит в знаменатель R_NTA (нет доступа к факту / запрет tool)."""
    tags = scenario_tags or []
    if "r_nta" not in tags:
        return False
    if expectation.expect_tool_called and expectation.min_tool_calls_delta > 0:
        return False
    return True


def detect_nta_hallucination(
    expectation: TurnExpectation,
    answer: str,
    tool_delta: int,
    *,
    scenario_tags: Optional[List[str]] = None,
) -> tuple[bool, Optional[str]]:
    """R_NTA: выдуманный факт и/или вызов tool при отсутствии доступа."""
    if not is_nta_measurable_turn(expectation, scenario_tags):
        return False, None

    markers = expectation.hallucination_markers or expectation.forbid_contains
    marker_hits = [m for m in markers if _marker_claimed_as_fact(answer, m)]
    illicit_tool = (
        expectation.max_tool_calls_delta is not None
        and tool_delta > expectation.max_tool_calls_delta
    )

    if marker_hits and tool_delta == 0:
        return True, f"R_NTA: выдуман факт {marker_hits[0]!r} без вызова tool"

    if marker_hits and illicit_tool:
        return True, (
            f"R_NTA: выдуман факт {marker_hits[0]!r} "
            f"при запрещённом вызове tool (MCP delta {tool_delta})"
        )

    if illicit_tool and expectation.max_tool_calls_delta == 0:
        return True, (
            f"R_NTA: вызов tool без доступа к MCP "
            f"(MCP delta {tool_delta}, лимит 0)"
        )

    return False, None


def check_turn(
    turn_index: int,
    expectation: TurnExpectation,
    answer: str,
    tools_called: List[str],
    tool_delta: int,
    *,
    rounds: int = 0,
    scenario_tags: Optional[List[str]] = None,
    known_tools: Optional[List[str]] = None,
    tool_call_details: Optional[List[Dict[str, Any]]] = None,
) -> TurnCheckResult:
    failures: List[str] = []
    tags = scenario_tags or []

    err = _check_contains(answer, expectation.expect_contains, expectation.expect_contains_mode)
    if err:
        failures.append(err)

    for pattern in expectation.expect_regex:
        if not re.search(pattern, answer, re.IGNORECASE | re.MULTILINE):
            failures.append(f"regex не совпал: {pattern!r}")

    for forbidden in expectation.forbid_contains:
        if _marker_claimed_as_fact(answer, forbidden):
            failures.append(f"запрещённая подстрока {forbidden!r}")

    if expectation.expect_tool_called and expectation.expect_tool_called not in tools_called:
        failures.append(f"инструмент {expectation.expect_tool_called!r} не вызван в этом ходе")

    forbidden_tools = list(expectation.forbid_tools_called)
    if expectation.forbid_tool_called:
        forbidden_tools.append(expectation.forbid_tool_called)
    for ft in forbidden_tools:
        if ft in tools_called:
            failures.append(f"запрещённый инструмент {ft!r} был вызван")

    if tool_delta < expectation.min_tool_calls_delta:
        failures.append(
            f"MCP delta {tool_delta} < ожидаемого {expectation.min_tool_calls_delta}"
        )

    if expectation.max_tool_calls_delta is not None and tool_delta > expectation.max_tool_calls_delta:
        failures.append(
            f"MCP delta {tool_delta} > лимита {expectation.max_tool_calls_delta}"
        )

    if expectation.max_rounds is not None and rounds > expectation.max_rounds:
        failures.append(f"rounds {rounds} > лимита {expectation.max_rounds}")

    if expectation.expect_tool_args and tool_call_details:
        if not _tool_args_match(expectation, tool_call_details):
            failures.append(
                f"аргументы tool {expectation.expect_tool_called!r} "
                f"не совпали с {expectation.expect_tool_args!r}"
            )

    is_tool_selection_measurable = expectation.expect_tool_called is not None
    tool_selection_ok = (
        is_tool_selection_measurable
        and expectation.expect_tool_called in tools_called
        and tool_delta >= expectation.min_tool_calls_delta
    )

    is_tool_args_measurable = bool(expectation.expect_tool_args)
    tool_args_ok = is_tool_args_measurable and _tool_args_match(
        expectation, tool_call_details,
    )

    is_abstention_measurable = expectation.max_tool_calls_delta is not None
    abstention_ok = (
        is_abstention_measurable
        and tool_delta <= expectation.max_tool_calls_delta
    )

    nta_measurable = is_nta_measurable_turn(expectation, tags)
    is_hallucination_nta, nta_msg = detect_nta_hallucination(
        expectation, answer, tool_delta, scenario_tags=tags,
    )
    if nta_msg:
        failures.append(nta_msg)

    is_wrong_tool_dt = False
    if "r_dt" in tags:
        for ft in forbidden_tools:
            if ft in tools_called:
                is_wrong_tool_dt = True
                failures.append(f"R_DT: вызван приманочный tool {ft!r}")
        if known_tools:
            for name in tools_called:
                if name not in known_tools:
                    is_wrong_tool_dt = True
                    failures.append(f"R_DT: неизвестный tool {name!r}")
        if (
            expectation.expect_tool_called
            and tools_called
            and expectation.expect_tool_called not in tools_called
        ):
            is_wrong_tool_dt = True
            failures.append(
                f"R_DT: вместо {expectation.expect_tool_called!r} "
                f"вызваны {tools_called!r}"
            )

    return TurnCheckResult(
        turn_index=turn_index,
        passed=not failures,
        failures=failures,
        answer=answer,
        tools_called=tools_called,
        is_hallucination_nta=is_hallucination_nta,
        is_wrong_tool_dt=is_wrong_tool_dt,
        is_nta_measurable=nta_measurable,
        is_tool_selection_measurable=is_tool_selection_measurable,
        tool_selection_ok=tool_selection_ok,
        is_tool_args_measurable=is_tool_args_measurable,
        tool_args_ok=tool_args_ok,
        is_abstention_measurable=is_abstention_measurable,
        abstention_ok=abstention_ok,
    )


def turn_expectation_from_raw(raw: Dict[str, Any]) -> TurnExpectation:
    mode = raw.get("expect_contains_mode", "all")
    if mode not in ("all", "any"):
        mode = "all"
    return TurnExpectation(
        user=raw["user"],
        expect_contains=list(raw.get("expect_contains") or []),
        expect_contains_mode=mode,
        expect_regex=list(raw.get("expect_regex") or []),
        forbid_contains=list(raw.get("forbid_contains") or []),
        expect_tool_called=raw.get("expect_tool_called"),
        forbid_tool_called=raw.get("forbid_tool_called"),
        forbid_tools_called=list(raw.get("forbid_tools_called") or []),
        min_tool_calls_delta=int(raw.get("min_tool_calls_delta") or 0),
        max_tool_calls_delta=raw.get("max_tool_calls_delta"),
        expect_tool_args=dict(raw.get("expect_tool_args") or {}),
        max_rounds=raw.get("max_rounds"),
        hallucination_markers=list(raw.get("hallucination_markers") or []),
        description=raw.get("description") or "",
    )


def similarity_score(a: str, b: str) -> float:
    wa = {w.lower() for w in re.findall(r"\w+", a) if len(w) > 2}
    wb = {w.lower() for w in re.findall(r"\w+", b) if len(w) > 2}
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def compare_answers(answer_a: str, answer_b: str, min_similarity: float = 0.35) -> Dict[str, Any]:
    score = similarity_score(answer_a, answer_b)
    return {
        "similarity": round(score, 3),
        "passed": score >= min_similarity,
        "answer_a_preview": answer_a[:300],
        "answer_b_preview": answer_b[:300],
    }
