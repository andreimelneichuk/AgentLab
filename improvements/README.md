# Доработки агента: против галлюцинаций tool call

Каталог экспериментальных доработок на основе аналитического документа  
«Галлюцинации LLM при вызове инструментов» (2025–2026).

Каждая папка — отдельный вариант для реализации как копия `original/` и прогона через `benchmark.compare`.

## Индекс

| # | Директория | Tier | Суть |
|---|------------|------|------|
| 01 | [four-section-prompt](./01-four-section-prompt/) | 1 | 4-секционный системный промпт |
| 02 | [tool-validation-layer](./02-tool-validation-layer/) | 1 | Валидация параметров до вызова инструмента |
| 05 | [deterministic-routing](./05-deterministic-routing/) | 1–2 | Детерминированный routing вместо LLM-switch |
| 06 | [scan-prompt-drift](./06-scan-prompt-drift/) | 2 | Метод SCAN против дрейфа промпта |
| 07 | [focus-active-compression](./07-focus-active-compression/) | 2 | Активная компрессия контекста (Focus) |
| 08 | [semantic-tool-selection](./08-semantic-tool-selection/) | 2 | Семантический выбор top-N инструментов |
| 09 | [context-engineering](./09-context-engineering/) | 2 | Write / Select / Compress / Isolate |
| 10 | [subtask-isolation](./10-subtask-isolation/) | 2 | Изоляция подзадач и субагенты |
| 11 | [neurosymbolic-guardrails](./11-neurosymbolic-guardrails/) | 3 | Хуки и guardrails на уровне фреймворка |
| 12 | [multi-agent-validation](./12-multi-agent-validation/) | 3 | Воркер + валидатор (перекрёстная проверка) |
| 13 | [buddy-system](./13-buddy-system/) | 3 | LLM-as-Judge: напарник корректирует дрейф |
| 14 | [graph-rag](./14-graph-rag/) | 3 | Graph-RAG вместо векторного RAG |
| 15 | [memory-formation](./15-memory-formation/) | 3 | Формирование памяти (факты вместо суммаризации) |
| 16 | [self-refinement-recall](./16-self-refinement-recall/) | 2 | Self-refinement: детекция пропущенных (не выдуманных) фактов |
| 17 | [hierarchical-context-tree](./17-hierarchical-context-tree/) | 2 | Иерархическое (2-level) MapReduce-сжатие контекста |

## Как использовать

В каждой папке уже есть `variant/` — копия `original/` для изолированной реализации.

```bash
# 1. Прочитать README доработки и IMPLEMENTATION.md (после субагента)
# 2. Прогнать бенчмарк
PYTHONPATH=. python -m benchmark.compare --variant improvements/01-four-section-prompt/variant --tags smoke

# 3. Блокирующие вопросы субагентов — improvements/XX/QUESTIONS.md
```

### Статус реализации (2026-06-30)

13 variant-ов реализованы субагентами-разработчиками.

### Оценка оконченности (судьи)

Рубрика: [`judge/RUBRIC.md`](./judge/RUBRIC.md)  
Сводка: [`improvement_reports/judge/JUDGE_SUMMARY.md`](../improvement_reports/judge/JUDGE_SUMMARY.md)  
Краткий отчёт в каждой папке: `NN-*/JUDGE.md`

## Связь с метриками бенчмарка

| Метрика / тег | Что измеряет | Релевантные доработки |
|---------------|--------------|----------------------|
| `r_nta` | Галлюцинация при отсутствии инструмента | 01, 02, 11 + `benchmark/scenarios/negative.yaml` |
| `r_dt` | Неверный выбор при наличии приманки | 01, 05, 08, 11 |
| `tool_args` | Неверные аргументы | 01, 02, 11 |
| `tool_missing` / `tool_forbidden` | Недовызов / запрещённый инструмент | 01, 05, 08 |
| `s10_adversarial` | Длинные / враждебные сессии | 06, 07, 09, 10, 13 |

## Что не выносится в отдельные доработки

- **NTA/DT-тесты** (SimpleToolHalluBench) — сценарии и scoring в `benchmark/scenarios/negative.yaml`, не отдельный variant агента.
- **Более умная модель** — документ показывает обратный эффект (Reasoning Trap, arXiv 2510.22977).
- **Повтор промпта каждые N сообщений** — антипаттерн; заменяется SCAN (06).
- **Только суммаризация истории** — антипаттерн; заменяется memory-formation (15) или Focus (07).

## Порядок внедрения (рекомендуемый)

1. **01 → 02** — базовый production-контур без внешних зависимостей.
2. **05, 08** — если много инструментов (~25 в бенчмарке).
3. **06, 07, 09** — если падает качество на длинных сессиях (`s09_instruction`, `s10_adversarial`).
4. **11–15** — для compliance и multi-session сценариев.
