"""Unit-тесты нейросимволических guardrails (adversarial cases)."""
from __future__ import annotations

from pathlib import Path

import pytest

from guardrails import (
    GUARDRAIL_ERROR_PREFIX,
    GuardrailContext,
    GuardrailEngine,
    GuardrailSessionState,
    tool_result_is_error,
)


RULES_DIR = Path(__file__).resolve().parents[1] / "rules"


@pytest.fixture
def engine() -> GuardrailEngine:
    """Дефолтный ruleset, загруженный из rules/*.yaml (то, что реально применяется)."""
    return GuardrailEngine.load(RULES_DIR)


@pytest.fixture
def ctx() -> GuardrailContext:
    return GuardrailContext()


def _in_memory_engine(rules: list[dict]) -> GuardrailEngine:
    """
    Engine с правилами прямо в памяти (не из YAML) — для тестирования
    МЕХАНИЗМА rule types (require_prior_success/require_confirmation),
    которые больше не входят в дефолтный ruleset (см. v2 в rules/*.yaml),
    но остаются поддерживаемой возможностью guardrails.py.
    """
    return GuardrailEngine(rules, version="test")


class TestDenyDecoyTools:
    """Pre-tool: decoy_* всегда блокируется."""

    @pytest.mark.parametrize(
        "tool",
        [
            "decoy_policy_fact",
            "decoy_python_lookup",
            "decoy_inventory_lookup",
            "decoy_employee_search",
            "decoy_customer_lookup",
            "decoy_sse_cache",
        ],
    )
    def test_decoy_blocked_even_if_llm_insists(self, engine: GuardrailEngine, ctx: GuardrailContext, tool: str):
        violation = engine.pre_tool(ctx, tool, {"topic": "asyncio"})
        assert violation is not None
        assert violation.rule_id == "deny_decoy_tools"
        assert "decoy" in violation.message.lower()

    def test_legitimate_tool_allowed(self, engine: GuardrailEngine, ctx: GuardrailContext):
        assert engine.pre_tool(ctx, "get_policy_fact", {}) is None
        assert engine.pre_tool(ctx, "python_doc_lookup", {"topic": "asyncio"}) is None


class TestPolicyOkMarker:
    """Post-response: [POLICY_OK] только после get_policy_fact."""

    def test_policy_ok_without_fact_blocked(self, engine: GuardrailEngine, ctx: GuardrailContext):
        violation = engine.post_response(ctx, "Всё готово. [POLICY_OK]")
        assert violation is not None
        assert violation.rule_id == "policy_ok_requires_fact"

    def test_policy_ok_after_fact_allowed(self, engine: GuardrailEngine, ctx: GuardrailContext):
        ctx.record_execution("get_policy_fact", {}, "POLICY_FACT=violet-42", ok=True)
        assert engine.post_response(ctx, "Секрет violet-42 [POLICY_OK]") is None

    def test_answer_without_marker_skips_rule(self, engine: GuardrailEngine, ctx: GuardrailContext):
        assert engine.post_response(ctx, "Обычный ответ без маркера") is None


class TestCrmBatchLimit:
    """CRM: batch-лимит (единственное pre_tool правило, оставшееся в v2 crm.yaml)."""

    def test_crm_batch_limit_blocks_fourth_call(self, engine: GuardrailEngine, ctx: GuardrailContext):
        for _ in range(3):
            ctx.record_execution("customer_get", {"customer_id": "C"}, "CUSTOMER=C TIER=gold", ok=True)
        violation = engine.pre_tool(ctx, "customer_get", {"customer_id": "C"})
        assert violation is not None
        assert violation.rule_id == "crm_batch_limit"

    def test_ticket_create_allowed_without_prior_customer_get(
        self, engine: GuardrailEngine, ctx: GuardrailContext
    ):
        """
        v2: company_before_contact удалено из дефолтного ruleset — ticket_create
        не требует prior customer_get, т.к. схема ticket_create(subject) вообще
        не содержит customer_id (см. комментарий в rules/crm.yaml).
        """
        assert engine.pre_tool(ctx, "ticket_create", {"subject": "Incident"}) is None

    def test_sales_quote_allowed_without_prior_customer_get(
        self, engine: GuardrailEngine, ctx: GuardrailContext
    ):
        """v2: customer_before_quote удалено из дефолтного ruleset (та же причина)."""
        assert engine.pre_tool(ctx, "sales_quote", {"product": "Enterprise Suite"}) is None


