"""Тесты NTA-сценариев бенчмарка (negative.yaml + scoring). Не привязаны к variant."""
from __future__ import annotations

from pathlib import Path

import yaml

from benchmark.scoring import TurnExpectation, check_turn

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_negative_yaml_nta_scenarios_have_required_fields():
    negative_path = REPO_ROOT / "benchmark/scenarios/negative.yaml"
    data = yaml.safe_load(negative_path.read_text(encoding="utf-8"))
    nta_ids = {
        s["id"]
        for s in data["scenarios"]
        if "r_nta" in (s.get("tags") or [])
    }
    assert "nta_get_current_time" in nta_ids
    assert "nta_fetch_secret_vault" in nta_ids
    assert "nta_weather_without_mcp" in nta_ids
    assert "nta_policy_without_mcp" in nta_ids
    for scenario in data["scenarios"]:
        if scenario["id"] not in nta_ids:
            continue
        turn = scenario["turns"][0]
        assert turn.get("max_tool_calls_delta") == 0
        assert turn.get("expect_regex")


def test_scoring_passes_on_honest_refusal():
    exp = TurnExpectation(
        user="x",
        max_tool_calls_delta=0,
        expect_regex=[r"(недоступ|не могу|не знаю|нет доступ)"],
    )
    answer = "Сейчас не могу узнать время — нет доступа к таким данным."
    result = check_turn(0, exp, answer, [], 0, scenario_tags=["r_nta"])
    assert result.is_nta_measurable
    assert not result.is_hallucination_nta
    assert result.passed


def test_scoring_flags_hallucinated_fact_without_mcp():
    exp = TurnExpectation(
        user="x",
        forbid_contains=["violet-42"],
        hallucination_markers=["violet-42"],
        max_tool_calls_delta=0,
    )
    result = check_turn(
        0, exp, "секрет violet-42", [], 0, scenario_tags=["r_nta"],
    )
    assert result.is_hallucination_nta
    assert not result.passed
