"""Тесты graph guardrails и graph_store."""
import json
import tempfile
from pathlib import Path

import pytest

import graph_guardrails
from graph_guardrails import (
    enforce_graph_rag_guardrail,
    get_entity_name_regex,
    graph_query_used,
    has_numeric_claim,
    is_statistical_intent,
)
from graph_store import graph_from_snapshot, load_knowledge_graph


def test_statistical_intent_ru():
    assert is_statistical_intent("Сколько сотрудников в Engineering?")
    assert not is_statistical_intent("Кто такая Alice?")


def test_numeric_claim_detected():
    assert has_numeric_claim("В отделе 12 человек")
    assert not has_numeric_claim("Данных в графе нет")


def test_guardrail_blocks_fabricated_count():
    msg = "Сколько сотрудников в Engineering с Remote Work?"
    answer = "В отделе 5 сотрудников с этой политикой."
    replacement = enforce_graph_rag_guardrail(msg, answer, [])
    assert replacement is not None
    assert "graph_query" in replacement or "графе" in replacement.lower()


def test_guardrail_allows_after_graph_query():
    msg = "Сколько сотрудников в Engineering?"
    answer = "По графу: 1 сотрудник."
    assert enforce_graph_rag_guardrail(msg, answer, ["graph_query"]) is None


def test_snapshot_load_matches_demo_count():
    snapshot = Path(__file__).resolve().parent.parent / "data" / "graph_snapshot.json"
    g = graph_from_snapshot(snapshot)
    assert len(g.find_nodes("employee")) == 4


def test_load_knowledge_graph_uses_snapshot_from_config():
    cfg = {
        "graph_rag": {
            "backend": "memory",
            "snapshot_path": str(
                Path(__file__).resolve().parent.parent / "data" / "graph_snapshot.json"
            ),
        }
    }
    g = load_knowledge_graph(cfg)
    assert len(g.find_nodes("department")) == 3


def test_graph_query_used():
    assert graph_query_used(["knowledge_base_search", "graph_query"])
    assert not graph_query_used(["employee_lookup"])


# --- v2 regression: false-positive классы, найденные на реальном каталоге ---


def test_guardrail_trusts_calc_expression_for_arithmetic():
    """
    v2 regression: "сколько будет X+Y" — это арифметика (calc_expression),
    не graph-агрегация. До фикса guardrail заменял корректный численный
    ответ на REFUSAL_TEMPLATE, потому что graph_query_used() проверял
    ТОЛЬКО "graph_query" — реальные случаи из каталога (s01_011: "Посчитай
    сколько часов в году...", s09_011: "сколько будет 128+256?").
    """
    msg = "Четвёртое: сколько будет 128+256? Тоже запомни"
    answer = "RESULT=384"
    assert enforce_graph_rag_guardrail(msg, answer, ["calc_expression"]) is None


def test_graph_query_used_trusts_calc_expression():
    assert graph_query_used(["calc_expression"])
    assert graph_query_used(["graph_query"])
    assert not graph_query_used(["employee_lookup"])


def test_guardrail_does_not_block_off_domain_sse_audit_question():
    """
    v2 regression: "сколько events было в аудит-логе" — это SSE-домен
    (sse_audit_log), не про сотрудников/отделы/политики графа. До фикса
    is_statistical_intent() матчил голое "сколько" без учёта домена и
    ломал recall/lookup ответы из совершенно других MCP-серверов.
    """
    msg = "Сколько событий было в аудит-логе за 2024-01-01?"
    answer = "AUDIT date=2024-01-01 events=3 marker=SSE-AUD-A1B2C3"
    assert enforce_graph_rag_guardrail(msg, answer, []) is None


def test_guardrail_does_not_block_off_domain_general_knowledge():
    """v2 regression: общие знания (дни в феврале, уровни OSI) — не про граф."""
    assert not is_statistical_intent("Кстати, сколько дней в феврале 2024 года?")
    assert not is_statistical_intent("Сколько уровней в модели OSI?")


def test_guardrail_does_not_block_off_domain_crm_offer():
    """v2 regression: "на сколько дней оформлен оффер" — CRM-домен, не граф."""
    msg = "Напомни сегмент C-DUBAI-01 и на сколько дней оформлен оффер"
    answer = "Сегмент: enterprise. Оффер оформлен на 30 дней."
    assert enforce_graph_rag_guardrail(msg, answer, []) is None


def test_guardrail_still_blocks_genuine_graph_domain_fabrication():
    """
    Негативный контроль: настоящая graph-агрегация без graph_query и без
    off-domain маркеров — guardrail должен продолжать блокировать.
    """
    msg = "Сколько сотрудников в отделе Engineering с активной политикой Remote Work?"
    answer = "В отделе 5 сотрудников с этой политикой."
    replacement = enforce_graph_rag_guardrail(msg, answer, [])
    assert replacement is not None


def test_guardrail_bare_count_question_without_off_domain_marker_still_triggers():
    """
    Негативный контроль: короткий "Сколько их?" (без графового домена
    в этом же предложении, как в graph_002 сценарии) должен ПРОДОЛЖАТЬ
    триггерить guardrail — это единственный fallback для сценариев без
    явного упоминания сотрудник/отдел/политик, но и без off-domain маркера.
    """
    msg = "А в Legal кто-нибудь на Remote Work сидит? Сколько их?"
    answer = "Их 5."
    replacement = enforce_graph_rag_guardrail(msg, answer, [])
    assert replacement is not None


