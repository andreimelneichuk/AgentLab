# Judge Summary — оконченность доработок (честная оценка)

Рубрика: `improvements/judge/RUBRIC.md`  
Бенчмарк baseline: SR **26%**, TSA **95%**, AH **57%**, lat **0.73s**  
Дата: 2026-07-01

> Старые автоматические оценки 93–100/100 **завышены**. Ниже — по ревью code-reviewer субагентов с учётом SR и критериев спеки.

| # | Доработка | Оконченность | Вердикт | SR | Δ SR | Главный пробел |
|---|-----------|------------:|---------|---:|-----:|----------------|
| **01** | four-section-prompt | **81** | Beta | 32% | +6 | Чеклист 5/6: smoke A/B не закрыт |
| **14** | graph-rag | **82** | Beta | 24% | −2 | `graph_rag.yaml`: `min_tool_calls_delta` для local tool |
| **15** | memory-formation | **84** | Beta | 26% | 0 | Нет 2-session benchmark; `hash()` в retriever |
| **07** | focus-active-compression | **74** | Alpha | 12% | −14 | Focus-tools не вызывались на бенчмарке |
| **09** | context-engineering | **72** | Alpha | 13% | −13 | Select ломает recall; SR/T A регрессия |
| **06** | scan-prompt-drift | **70** | Alpha | 11% | −15 | Latency 4.9s; чеклист 60%; SR регрессия |
| **05** | deterministic-routing | **68** | Alpha | 14% | −12 | Recall s05/s06 → `unknown` вместо `no_tools` |
| **10** | subtask-isolation | **65** | Alpha | 15% | −11 | Orchestrator multiserver SR=0% |
| **11** | neurosymbolic-guardrails | **62** | Alpha | 21% | −5 | AH=30% (цель adversarial не достигнута) |
| **02** | tool-validation-layer | **61** | Alpha | 14% | −12 | TSA 69%; `tool_args` не обнулён |
| **12** | multi-agent-validation | **58** | Alpha | 11% | −15 | Validator не на тегах s10; criteria не в pipeline |
| **08** | semantic-tool-selection | **56** | Alpha | 11% | −15 | TSA=56%; top-5 отсекает нужные tools |
| **13** | buddy-system | **55** | Alpha | 10% | −16 | Lat 5.6s; fail-open judge; `[BUDDY_GUIDE]` в history |

## Вердикты по tier

| Вердикт | Доработки | Смысл |
|---------|-----------|-------|
| **Beta (75–84)** | 01, 14, 15 | 01 — лучший SR (+6 п.п.); остальные ≈ baseline |
| **Alpha (50–74)** | 05–13, 02 | POC реализован, бенчмарк регрессирует или цель не достигнута |

**Production-ready (90+):** ни одна доработка не прошла честную оценку.

## Топ-3 по оконченности реализации

1. **01** four-section-prompt (81/100) — единственный с SR **выше** baseline
2. **15** memory-formation — SR на уровне baseline, CSR/AH лучше
3. **14** graph-rag — SR близко к baseline; нужен фикс сценариев

## Топ-3 по регрессии бенчмарка (избегать в prod как есть)

1. **13** buddy-system — SR 10%, lat ×7.7
2. **08** semantic-tool-selection — TSA 56% (−39 п.п.)
3. **12** multi-agent-validation — validator не срабатывает на decoy

## Общий вывод

- **Код и unit-тесты** у всех 13 variant — в основном на уровне (12–37 тестов, зелёные).
- **Бенчмарк** почти везде хуже baseline: лишний контекст, фильтрация tools, второй LLM-call, неправильные теги/сценарии.
- **Рекомендация:** в production первым мержить **01**; **15** и **14** после мелких фиксов; остальные — итерация по метрикам, не «готово как есть».

## Артефакты

- Кратко: `improvements/NN-*/JUDGE.md` (обновлены честно: **02**, **15**; остальные — в процессе)
- Подробно: `improvement_reports/judge/judge_NN.md`