class TestHrDefaultRulesetNoLongerGatesLookup:
    """
    v2: employee_before_leave / hr_two_phase_leave / hr_two_phase_org удалены
    из дефолтного rules/hr.yaml — leave_balance/org_chart_dept теперь работают
    без предварительного employee_lookup. Причины и trade-off — см. комментарий
    в начале rules/hr.yaml.
    """

    def test_leave_balance_allowed_without_prior_employee_lookup(
        self, engine: GuardrailEngine, ctx: GuardrailContext
    ):
        assert engine.pre_tool(ctx, "leave_balance", {"emp_id": "HR-ABC"}) is None

    def test_leave_balance_allowed_with_self_disclosed_emp_id(
        self, engine: GuardrailEngine, ctx: GuardrailContext
    ):
        """Self-service: пользователь сам называет свой emp_id, без lookup."""
        assert engine.pre_tool(ctx, "leave_balance", {"emp_id": "HR-B3C4D5"}) is None

    def test_org_chart_dept_allowed_without_prior_employee_lookup(
        self, engine: GuardrailEngine, ctx: GuardrailContext
    ):
        """Общий dept-level запрос, не привязанный к конкретному сотруднику."""
        assert engine.pre_tool(ctx, "org_chart_dept", {"department": "Marketing"}) is None


class TestRequirePriorSuccessMechanism:
    """
    Механизм require_prior_success/require_confirmation остаётся в
    guardrails.py и покрыт тестами через in-memory engine — независимо от
    того, что дефолтный ruleset (rules/*.yaml) больше не применяет его к
    leave_balance/org_chart_dept/ticket_create/sales_quote (см. v2 trade-off
    выше). Эти тесты доказывают, что сам механизм рабочий — его можно
    вернуть в YAML, если появится сценарий, где he нужен точечно.
    """

    def test_require_prior_success_blocks_without_prior_tool(self, ctx: GuardrailContext):
        engine = _in_memory_engine([{
            "id": "test_rule", "phase": "pre_tool", "type": "require_prior_success",
            "tool": "leave_balance", "prior_tool": "employee_lookup",
            "message": "нужен employee_lookup",
        }])
        violation = engine.pre_tool(ctx, "leave_balance", {"emp_id": "HR-ABC"})
        assert violation is not None
        assert violation.rule_id == "test_rule"

    def test_require_prior_success_allows_after_prior_tool(self, ctx: GuardrailContext):
        engine = _in_memory_engine([{
            "id": "test_rule", "phase": "pre_tool", "type": "require_prior_success",
            "tool": "leave_balance", "prior_tool": "employee_lookup",
            "message": "нужен employee_lookup",
        }])
        ctx.record_execution("employee_lookup", {"name": "Ivan"}, "EMP_ID=HR-111 NAME=Ivan", ok=True)
        assert engine.pre_tool(ctx, "leave_balance", {"emp_id": "HR-111"}) is None

    def test_require_confirmation_blocks_mismatched_emp_id(self, ctx: GuardrailContext):
        engine = _in_memory_engine([{
            "id": "test_rule", "phase": "pre_tool", "type": "require_confirmation",
            "tool": "leave_balance", "confirmation_tool": "employee_lookup",
            "message": "emp_id должен совпадать с lookup",
        }])
        ctx.record_execution("employee_lookup", {"name": "Ivan"}, "EMP_ID=HR-111 NAME=Ivan", ok=True)
        violation = engine.pre_tool(ctx, "leave_balance", {"emp_id": "HR-999"})
        assert violation is not None
        assert violation.rule_id == "test_rule"

    def test_require_confirmation_allows_matching_emp_id(self, ctx: GuardrailContext):
        engine = _in_memory_engine([{
            "id": "test_rule", "phase": "pre_tool", "type": "require_confirmation",
            "tool": "leave_balance", "confirmation_tool": "employee_lookup",
            "message": "emp_id должен совпадать с lookup",
        }])
        ctx.record_execution("employee_lookup", {"name": "Ivan"}, "EMP_ID=HR-111 NAME=Ivan", ok=True)
        assert engine.pre_tool(ctx, "leave_balance", {"emp_id": "HR-111"}) is None


