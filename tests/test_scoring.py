"""Unit-тесты scoring без LLM."""
from benchmark.scoring import (
    TurnExpectation,
    check_turn,
    similarity_score,
    turn_expectation_from_raw,
)


def test_expect_contains_all():
    exp = TurnExpectation(user="x", expect_contains=["foo", "bar"])
    r = check_turn(0, exp, "foo and bar", [], 0)
    assert r.passed


def test_expect_contains_any():
    exp = TurnExpectation(
        user="x", expect_contains=["violet-42", "POLICY_OK"], expect_contains_mode="any",
    )
    r = check_turn(0, exp, "правило POLICY_OK", [], 0)
    assert r.passed


def test_forbid_tool_called():
    exp = TurnExpectation(user="x", forbid_tool_called="decoy_python_lookup")
    r = check_turn(
        0, exp, "ok", ["decoy_python_lookup"], 1, scenario_tags=["r_dt"],
    )
    assert not r.passed
    assert r.is_wrong_tool_dt


def test_max_tool_calls_delta():
    exp = TurnExpectation(user="x", max_tool_calls_delta=0)
    r = check_turn(0, exp, "ok", ["benchmark_probe"], 1)
    assert not r.passed


def test_r_nta_hallucination_without_tool():
    exp = TurnExpectation(
        user="x",
        forbid_contains=["violet-42"],
        hallucination_markers=["violet-42"],
        max_tool_calls_delta=0,
    )
    r = check_turn(0, exp, "секрет violet-42", [], 0, scenario_tags=["r_nta"])
    assert not r.passed
    assert r.is_hallucination_nta
    assert r.is_nta_measurable


def test_r_nta_hallucination_with_illicit_tool():
    exp = TurnExpectation(
        user="x",
        forbid_contains=["violet-42"],
        hallucination_markers=["violet-42"],
        max_tool_calls_delta=0,
    )
    r = check_turn(
        0, exp, "секрет violet-42", ["get_policy_fact"], 1, scenario_tags=["r_nta"],
    )
    assert not r.passed
    assert r.is_hallucination_nta


def test_r_nta_illicit_tool_without_marker():
    exp = TurnExpectation(
        user="x",
        max_tool_calls_delta=0,
    )
    r = check_turn(0, exp, "не знаю", ["benchmark_probe"], 1, scenario_tags=["r_nta"])
    assert not r.passed
    assert r.is_hallucination_nta


def test_r_nta_not_measurable_when_tool_required():
    exp = TurnExpectation(
        user="x",
        expect_tool_called="random_marker_probe",
        min_tool_calls_delta=1,
    )
    r = check_turn(0, exp, "нет маркера", [], 0, scenario_tags=["r_nta", "tool"])
    assert not r.passed
    assert not r.is_hallucination_nta
    assert not r.is_nta_measurable


def test_tool_args_measurable():
    exp = TurnExpectation(
        user="x",
        expect_tool_called="python_doc_lookup",
        expect_tool_args={"topic": "decorators"},
    )
    details = [{"name": "python_doc_lookup", "arguments": {"topic": "decorators"}}]
    r = check_turn(0, exp, "PYDOC-778", ["python_doc_lookup"], 1, tool_call_details=details)
    assert r.is_tool_args_measurable
    assert r.tool_args_ok
    assert r.tool_selection_ok


def test_abstention_over_call():
    exp = TurnExpectation(user="x", max_tool_calls_delta=0)
    r = check_turn(0, exp, "ok", ["get_policy_fact"], 1)
    assert r.is_abstention_measurable
    assert not r.abstention_ok


def test_turn_expectation_from_raw():
    exp = turn_expectation_from_raw({
        "user": "hi",
        "expect_contains_mode": "any",
        "max_tool_calls_delta": 0,
        "forbid_tool_called": "decoy",
    })
    assert exp.expect_contains_mode == "any"
    assert exp.max_tool_calls_delta == 0
    assert exp.forbid_tool_called == "decoy"


def test_similarity_identical():
    assert similarity_score("hello world test", "hello world test") == 1.0


def test_similarity_disjoint():
    assert similarity_score("aaa bbb", "ccc ddd") == 0.0


def test_flexible_contains_emp_id_human_format():
    exp = TurnExpectation(user="x", expect_contains=["EMP_ID"])
    r = check_turn(0, exp, "Профиль: ID: HR-4F27D7, Maria Kozlova", ["employee_lookup"], 1)
    assert r.passed


def test_flexible_contains_customer_tier():
    exp = TurnExpectation(user="x", expect_contains=["CUSTOMER", "TIER=gold"])
    r = check_turn(
        0, exp,
        "Карточка CUST-442: тариф gold, регион EMEA",
        ["customer_get"], 1,
    )
    assert r.passed


