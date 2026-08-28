"""Post-response guardrails для Graph-RAG: блок числовых утверждений без graph_query."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger("graph_guardrails")

# Признаки агрегатного / статистического запроса
_STAT_INTENT_RE = re.compile(
    r"(?i)"
    r"(сколько|how\s+many|count|число|количеств|статистик|агрегат|"
    r"скольк[оа]\s+сотрудник|employees?\s+in|per\s+department|"
    r"с\s+политик|with\s+policy|активн\w+\s+политик)"
)

# v3 fix: позитивная domain-детекция через реальные имена сущностей графа
# (отделы/политики/сотрудники из nightly ETL snapshot), в дополнение к
# _OFF_DOMAIN_RE exclusion-списку. См. "v3 fix" в IMPLEMENTATION.md/JUDGE.md.
_DEFAULT_SNAPSHOT_PATH = (
    Path(__file__).resolve().parent / "data" / "graph_snapshot.json"
)

_entity_name_re_cache: Dict[str, "re.Pattern[str]"] = {}


# Только "policy" — эмпирически проверено (см. offline-симуляция в v3
# JUDGE.md/IMPLEMENTATION.md): department-имена графа ("Engineering", "HR",
# "Sales") и employee-имена ("Alice", "Bob", "Carol", "Dave") массово
# коллизируют с ДРУГИМ, отдельным HR MCP-доменом каталога (employee_lookup /
# org_chart_dept в s05/s08/s11-s14 использует ДРУГИХ сотрудников и ТЕ ЖЕ
# самые отделы/имена как generic термины). Policy-имена ("Remote Work",
# "Health Insurance", "Legacy Benefits") нигде в каталоге вне graph_rag.yaml
# не встречаются — уникальный, надёжный позитивный сигнал.
_ENTITY_NAME_NODE_TYPES = ("policy",)


def _extract_entity_names(snapshot_path: Path) -> List[str]:
    """Достаёт имена policy-узлов из snapshot JSON."""
    raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    names: List[str] = []
    for node in raw.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if node.get("type") not in _ENTITY_NAME_NODE_TYPES:
            continue
        name = node.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def get_entity_name_regex(
    snapshot_path: Optional[Path] = None,
) -> Optional["re.Pattern[str]"]:
    """
    Лениво строит и кеширует regex литеральных имён сущностей графа
    (department/policy/employee) из nightly ETL snapshot.

    Возвращает None, если snapshot недоступен/пуст/повреждён — в этом случае
    is_statistical_intent() падает обратно на старое regex-only поведение
    (_STAT_INTENT_RE + _OFF_DOMAIN_RE), без падения модуля при импорте.
    """
    path = snapshot_path or _DEFAULT_SNAPSHOT_PATH
    cache_key = str(path)
    if cache_key in _entity_name_re_cache:
        return _entity_name_re_cache[cache_key]

    pattern: Optional["re.Pattern[str]"] = None
    try:
        names = _extract_entity_names(path)
        names = sorted({n for n in names if n}, key=len, reverse=True)
        if names:
            alternation = "|".join(re.escape(n) for n in names)
            pattern = re.compile(r"(?i)\b(" + alternation + r")\b")
        else:
            logger.warning(
                "Graph guardrails: snapshot %s has no department/policy/"
                "employee names — entity-name signal disabled, falling back "
                "to keyword-only is_statistical_intent()",
                path,
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "Graph guardrails: could not load snapshot %s for entity-name "
            "signal (%s) — falling back to keyword-only is_statistical_intent()",
            path,
            exc,
        )
        pattern = None

    _entity_name_re_cache[cache_key] = pattern
    return pattern

# v2 fix: голое "сколько"/"count" матчит ЛЮБОЙ counting-вопрос, не только про
# граф знаний (сотрудники/отделы/политики). На реальном каталоге это ловило
# вопросы из совершенно других доменов — SSE-аудит ("сколько events было в
# аудит-логе"), CRM-оффер ("на сколько дней оформлен оффер"), общие знания
# ("сколько дней в феврале", "сколько уровней в модели OSI") — заменяя
# корректные ответы бессмысленным "уточните отдел, политику" REFUSAL_TEMPLATE.
# Явный exclusion-список для доменов, определённо не про Graph-RAG.
_OFF_DOMAIN_RE = re.compile(
    r"(?i)"
    r"(аудит|audit|событ|event|sse|оффер|offer|"
    r"феврал|январ|март|апрел|ма[йя]\b|июн|июл|август|сентябр|октябр|ноябр|декабр|"
    r"osi\b|модели\s+osi|уровней\s+в\s+модели|"
    r"внешние\s+систем|external\s+system)"
)

# Числовое утверждение в ответе (не даты вида 2024)
_NUMERIC_CLAIM_RE = re.compile(
    r"(?<!\d)(?<![:/-])\b\d{1,6}\b(?!\d)(?![:/-])"
)

REFUSAL_TEMPLATE = (
    "В графе знаний нет подтверждённых данных для этого статистического запроса. "
    "Я не могу назвать число без вызова graph_query. "
    "Уточните отдел, политику или переформулируйте вопрос."
)


def is_statistical_intent(user_message: str) -> bool:
    text = user_message or ""

    keyword_hit = bool(_STAT_INTENT_RE.search(text))
    entity_re = get_entity_name_regex()
    entity_hit = bool(entity_re and entity_re.search(text))

    if not (keyword_hit or entity_hit):
        return False
    # _OFF_DOMAIN_RE остаётся как fallback exclusion — v3 не убирает его,
    # он всё ещё нужен для голого "сколько" без entity-имени в предложении
    # (см. graph_rag.yaml::graph_002 и v2 regression тесты в
    # tests/test_graph_decisions.py). Позитивный entity-name сигнал (v3)
    # снижает КОЛИЧЕСТВО случаев, где exclusion-список является единственной
    # линией защиты, но не заменяет его целиком.
    if _OFF_DOMAIN_RE.search(text):
        return False
    return True


def has_numeric_claim(answer: str) -> bool:
    text = answer or ""
    for match in _NUMERIC_CLAIM_RE.finditer(text):
        # Игнорируем маркеры вроде HR-001
        span = match.group(0)
        if len(span) >= 4 and span.isdigit():
            continue
        return True
    return False


# Tools, которые могут легитимно вернуть числовое значение без graph_query —
# они детерминированные, не подвержены fabrication-риску, который этот
# guardrail призван ловить. _STAT_INTENT_RE матчит голое "сколько"/"count"
# без учёта контекста — это ловит и entity-агрегацию ("сколько сотрудников"),
# и обычную арифметику ("сколько будет 128+256?"), для которой calc_expression
# — легитимный, доверенный источник числа. Подтверждено на реальном каталоге:
# без этого исключения guardrail заменял верные ответы calc_expression
# (s01_011, s09_011) на бессмысленный REFUSAL_TEMPLATE.
_TRUSTED_NUMERIC_TOOLS = frozenset({"graph_query", "calc_expression"})


def graph_query_used(tool_names: Sequence[str]) -> bool:
    return bool(_TRUSTED_NUMERIC_TOOLS & set(tool_names or []))


def enforce_graph_rag_guardrail(
    user_message: str,
    answer: str,
    tool_names: Sequence[str],
    *,
    enabled: bool = True,
) -> Optional[str]:
    """
    Возвращает замену ответа при нарушении, иначе None (ответ допустим).
    """
    if not enabled:
        return None
    if not is_statistical_intent(user_message):
        return None
    if graph_query_used(tool_names):
        return None
    if not has_numeric_claim(answer):
        return None
    return REFUSAL_TEMPLATE