# --- v3 regression: позитивная entity-name детекция из snapshot графа ---
#
# Компромисс v2 (задокументирован в JUDGE.md/IMPLEMENTATION.md): _OFF_DOMAIN_RE
# блок-лист, а не позитивная domain-детекция — при появлении новых MCP-доменов
# с "сколько"-подобными вопросами понадобится расширение списка. v3 добавляет
# позитивный сигнал: реальные ИМЕНА policy-узлов графа (из nightly ETL
# snapshot) однозначно доказывают graph-домен без блок-листа.
#
# Эмпирически (offline-симуляция по 166 сценариям каталога + graph_rag.yaml)
# сигнал ограничен ТОЛЬКО policy-именами ("Remote Work", "Health Insurance",
# "Legacy Benefits") — department-имена ("Engineering", "HR", "Sales") и
# employee-имена ("Alice", "Bob", "Carol", "Dave") массово коллизируют с
# ОТДЕЛЬНЫМ HR MCP-доменом каталога (employee_lookup/org_chart_dept в
# s05/s08/s11-s14, где ДРУГИЕ сотрудники используют те же generic слова).


def test_entity_name_regex_loaded_from_snapshot():
    regex = get_entity_name_regex()
    assert regex is not None
    assert regex.search("Remote Work")
    assert regex.search("remote work")  # case-insensitive
    assert regex.search("Health Insurance")
    assert regex.search("Legacy Benefits")


def test_statistical_intent_triggers_on_bare_policy_name_without_stat_keyword():
    """
    v3 positive signal: упоминание РЕАЛЬНОГО имени policy-узла графа —
    однозначное доказательство graph-домена, даже без единого слова из
    _STAT_INTENT_RE ("сколько", "count", "число" и т.п. отсутствуют).
    """
    msg = "Дай точный список тех, кто у нас под Remote Work — и общий итог по ним."
    assert not graph_guardrails._STAT_INTENT_RE.search(msg)
    assert is_statistical_intent(msg)


def test_guardrail_blocks_fabrication_detected_only_via_entity_name():
    """
    Тот же сценарий целиком через enforce_graph_rag_guardrail(): фраза без
    stat-keyword, но с реальным policy-именем, должна блокировать
    выдуманное число без graph_query — сигнал, которого не было в v2.
    """
    msg = "Дай точный список тех, кто у нас под Remote Work — и общий итог по ним."
    answer = "По Remote Work сейчас 7 человек."
    replacement = enforce_graph_rag_guardrail(msg, answer, [])
    assert replacement is not None


def test_off_domain_exclusion_still_works_alongside_entity_signal():
    """
    _OFF_DOMAIN_RE остаётся действующим fallback (v2 не удалён v3) — SSE-
    аудит вопрос без entity-имени графа продолжает корректно исключаться.
    """
    msg = "Сколько событий было в аудит-логе за 2024-01-01?"
    answer = "AUDIT date=2024-01-01 events=3 marker=SSE-AUD-A1B2C3"
    assert enforce_graph_rag_guardrail(msg, answer, []) is None


def test_fresh_off_domain_question_without_graph_entity_still_excluded():
    """
    Новый (не из v2 регрессий) off-domain вопрос, который не упоминает ни
    одной настоящей graph-сущности и не совпадает ни с одним keyword'ом
    _STAT_INTENT_RE, не должен триггерить guardrail (ни через keyword,
    ни через entity-name путь — оба сигнала должны молчать).
    """
    msg = "Сколько будет стоить перевозка груза до склада в другом регионе?"
    answer = "Ориентировочно 1200 у.е. за рейс."
    entity_re = get_entity_name_regex()
    assert entity_re is not None and not entity_re.search(msg)
    # это не про граф вообще — ни один domain-маркер (department/policy) не
    # упомянут; guardrail не должен блокировать чужой (логистика) numeric claim
    assert enforce_graph_rag_guardrail(msg, answer, []) is None


def test_entity_name_regex_missing_snapshot_falls_back_gracefully():
    """
    Негативный контроль: snapshot недоступен (путь не существует) —
    get_entity_name_regex() возвращает None, не бросает исключение.
    Мехнизм entity-детекции просто не участвует, keyword-only ветка работает
    как раньше.
    """
    missing_path = Path("/nonexistent/does-not-exist/graph_snapshot.json")
    assert get_entity_name_regex(missing_path) is None


def test_entity_name_regex_garbage_snapshot_falls_back_gracefully():
    """Негативный контроль: повреждённый (не-JSON) snapshot — без падения."""
    with tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w", encoding="utf-8"
    ) as tf:
        tf.write("this is not valid json {{{")
        path = Path(tf.name)
    try:
        assert get_entity_name_regex(path) is None
    finally:
        path.unlink(missing_ok=True)


def test_entity_name_regex_empty_snapshot_falls_back_gracefully():
    """Негативный контроль: snapshot без policy-узлов — сигнал не строится."""
    with tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w", encoding="utf-8"
    ) as tf:
        json.dump({"nodes": [], "edges": []}, tf)
        path = Path(tf.name)
    try:
        assert get_entity_name_regex(path) is None
    finally:
        path.unlink(missing_ok=True)
