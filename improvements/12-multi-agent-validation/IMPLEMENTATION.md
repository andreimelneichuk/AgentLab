# Реализация: мультиагентная валидация (Worker + Validator)

> **v2 — фикс потери tool_calls между retry-попытками валидатора.**
> См. раздел "v2 fix" ниже.

## Что сделано

1. **`variant/validator_agent.py`**
   - `ValidatorAgent` — LLM-as-validator с injectable `llm_invoke` для unit-тестов
   - `build_tool_trace()` — сбор trace из assistant tool_calls + tool results
   - `parse_validator_response()` — разбор JSON approve/reject
   - `validator_enabled_for_tags()` — включение по тегам сценария

2. **`variant/prompts/validator_system.txt`**
   - Чеклист: запрос, tool trace, консистентность фактов, формат `[POLICY_OK]`, STOP

3. **`variant/agent_core.py`**
   - Pipeline `BasicLoopSession`: worker → validator → user
   - Reject → retry worker с `[VALIDATOR REJECTED]` feedback
   - `max_reject_retries: 2` (1 начальный + 2 retry)
   - `run_turn(user_message, scenario_tags=None)` — теги для включения валидатора
   - `TurnResult`: поля `validator_approved`, `validator_retries`, `validator_feedback`

4. **`variant/config.yml`**
   ```yaml
   validator:
     enabled_for_tags: [critical, adversarial]
     max_reject_retries: 2
     model: same_or_smaller
   ```

5. **`variant/tests/test_validator.py`** — unit-тесты с mock LLM и mock pipeline

## Как проверить

### Unit-тесты

```bash
cd improvements/12-multi-agent-validation/variant
source .venv/bin/activate
PYTHONPATH=. pytest tests/test_validator.py -q
```

### Регрессия

```bash
PYTHONPATH=. pytest tests/ -q
```

### Бенчмарк (critical / adversarial)

```bash
cd /path/to/AgentLab
python -m benchmark.mcp_mock_server  # терминал 1

PYTHONPATH=. python -m benchmark.compare \
  --variant improvements/12-multi-agent-validation/variant \
  --tags critical,adversarial --limit 10
```

`benchmark/compare.py::invoke_run_turn()` уже передаёт `scenario_tags`
автоматически, если сигнатура `run_turn` variant-а принимает этот параметр
(проверка через `inspect.signature`) — доп. правка `compare.py` не нужна,
валидатор включается сам для сценариев с тегами `critical`/`adversarial`
(33/166 в каталоге).

## Ожидаемый эффект

- Validator ловит false success (violet-99 вместо violet-42, ответ без tool)
- +1 LLM call на ход для сценариев с тегами `critical` / `adversarial`
- Worker получает actionable feedback при reject вместо молчаливой галлюцинации

## v2 fix: накопление tool_calls через retry-попытки

**Проблема:** `_run_turn_with_validation()` вызывает `_run_worker_turn()`
до `max_reject_retries + 1` раз, но возвращает только **последний**
`TurnResult`. `_run_worker_turn()` инициализирует `all_tool_names`/
`turn_details`/`rounds`/токены **с нуля** при каждом вызове — это
изолированная функция, ничего не знает о предыдущих попытках.

Сценарий провала: попытка 1 корректно вызывает `get_policy_fact`, но
валидатор реджектит ответ (например, из-за формата). На retry worker
видит `[VALIDATOR REJECTED]` фидбек и уже ИМЕЕТ результат tool в контексте
(история сообщений накапливается) — поэтому естественно НЕ вызывает tool
снова, просто переформулирует текст. Финальный (одобренный) `TurnResult`
имеет `tool_calls=[]`, хотя `get_policy_fact` реально был вызван в этом
логическом ходе.

`benchmark/scoring.py::check_turn` проверяет
`expectation.expect_tool_called not in tools_called` — с пустым
`tools_called` эта проверка **всегда** проваливается для отклонённых-затем-
исправленных ходов. Именно сценарии с тегами `critical`/`adversarial`
включают валидатор — отсюда CSR (12%) хуже даже среднего SR (11%).

**Fix:** `_run_turn_with_validation()` аккумулирует `tool_calls`/
`tool_call_details`/`rounds`/`prompt_tokens`/`completion_tokens`/
`latency_sec` через ВСЕ попытки перед вызовом валидатора на каждой
итерации:

