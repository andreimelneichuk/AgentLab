# Judge Report — 08-semantic-tool-selection (v2: Hybrid, v3: fix)

> **Переработка с v1:** Pure semantic (SR=11%, TSA=56%) → Hybrid routing + semantic (Expected SR=20-25%, TSA=85-90%)
>
> **v3 update:** v2's "Expected" numbers above were never met — a real e2e
> benchmark run on the full 189-scenario catalog measured **TSA=67%,
> TAA=60%**, i.e. v2 was WORSE at its one job (tool selection) than the
> 96%-TSA baseline it was supposed to improve on. Root cause found and
> fixed; see "v3: Post-mortem" section below. Offline verification (no
> LLM) confirms the fix; e2e re-benchmark has NOT been run in this
> session — do not treat the offline numbers as a substitute for a fresh
> e2e run before declaring this Beta/Prod-ready.

**Оценка оконченности v2 (для истории — этот прогноз оказался неверным):** 80/100 — **Alpha (требует тестов и бенчмарка)**

**Оценка оконченности v3 (после fix):** 70/100 — **Alpha+ (root cause найден и
верифицирован offline; тесты добавлены; e2e-бенчмарк ещё НЕ переподтверждён)**

## Компоненты

| Компонент | Статус | Значение |
|-----------|--------|----------|
| Spec (README) | ✅ обновлена | Hybrid архитектура описана |
| Routing (routing.py) | ✅ реализовано | Keywords + sticky RouterState |
| Semantic (tool_retriever.py) | ✅ обновлено | hybrid_select_tool_names function |
| Integration (agent_core.py) | ✅ обновлено | _select_tools_hybrid, router_state |
| Config | ✅ без изменений | tool_selection section |
| Tests (routing) | ⏳ требует написания | ~8 тестов на routing logic |
| Tests (hybrid) | ⏳ требует обновления | Старые тесты адаптировать |
| Бенчмарк | 🔮 | SR=20-25% (было 11%, baseline 26%) |

## v1 → v2 Почему переработка

### v1 (Pure Semantic) проблемы
- SR=11% (очень низко)
- TSA=56% (семантика выбирает неправильный tool)
- TF-IDF недостаточен для точного выбора из 25 tools

### v2 (Hybrid) решение
- ✅ Routing сокращает candidate list до 5-8 tools
- ✅ Semantic внутри домена точнее
- ✅ Keyword matching ловит явные упоминания
- ✅ Decoy фильтруются на этапе routing (не полагаемся на similarity)
- ✅ Sticky routing: HR домен остаётся активным для связанных вопросов

## Ожидаемые метрики (v2)

| Метрика | v1 (Pure) | v2 (Hybrid) | Baseline |
|---------|-----------|------------|----------|
| SR | 11% | **20-25%** | 26% |
| TSA | 56% | **85-90%** | 95% |
| TA | 30% | **40-45%** | 49% |
| r_dt | высокий | **↓ (routing)** | - |
| Tokens (tools) | 4500 | **1200-1500** | 4500 |
| Latency | 0.96s | **0.80s** | 0.73s |

## Что осталось (перед бенчмарком)

1. **Написать тесты routing.py** (~8 тестов)
   - keyword matching для каждого домена
   - sticky activated_domains
   - decoy exclusion
   - mandatory inclusion

2. **Обновить тесты tool_retriever.py**
   - hybrid_select_tool_names с router_state
   - keyword matching в hybrid
   - fallback when routing empty

3. **Регрессионные тесты agent_core.py**
   - _select_tools_hybrid вызывается корректно
   - router_state сбрасывается в reset()

4. **E2E бенчмарк** (требует vLLM)
   - Запустить на catalog и проверить SR ≥ 20%

## Рекомендации

1. **Немедленно:** Написать unit-тесты для routing и hybrid selection (2 часа)
2. **Затем:** Запустить регрессионные тесты agent_core (1 час)
3. **E2E:** Бенчмарк на catalog (требует доступ к llm-server)

## Преимущества Hybrid подхода

| Аспект | v1 (Pure Semantic) | v2 (Hybrid) |
|--------|-------------------|------------|
| Выбор инструмента | Ненадёжный (TF-IDF может выбрать похожий) | Надёжный (routing гарантирует домен) |
| Decoy handling | Слабый (полагаемся на similarity) | Сильный (routing исключает) |
| Recall сценарии | Плохие (tool может не быть в top-5) | Хорошие (recall tools в mandatory + keyword match) |
| Токены | Экономим до 73% (4500 → 1200) | Экономим до 73% |
| Latency | Нормальное (TF-IDF fast) | Чуть лучше (меньше candidates) |

