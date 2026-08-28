"""Unit-тесты LLM-судьи (без реального LLM)."""
from benchmark.judge import is_judge_eligible, _parse_judge_json
from benchmark.scoring import TurnCheckResult, TurnExpectation, check_turn


def test_is_judge_eligible_content_only():
    assert is_judge_eligible(["нет подстроки ['EMP_ID']"])


def test_is_judge_eligible_rejects_tool_fail():
    assert not is_judge_eligible([
        "нет подстроки ['EMP_ID']",
        "инструмент 'employee_lookup' не вызван в этом ходе",
    ])


def test_is_judge_eligible_rejects_nta():
    assert not is_judge_eligible(["R_NTA: выдуман факт 'violet-42' без вызова tool"])


def test_parse_judge_json_plain():
    parsed = _parse_judge_json('{"pass": true, "reason": "факты верные"}')
    assert parsed["pass"] is True
    assert "верные" in parsed["reason"]


def test_check_turn_still_hard_fails_tool():
    exp = TurnExpectation(user="x", expect_tool_called="benchmark_probe", min_tool_calls_delta=1)
    check = check_turn(0, exp, "ok", [], 0)
    assert not check.passed
    assert not is_judge_eligible(check.failures)


def test_is_judge_eligible_allows_city_alias_tool_args():
    failures = [
        "нет подстроки ['WEATHER']",
        "аргументы tool 'weather_city' не совпали с {'city': 'Helsinki'}",
    ]
    assert is_judge_eligible(failures)


def test_is_judge_eligible_rejects_non_city_tool_args():
    failures = [
        "аргументы tool 'calc_expression' не совпали с {'expression': '3*850'}",
    ]
    assert not is_judge_eligible(failures)


def test_is_judge_eligible_rejects_unknown_city_tool_args():
    failures = [
        "аргументы tool 'weather_city' не совпали с {'city': 'Stockholm'}",
    ]
    assert not is_judge_eligible(failures)