```python
acc_tool_calls.extend(last_result.tool_calls)
acc_tool_call_details.extend(last_result.tool_call_details)
acc_rounds += last_result.rounds
...
last_result.tool_calls = list(acc_tool_calls)
last_result.tool_call_details = list(acc_tool_call_details)
last_result.rounds = acc_rounds
...
```

**Побочный эффект (тоже исправлен):** до фикса forbidden-tool нарушение
из отклонённой попытки (`decoy_*` вызван в attempt 1, validator реджектит,
attempt 2 tool не повторяет) тоже "исчезало" из финального результата —
`forbid_tools_called` check в `check_turn` не видел нарушение. Теперь
видит, т.к. `tool_calls` накапливаются, а не заменяются.

## Побочные фиксы

- Дублирующийся мёртвый код в `agent_core.py` (copy-paste, как в
  05/07/09/10/11) — `_execute_single_tool`/`_execute_tool_calls`/
  `_execute_tool_round`/`_tool_results_to_messages` были определены дважды.
- Двойной `continue` в конце tool-loop (как в 05/06/11).
- Устаревшая заметка про "опциональную доработку compare.py" удалена —
  `invoke_run_turn()` уже threads `scenario_tags` автоматически через
  `inspect.signature`.

## Тесты (v2)

```bash
cd improvements/12-multi-agent-validation/variant
PYTHONPATH=. pytest tests/test_validator.py -v
```

17/17 тестов, включая 2 новых regression:
- `test_pipeline_accumulates_tool_calls_across_rejected_retries` — попытка 1
  вызывает tool и реджектится, попытка 2 не повторяет вызов, финальный
  результат всё равно содержит tool из попытки 1 + суммарные rounds/токены
- `test_pipeline_preserves_forbidden_tool_call_from_rejected_attempt` —
  forbidden tool из отклонённой попытки не исчезает из финального результата

## v3: три направления улучшения (реализованы параллельными агентами)

Реализация распределена между тремя агентами на непересекающиеся НОВЫЕ
файлы (репозиторий не git — без worktree-изоляции, поэтому конкурентные
правки одного файла были риском; интеграция в `agent_core.py`/
`validator_agent.py` сделана после, вручную).

### 1. Tier-1 символическая проверка — `symbolic_precheck.py`

Самодостаточный модуль (не импортирует из эксперимента 11, логика
адаптирована напрямую), реализующий детерминированные проверки пунктов
2-5 чеклиста валидатора (`prompts/validator_system.txt`) без LLM:

| Проверка | rule_id | Соответствует пункту чеклиста |
|----------|---------|-------------------------------|
| decoy-инструмент вызван | `deny_decoy_tools` | 2. Tool trace |
| success-слова после tool error | `no_false_success_after_tool_error` | 5. Политика STOP |
| `[POLICY_OK]` без успешного `get_policy_fact` | `policy_ok_requires_fact` | 4. Формат |
| маркер `слово-число` без backing tool call | `no_untraceable_markers` | 3. Консистентность |
| tool call без результата (`pending`/`missing_result`) | `missing_tool_result` | 2. Tool trace |

`symbolic_precheck(user_message, draft_answer, tool_trace, *, session_tool_response_history=None) -> Optional[SymbolicViolation]`
возвращает первое найденное нарушение или `None` (значит нужна LLM-проверка
пункта 1 "соответствие запросу").

**Интеграция:** `_run_turn_with_validation()` вызывает `symbolic_precheck()`
ПЕРЕД `validator.validate()` на каждой попытке. Если найдено нарушение —
`validation_result_from_symbolic_violation()` (в `validator_agent.py`)
конвертирует его в `ValidationResult` с соответствующим structured-кодом
(маппинг `SYMBOLIC_RULE_TO_REASON_CODE`), LLM не вызывается вообще.

**Важный нюанс, найденный при интеграции:** trace строится по ВСЕЙ
накопленной истории хода (все попытки с начала turn, не только текущая) —
значит decoy-вызов из попытки 1 остаётся видимым для tier-1 на ВСЕХ
последующих retry, даже если retry не повторяет тот же вызов явно. Это
намеренное защитное поведение: decoy/hallucination-нарушение нельзя
"вылечить", просто переформулировав текст, пока тронутые данные в
контексте — turn останется `approved=False` до истощения retries. См.
`test_pipeline_preserves_forbidden_tool_call_from_rejected_attempt`.