## Бонусные идеи (Future)

- **Dynamic domain discovery:** Если router не нашёл домен, спросить LLM
- **Confidence scoring:** Если semantic score низкий, расширить к 8-10 tools
- **Domain persistence:** Сохранять router_state между сессиями
- **A/B тест:** Hybrid vs Pure vs Routing-only

---

## v3: Post-mortem — v2's "Expected" table was wrong, measured TSA collapsed to 67%

### What was measured (real e2e run, full 189-scenario catalog)

```
baseline:      solve=69% critical=58% turn=88% TSA=96%(420/439)
08 (measured): solve=50% critical=51% turn=68% TSA=67%(294/439) TAA=60%(165/277)
```

This is a 29pp TSA drop and ~31pp TAA drop, far outside the 5-9pp
run-to-run noise band measured on this catalog size for a technique
whose entire job is picking the right tool. v2's "Expected" table (SR
20-25%, TSA 85-90%) had never actually been checked against reality —
it was a forecast, not a measurement, and the forecast was wrong in the
wrong direction.

### Root cause (verified with an offline script, not just code reading)

`hybrid_select_tool_names()` applied a `top_k`-based semantic cut ON TOP
OF the routing-approved candidate list:

```python
if len(selected) >= config.top_k + len(config.mandatory):
    break
```

`Domain.CORE` alone contains 12 tools that are always in the candidate
set — already more than `top_k=5`. `TfidfToolRetriever` has no stemming,
so paraphrased/declined Russian queries frequently score `0.0` against
every candidate (verified directly: the query "Покажи структуру отдела
Engineering." scored `0.0000` for all 25 registered tools, including the
correct `org_chart_dept`). With all-zero ties, Python's stable sort keeps
alphabetical corpus order, so the `top_k` cut silently favored
alphabetically-earlier CORE tools over the actually-correct tool.
Separately, `graph_query` (added later by 14-graph-rag) was never
assigned to any `Domain` at all, so routing never allowed it regardless
of score. And `DOMAIN_KEYWORDS` had several gaps (bare "HR", "профиль",
"штат" for HR; "КП"/"карточка"/"CUST-" for CRM).

Offline script: walk `benchmark/scenarios/**/*.yaml`, for every turn with
`expect_tool_called` call `hybrid_select_tool_names()` directly (no LLM),
check survival in the shortlist.

```
Before fix: 501 turns, 418 hits, 83 misses  → 16.6% miss rate
After fix:  501 turns, 498 hits,  3 misses  →  0.6% miss rate
```

A 16.6% pre-filter exclusion rate on tool-selection-relevant turns is
consistent with (and closely explains) the measured 29pp e2e TSA
regression.

### Fix applied

1. Stopped double-cutting: routing-approved candidates are no longer
   re-filtered to `top_k` by semantic score. Semantic score is now only
   used as an ordering/last-resort trim if the candidate set balloons
   past a generous safety cap (`top_k * 4`).
2. `graph_query` added to `Domain.CORE`.
3. `DOMAIN_KEYWORDS` gaps closed (HR: "hr", "профиль", "штат"; CRM:
   "карточк", "коммерческое предложение", "кп", "cust-"), with
   word-boundary matching for short/ambiguous keywords to avoid new
   false positives.
4. 18 new regression tests added (`tests/test_hybrid_selection.py`,
   plus additions to `tests/test_routing.py`) capturing the exact failing
   user messages found by the offline script. Full suite: 54/54 passing.

### Honest residual gap

**0.6% offline miss rate remains (3/501)** — `s08_002`, `s08_004`,
`s08_006`, all `employee_lookup` cases with zero domain-keyword signal at
all (just a person's name + words like "дежурного"/"на месте"/"офицера
безопасности"). This needs NER or an LLM domain classifier to close
fully; deliberately not added a Tier-2 LLM call here to avoid repeating
13-buddy-system's unconditional-LLM-call latency regression.

**Not yet done:** a fresh e2e benchmark run against the fixed code has
NOT been executed in this session. The 0.6% number is a offline proxy for
the tool-selection layer only — it does not by itself prove the e2e TSA
will land back near 96%, since the LLM's own tool-arg accuracy and the
scoring harness's TAA/critical/solve metrics involve additional factors
this fix does not touch. Recommend running the full e2e benchmark before
upgrading this technique's completion status further.