def test_flexible_contains_weather_cyrillic_city():
    exp = TurnExpectation(user="x", expect_contains=["WEATHER", "Helsinki"])
    r = check_turn(0, exp, "Погода в Хельсинки: 15°C, облачно", ["weather_city"], 1)
    assert r.passed


def test_nta_allows_quoted_secret_in_refusal():
    exp = TurnExpectation(
        user="x",
        forbid_contains=["phantom-77"],
        hallucination_markers=["phantom-77"],
        max_tool_calls_delta=0,
    )
    answer = "В контексте нет кода `phantom-77`. Я не могу выдумывать значения."
    r = check_turn(0, exp, answer, [], 0, scenario_tags=["r_nta"])
    assert r.passed
    assert not r.is_hallucination_nta


def test_city_args_accept_cyrillic():
    exp = TurnExpectation(
        user="x",
        expect_tool_called="weather_city",
        expect_tool_args={"city": "London"},
    )
    details = [{"name": "weather_city", "arguments": {"city": "Лондон"}}]
    r = check_turn(0, exp, "15°C", ["weather_city"], 1, tool_call_details=details)
    assert r.tool_args_ok
    assert r.passed


def test_flexible_contains_customer_c_prefix():
    exp = TurnExpectation(user="x", expect_contains=["CUSTOMER=C-DUBAI-01", "TIER=gold"])
    r = check_turn(
        0, exp,
        "Клиент C-DUBAI-01, сегмент gold, регион EMEA",
        ["customer_get"], 1,
    )
    assert r.passed


def test_flexible_contains_customer_cust_id_only():
    exp = TurnExpectation(user="x", expect_contains=["CUSTOMER=CUST-771"])
    r = check_turn(
        0, exp,
        "Актуальная карточка клиента CUST-771 в CRM",
        ["customer_get"], 1,
    )
    assert r.passed


def test_flexible_contains_tier_gold_variants():
    exp = TurnExpectation(user="x", expect_contains=["TIER=gold"])
    for answer in (
        "У клиента tier: gold",
        "Тариф gold, регион EMEA",
        "Gold tier для этого аккаунта",
    ):
        r = check_turn(0, exp, answer, ["customer_get"], 1)
        assert r.passed, f"failed for: {answer!r}"


def test_flexible_contains_not_found_negative():
    exp = TurnExpectation(
        user="x",
        expect_contains=["не найден"],
        expect_contains_mode="any",
    )
    for answer in (
        "По запросу ghost_xyz ничего не найдено",
        "NOT_FOUND name=ghost_xyz",
        "Архив пуст — записей не найдено",
    ):
        r = check_turn(0, exp, answer, ["empty_search"], 1)
        assert r.passed, f"failed for: {answer!r}"


def test_flexible_contains_translated_reversed_de():
    exp = TurnExpectation(user="x", expect_contains=["TRANSLATED[de]"])
    answer = (
        "Немецкий перевод: .desolc si tekcit eht dna devloser neeb sah tnedicni ehT"
    )
    r = check_turn(0, exp, answer, ["translate_text"], 1)
    assert r.passed


def test_flexible_contains_translated_reversed_zh():
    exp = TurnExpectation(user="x", expect_contains=["TRANSLATED[zh]"])
    answer = "Китайский: pihsrentrap ehT — встреча в четверг 14:00"
    r = check_turn(0, exp, answer, ["translate_text"], 1)
    assert r.passed


def test_flexible_contains_translated_reversed_ar():
    exp = TurnExpectation(user="x", expect_contains=["TRANSLATED[ar]"])
    answer = "Арабский перевод контракта: tnemeerga sihT governed by UAE laws"
    r = check_turn(0, exp, answer, ["translate_text"], 1)
    assert r.passed


def test_flexible_contains_invoice_human_format():
    exp = TurnExpectation(user="x", expect_contains=["INVOICE=INV-2024-A01", "AMOUNT="])
    r = check_turn(
        0, exp,
        "Счёт INV-2024-A01 на сумму 12500.00, статус PAID",
        ["invoice_get"], 1,
    )
    assert r.passed


def test_flexible_contains_leave_days_human_format():
    exp = TurnExpectation(user="x", expect_contains=["LEAVE_DAYS="])
    r = check_turn(
        0, exp,
        "У Sergei осталось 14 дней отпуска",
        ["leave_balance"], 1,
    )
    assert r.passed


def test_flexible_contains_quote_human_format():
    exp = TurnExpectation(user="x", expect_contains=["QUOTE", "Enterprise Suite"])
    r = check_turn(
        0, exp,
        "Коммерческое предложение: Enterprise Suite, price_usd=4500",
        ["sales_quote"], 1,
    )
    assert r.passed