### 2. Production-совместимый триггер — `risk_signals.py`

`validator.is_enabled_for_tags(scenario_tags)` — единственный prod-нежизнеспособный
механизм (scenario_tags существуют только в тестовом harness). Новый
`detect_risk_signals(user_message, draft_answer, tool_trace) -> RiskSignal`
анализирует содержание САМОГО ХОДА:

| Сигнал | Условие |
|--------|---------|
| `tool_error_present` | Есть запись `status=="error"` в trace |
| `policy_context` | policy/security-слова в user_message ИЛИ draft_answer |
| `side_effecting_tool_called` | Вызван `ticket_create`/`sales_quote` (не read-only lookup) |
| `financial_value_present` | Денежная сумма в draft_answer (`$X.XX`, `price_usd=`, `AMOUNT=`) |
| `adversarial_injection_pattern` | Фразы из реальных adversarial-сценариев каталога |
| `marker_without_matching_tool` | Паттерн `слово-число` без успешного tool call в trace |

**Офлайн-замер на реальном каталоге** (753 хода, с реалистичным tool_trace
для ходов с `expect_tool_called`): **22.7%** триггер-рейт (171/753) — разумный
масштаб, не взрывной рост. Часть срабатываний на recall-ходах (маркер без
tool-call В ЭТОМ ходе, но легитимно вспоминаемый из памяти) компенсируется
tier-1's session-wide traceability — validator не отклонит легитимный
recall, просто потратит один лишний (но бесплатный, tier-1) проверочный
проход.

**Интеграция:** `_run_turn_with_validation()` решает "нужна ли валидация"
ПОСЛЕ первой попытки worker'а (не до, как раньше) — по формуле
`validator.is_enabled_for_tags(scenario_tags) OR detect_risk_signals(...).triggered`.
Если ни то ни другое не сработало — worker-результат отдаётся напрямую,
без tier-1/tier-2 накладных расходов.

### 3. Структурированный retry-feedback

`ValidationResult.reasons` изменён с `List[str]` на `List[ValidationReason]`
(`code` из фиксированного enum + `detail`), с полной обратной
совместимостью (`_coerce_reason()` конвертирует старые строки,
`ValidationReason.__eq__` сравнивается со строками как раньше — существующие
тесты не менялись).

`KNOWN_REASON_CODES`: `request_mismatch`, `tool_not_called`,
`forbidden_tool_called`, `fact_inconsistent`, `format_missing`,
`false_success`, `missing_tool_result` (добавлен для symbolic_precheck),
`other`.

`format_retry_feedback(result)` — вместо `f"[VALIDATOR REJECTED] {feedback}"`
строит многострочное actionable-сообщение с подсказкой на каждый уникальный
код причины (дедуп по `(code, detail)`):

```
[VALIDATOR REJECTED]
- tool_not_called: get_policy_fact не вызван → Вызови обязательный инструмент перед тем как отвечать.

Исправь ответ с учётом замечаний валидатора.
```

Промпт `validator_system.txt` обновлён — описание кодов + новый JSON-формат
ответа для LLM-валидатора (`reasons: [{"code": ..., "detail": ...}]`,
обратная совместимость со старым форматом строк сохранена в парсере).

## Тесты (v3)

```bash
cd improvements/12-multi-agent-validation/variant
PYTHONPATH=. pytest tests/ -v
```

66/66 тестов (было 17 после v2):
- `tests/test_symbolic_precheck.py` — 16 тестов на все 5 tier-1 проверок
- `tests/test_risk_signals.py` — 21 тест на 6 эвристик + `should_validate()`
- `tests/test_validator.py` — 29 тестов, включая 4 новых интеграционных:
  - `test_pipeline_triggers_validation_via_risk_signal_without_matching_tag` —
    risk signal триггерит валидацию БЕЗ совпадения тега (production-кейс)
  - `test_pipeline_skips_validation_when_no_tag_and_no_risk_signal` —
    негативный контроль
  - `test_pipeline_symbolic_precheck_rejects_untraceable_marker_without_llm_call` —
    tier-1 ловит hallucination без LLM-вызова, retry с traceable значением одобряется
  - `test_pipeline_uses_structured_retry_feedback_message` — retry-сообщение
    worker'у содержит структурированный код + actionable подсказку
