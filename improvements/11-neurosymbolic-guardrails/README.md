# 11 — Нейросимволические guardrails

**Tier:** 3 (compliance-критичные системы)  
**Источник:** AWS / Elizabeth Fuentes  
**Проблема:** Промпт — рекомендация; бизнес-правила можно нарушить на любом вызове

> **v2 (два раунда фикса после аудита, baseline SR=21% — лучший результат до
> переработки):**
>
> **Раунд 1 — cross-turn state bug:** `GuardrailContext` пересоздавался
> целиком на каждый `run_turn()`, из-за чего правила `require_prior_success`/
> `require_confirmation` ошибочно блокировали легитимные цепочки, разнесённые
> по двум ходам диалога — 40 таких пар в каталоге. Состояние разделено на
> persistent `GuardrailSessionState` (successful_tools, confirmed_pairs —
> живёт всю сессию) и per-turn `GuardrailContext` (turn_tool_counts).
> Убрал 38→18 affected сценариев.
>
> **Раунд 2 — над-строгие workflow-правила:** офлайн-симуляция по всему
> каталогу (166 сценариев, честная замена `GuardrailContext`/`session` без
> реального LLM) показала, что 18 сценариев всё ещё ложно блокируются —
> но уже **другой** причиной: `hr_two_phase_org`/`company_before_contact`/
> `customer_before_quote` требовали prior lookup безусловно, хотя схема
> `ticket_create(subject)`/`sales_quote(product)`/`org_chart_dept(department)`
> вообще не содержит `customer_id`/`emp_id` — правило структурно не может
> быть привязано к конкретной сущности. Правила удалены из дефолтного
> ruleset (см. rules/hr.yaml, rules/crm.yaml для полного разбора trade-off).
> **Итог симуляции: 38 → 18 → 0 сценариев с false-block.**
>
> Также убран дублирующийся мёртвый код и двойной `continue` (та же
> категория багов, что чинили в 05/07/09/10).
>
> **Раунд 3 — новые аддитивные правила (не над-строгие, только усиливают):**
> (a) `rules/core.yaml`/`rules/sse.yaml` — post_tool field-валидация для 7
> CORE/SSE tools, ранее не покрытых guardrails вообще (weather_city,
> invoice_get, inventory_lookup, translate_text, benchmark_probe,
> benchmark_sse_probe, sse_audit_log). `calc_expression` намеренно исключён
> (два легитимных формата успеха/ошибки, require_substrings не умеет "A или B").
> (b) `no_untraceable_markers` (post_response) — новый тип правила
> `require_marker_traceable`: сканирует финальный ответ на паттерн
> `слово-число`, и для каждого найденного маркера проверяет, что он реально
> получен от какого-то tool в этой сессии. Ловит и случайную галлюцинацию,
> и adversarial-инъекцию (`ORANGE-99` вместо `violet-42`). Не флагает
> маркер, упомянутый в контексте явного отказа (та же логика, что
> `benchmark/scoring.py::_marker_claimed_as_fact`) — иначе ломало бы
> корректные adversarial-refusal ответы. Проверено офлайн-симуляцией по
> всему каталогу — 0 false positives при реалистичных tool-ответах.
>
> **Известное ограничение (не фикшено):** `policy_ok_requires_fact`
> (существовал до этой переработки) ложно блокирует `s09_009` turn 0 —
> там `[POLICY_OK]` подтверждает НОВОЕ правило форматирования диалога от
> пользователя, а не факт политики. Не исправлено намеренно — надёжно
> отличить "подтверждение формата" от "утверждение факта" без риска
> over-generalization (той же категории, что уже отклонили для anaphora-
> эвристики в 10) требует отдельного дизайна. См. JUDGE.md.
>
> См. IMPLEMENTATION.md для полного разбора всех трёх раундов.

## Суть проблемы

Правила в system prompt или docstrings инструментов — **предложения**, не ограничения. Модель решает следовать им на каждом шаге заново.

Типичные functional hallucinations, которые ловят guardrails:

| Подтип | Пример |
|--------|--------|
| Parameter errors | `guests=15` при max=10 |
| Completeness errors | Действие без обязательной проверки оплаты |
| Tool bypass | SUCCESS без вызова validation tool |
| False success | Отчёт об успехе при failed MCP response |

## Архитектура

```
LLM proposes action
       ↓
Framework hook (КОД, не LLM)
       ↓
  Business rules engine
       ↓ pass              ↓ fail
Execute              Cancel + structured error → optional retry
```

Правила живут **ниже** контроля LLM — их нельзя «забыть» через дрейф промпта.

## Типы правил для Basic

### Pre-tool hooks

- Запрет `decoy_*` tools всегда
- Обязательный `get_policy_fact` перед ответом с `[POLICY_OK]`
- Лимиты на batch-операции CRM

### Post-tool hooks

- Проверка: ответ MCP содержит ожидаемые поля
- Запрет сообщать пользователю success если `status=error`

### Workflow hooks

- Цепочка: `fetch_company` → OK → только тогда `fetch_contact`
- Финансовые/HR: двухфазное подтверждение

## Результаты (AWS demo)

3/3 невалидных операции заблокированы **без изменений** в tools и промптах.

## Отличие от 02

| 02 Validation | 11 Guardrails |
|---------------|---------------|
| JSON Schema (типы, required) | Доменная логика |
| Универсально | Policy-as-code |
| Tier 1 | Tier 3 |

Стек: **02 → 11 → execute**

## Что менять в Basic

| Место | Действие |
|-------|----------|
| `langchain_mcp_adapters.interceptors` | Pre/post hooks |
| `rules/` | YAML/Python правила по доменам |
| `benchmark/scenarios/catalog/s10_adversarial.yaml` | Тесты обхода |

## Критерии успеха

- Adversarial сценарии: 0 false success
- `tool_forbidden` блокируется даже если LLM «настаивает»
- Правила версионируются отдельно от промпта (audit trail)

## Зависимости

- **02** (схема до бизнес-правил)

## Чеклист

- [x] Каталог бизнес-правил Basic (HR, CRM, policy)
- [x] Pre-tool interceptor с deny/allow lists
- [x] Post-tool: verify MCP status vs agent message
- [x] Unit-тесты на каждое правило
- [x] Документация: как добавить правило без смены промпта