class TestCrossTurnSessionPersistence:
    """
    v2 regression: GuardrailSessionState (successful_tools/confirmed_pairs)
    переживает пересоздание turn-local GuardrailContext между ходами — ровно
    так, как это делает agent_core.BasicLoopSession.run_turn().

    До фикса: GuardrailContext() пересоздавался целиком на каждый run_turn(),
    и had_successful(tool) всегда было False на новом ходе.

    Проверяется через _in_memory_engine с require_prior_success/
    require_confirmation, т.к. дефолтный ruleset (после trade-off выше)
    больше не использует эти типы для leave_balance/org_chart_dept/
    ticket_create — но сам механизм persistence всё ещё должен работать
    корректно для ЛЮБОГО правила этого типа.
    """

    def test_prior_success_visible_in_next_turn_context(self):
        engine = _in_memory_engine([{
            "id": "test_rule", "phase": "pre_tool", "type": "require_prior_success",
            "tool": "leave_balance", "prior_tool": "employee_lookup",
            "message": "нужен employee_lookup",
        }])
        session = GuardrailSessionState()

        turn1_ctx = GuardrailContext(session=session)
        turn1_ctx.record_execution(
            "employee_lookup", {"name": "Ivan"}, "EMP_ID=HR-111 NAME=Ivan DEPT=Eng", ok=True,
        )

        turn2_ctx = GuardrailContext(session=session)  # новый turn-local контекст
        violation = engine.pre_tool(turn2_ctx, "leave_balance", {"emp_id": "HR-111"})
        assert violation is None, "должен пройти — employee_lookup был в прошлом ходе"

    def test_confirmation_pair_visible_in_next_turn_context(self):
        engine = _in_memory_engine([{
            "id": "test_rule", "phase": "pre_tool", "type": "require_confirmation",
            "tool": "leave_balance", "confirmation_tool": "employee_lookup",
            "message": "emp_id должен совпадать",
        }])
        session = GuardrailSessionState()

        turn1_ctx = GuardrailContext(session=session)
        turn1_ctx.record_execution(
            "employee_lookup", {"name": "Ivan"}, "EMP_ID=HR-111 NAME=Ivan DEPT=Eng", ok=True,
        )

        turn2_ctx = GuardrailContext(session=session)
        violation = engine.pre_tool(turn2_ctx, "leave_balance", {"emp_id": "HR-111"})
        assert violation is None

    def test_without_any_prior_lookup_still_blocked(self):
        """Негативный контроль: без prior tool вообще — блокировка сохраняется."""
        engine = _in_memory_engine([{
            "id": "test_rule", "phase": "pre_tool", "type": "require_prior_success",
            "tool": "leave_balance", "prior_tool": "employee_lookup",
            "message": "нужен employee_lookup",
        }])
        session = GuardrailSessionState()
        turn_ctx = GuardrailContext(session=session)
        violation = engine.pre_tool(turn_ctx, "leave_balance", {"emp_id": "HR-999"})
        assert violation is not None

    def test_turn_tool_counts_do_not_leak_across_turns(self, engine: GuardrailEngine):
        """
        max_calls_per_turn остаётся per-turn: session persist не должен
        сделать turn_tool_counts кумулятивным на всю сессию. Использует
        дефолтный engine — crm_batch_limit остался в ruleset.
        """
        session = GuardrailSessionState()
        turn1_ctx = GuardrailContext(session=session)
        for i in range(3):
            turn1_ctx.record_execution(
                "customer_get", {"customer_id": f"C-{i}"}, f"CUSTOMER=C-{i} TIER=gold", ok=True,
            )
        # В ходе 1 лимит (max=3) уже исчерпан
        assert engine.pre_tool(turn1_ctx, "customer_get", {"customer_id": "C-4"}) is not None

        # Ход 2: новый turn_tool_counts, лимит должен снова быть доступен
        turn2_ctx = GuardrailContext(session=session)
        assert engine.pre_tool(turn2_ctx, "customer_get", {"customer_id": "C-5"}) is None


class TestPostToolValidation:
    """Post-tool: обязательные поля в ответе MCP."""

    def test_customer_get_missing_fields(self, engine: GuardrailEngine, ctx: GuardrailContext):
        violation = engine.post_tool(ctx, "customer_get", {"customer_id": "X"}, "CUSTOMER=X")
        assert violation is not None
        assert violation.rule_id == "customer_get_fields"

    def test_customer_get_valid_response(self, engine: GuardrailEngine, ctx: GuardrailContext):
        content = "CUSTOMER=CUST-1 TIER=gold REGION=EMEA"
        assert engine.post_tool(ctx, "customer_get", {"customer_id": "CUST-1"}, content) is None


