"""Тесты для benchmark.audit_realism."""
from __future__ import annotations

from benchmark.audit_realism import (
    _is_business_scenario,
    build_report,
    scan_scenarios,
    score_prompt,
)


def test_score_clean_business_prompt():
    score, category, reasons = score_prompt("Сколько SKU-1001 на складе?")
    assert score == 1.0
    assert category == "business"
    assert reasons == []


def test_score_penalizes_tool_name():
    score, category, reasons = score_prompt("Вызови employee_lookup для Anna Smith")
    assert score < 0.7
    assert category == "business"
    assert any(r.startswith("tool_name:") for r in reasons)


def test_score_penalizes_meta_phrase():
    score, _, reasons = score_prompt("Ответь из памяти, без tool")
    assert score < 0.7
    assert len(reasons) >= 2


def test_score_tags_format_rule_as_instruction_following():
    score, category, reasons = score_prompt("Каждый ответ заканчивается словом BANANA")
    assert category == "instruction_following"
    assert any(r.startswith("format_rule:") for r in reasons)
    assert score < 0.7


def test_score_penalizes_wrong_customer_id():
    score, _, reasons = score_prompt("Открой карточку CUS-4471")
    assert score == 0.7
    assert "wrong_id:CUS-" in reasons


def test_business_exclusion_by_tag():
    assert not _is_business_scenario("catalog/s01_core.yaml", ["catalog", "instruction"])
    assert _is_business_scenario("catalog/s01_core.yaml", ["catalog", "smoke"])


def test_business_exclusion_by_file():
    assert not _is_business_scenario("instruction.yaml", ["critical"])
    assert not _is_business_scenario("catalog/creative_b.yaml", ["catalog", "creative"])


def test_scan_scenarios_returns_hits():
    hits = scan_scenarios()
    assert len(hits) > 400
    assert all(0.0 <= h.score <= 1.0 for h in hits)


def test_build_report_contains_summary():
    hits = scan_scenarios()
    report = build_report(hits)
    assert "Overall average:" in report
    assert "Business average" in report
    assert "Per-file breakdown:" in report