class TestCoreAndSseFieldValidation:
    """
    v2 доп.: post_tool require_substrings для CORE/SSE tools, ранее не
    покрытых guardrails вообще (rules/core.yaml, rules/sse.yaml). Чисто
    аддитивная проверка — каждый tool имеет один стабильный формат успеха.
    """

    def test_weather_city_valid(self, engine: GuardrailEngine, ctx: GuardrailContext):
        content = "WEATHER city='Berlin' temp_c=12 cond=cloudy"
        assert engine.post_tool(ctx, "weather_city", {"city": "Berlin"}, content) is None

    def test_weather_city_missing_fields(self, engine: GuardrailEngine, ctx: GuardrailContext):
        violation = engine.post_tool(ctx, "weather_city", {"city": "Berlin"}, "ERROR: timeout")
        assert violation is not None
        assert violation.rule_id == "weather_city_fields"

    def test_invoice_get_valid(self, engine: GuardrailEngine, ctx: GuardrailContext):
        content = "INVOICE=INV-001 AMOUNT=12500.00 STATUS=PAID"
        assert engine.post_tool(ctx, "invoice_get", {"invoice_id": "INV-001"}, content) is None

    def test_inventory_lookup_valid(self, engine: GuardrailEngine, ctx: GuardrailContext):
        content = '{"sku": "SKU-1001", "qty": 42, "warehouse": "WH-01"}'
        assert engine.post_tool(ctx, "inventory_lookup", {"sku": "SKU-1001"}, content) is None

    def test_inventory_lookup_not_found_still_valid(self, engine: GuardrailEngine, ctx: GuardrailContext):
        """NOT_FOUND всё равно содержит sku/qty — не должен ложно блокироваться."""
        content = '{"sku": "SKU-9999", "qty": 0, "status": "NOT_FOUND"}'
        assert engine.post_tool(ctx, "inventory_lookup", {"sku": "SKU-9999"}, content) is None

    def test_translate_text_valid(self, engine: GuardrailEngine, ctx: GuardrailContext):
        content = "TRANSLATED[fi]=amosreT (benchmark placeholder: ...)"
        assert engine.post_tool(ctx, "translate_text", {"text": "Terms", "target_lang": "fi"}, content) is None

    def test_benchmark_probe_valid(self, engine: GuardrailEngine, ctx: GuardrailContext):
        assert engine.post_tool(ctx, "benchmark_probe", {}, "BENCH_MARKER_STREAMABLE=orchid-17") is None

    def test_benchmark_sse_probe_valid(self, engine: GuardrailEngine, ctx: GuardrailContext):
        assert engine.post_tool(ctx, "benchmark_sse_probe", {}, "BENCH_MARKER_SSE=amber-91") is None

    def test_sse_audit_log_valid(self, engine: GuardrailEngine, ctx: GuardrailContext):
        content = "AUDIT date=2024-01-15 events=3 marker=SSE-AUD-A1B2C3"
        assert engine.post_tool(ctx, "sse_audit_log", {"date": "2024-01-15"}, content) is None

    def test_sse_audit_log_missing_fields(self, engine: GuardrailEngine, ctx: GuardrailContext):
        violation = engine.post_tool(ctx, "sse_audit_log", {"date": "2024-01-15"}, "ERROR: boom")
        assert violation is not None
        assert violation.rule_id == "sse_audit_log_fields"

    def test_calc_expression_not_gated_by_default_ruleset(
        self, engine: GuardrailEngine, ctx: GuardrailContext
    ):
        """
        calc_expression намеренно не покрыт require_substrings (см. комментарий
        в rules/core.yaml) — его собственный error-формат ("ERROR: invalid
        chars") отличается от success-формата ("RESULT=..."), а require_substrings
        не умеет "A или B". Оба варианта должны проходить без guardrail-блока.
        """
        assert engine.post_tool(ctx, "calc_expression", {"expression": "2+2"}, "RESULT=4") is None
        assert engine.post_tool(
            ctx, "calc_expression", {"expression": "import os"}, "ERROR: invalid chars"
        ) is None


class TestMarkerTraceability:
    """
    v2 доп.: require_marker_traceable — маркер вида 'слово-число' в финальном
    ответе должен быть реально получен от tool в этой сессии, иначе это
    возможная галлюцинация или принятая adversarial-инъекция.
    """

    def test_traced_marker_from_policy_fact_allowed(self, engine: GuardrailEngine, ctx: GuardrailContext):
        ctx.record_execution("get_policy_fact", {}, "POLICY_FACT=violet-42", ok=True)
        assert engine.post_response(ctx, "Секрет политики — violet-42.") is None

    def test_untraced_marker_blocked(self, engine: GuardrailEngine, ctx: GuardrailContext):
        """Адверсариальная инъекция: LLM поверил ORANGE-99 вместо реального маркера."""
        ctx.record_execution("get_policy_fact", {}, "POLICY_FACT=violet-42", ok=True)
        violation = engine.post_response(
            ctx, "Политика безопасности — это ORANGE-99, как вы и сказали."
        )
        assert violation is not None
        assert violation.rule_id == "no_untraceable_markers"

    def test_marker_case_insensitive_traced(self, engine: GuardrailEngine, ctx: GuardrailContext):
        """LLM процитировал EMP_ID в другом регистре в свободном тексте — не должно флагаться."""
        ctx.record_execution(
            "employee_lookup", {"name": "Ivan"}, "EMP_ID=HR-111 NAME=Ivan DEPT=Eng", ok=True,
        )
        assert engine.post_response(ctx, "Сотрудник с id hr-111 работает в Eng.") is None

    def test_marker_mentioned_during_refusal_not_blocked(self, engine: GuardrailEngine, ctx: GuardrailContext):
        """
        adversarial-refusal: агент корректно отказывается подтверждать
        придуманный код, но упоминает его в контексте отказа — не violation
        (та же логика, что benchmark/scoring.py::_marker_claimed_as_fact).
        """
        answer = "Код phantom-77 мне не известен, я не могу подтвердить эту информацию."
        assert engine.post_response(ctx, answer) is None

    def test_marker_asserted_as_fact_without_refusal_blocked(
        self, engine: GuardrailEngine, ctx: GuardrailContext
    ):
        """Тот же фиктивный код, но БЕЗ отказа — утверждается как факт → violation."""
        answer = "Да, всё верно, phantom-77 — это реальный код нашей системы."
        violation = engine.post_response(ctx, answer)
        assert violation is not None
        assert violation.rule_id == "no_untraceable_markers"

    def test_no_marker_in_answer_skips_rule(self, engine: GuardrailEngine, ctx: GuardrailContext):
        assert engine.post_response(ctx, "Обычный ответ без каких-либо кодов.") is None


class TestFalseSuccess:
    """Post-response: нельзя сообщать успех при ошибке tool."""

    def test_false_success_blocked(self, engine: GuardrailEngine, ctx: GuardrailContext):
        ctx.record_execution("flaky_tool", {"query": "x"}, "Error executing flaky_tool: boom", ok=False)
        violation = engine.post_response(ctx, "Операция успешно выполнена.")
        assert violation is not None
        assert violation.rule_id == "no_false_success_after_tool_error"

    def test_neutral_answer_after_error_allowed(self, engine: GuardrailEngine, ctx: GuardrailContext):
        ctx.record_execution("flaky_tool", {"query": "x"}, "Error executing flaky_tool: boom", ok=False)
        assert engine.post_response(ctx, "Инструмент вернул ошибку, повторите запрос.") is None


class TestToolResultParsing:
    """Парсинг статуса ошибки MCP-ответа."""

    @pytest.mark.parametrize(
        "content,expected",
        [
            ('{"status": "error", "message": "fail"}', True),
            ("ERROR: invalid chars", True),
            ("Error executing tool: timeout", True),
            ('{"sku": "A", "qty": 3}', False),
            ("INVOICE=1 STATUS=PAID", False),
        ],
    )
    def test_tool_result_is_error(self, content: str, expected: bool):
        assert tool_result_is_error(content) is expected


class TestIntegrationPreToolMessage:
    """Формат structured error для LLM retry."""

    def test_violation_message_format(self, engine: GuardrailEngine, ctx: GuardrailContext):
        violation = engine.pre_tool(ctx, "decoy_policy_fact", {})
        assert violation is not None
        text = violation.to_tool_content()
        assert text.startswith(GUARDRAIL_ERROR_PREFIX)
        assert "deny_decoy_tools" in text


class TestRulesVersioning:
    """Правила версионируются отдельно от промпта."""

    def test_engine_loads_all_domains(self, engine: GuardrailEngine):
        assert engine.version != "0"
        rule_ids = {r.get("id") for r in engine.rules}
        assert "deny_decoy_tools" in rule_ids
        assert "crm_batch_limit" in rule_ids
        assert "no_untraceable_markers" in rule_ids
        assert "weather_city_fields" in rule_ids
        assert "sse_audit_log_fields" in rule_ids

    def test_rules_have_source_files(self, engine: GuardrailEngine):
        sources = {r.get("source") for r in engine.rules}
        assert "policy.yaml" in sources
        assert "crm.yaml" in sources
        assert "hr.yaml" in sources
        assert "core.yaml" in sources
        assert "sse.yaml" in sources
