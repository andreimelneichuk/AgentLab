# Judge Report — 02-tool-validation-layer

**Дата ревью:** 2026-07-01  
**Вариант:** `improvements/02-tool-validation-layer/variant/`  
**Спека:** `improvements/02-tool-validation-layer/README.md`  
**Рубрика:** `improvements/judge/RUBRIC.md`

---

## Итог

| Показатель | Значение |
|------------|----------|
| **Оценка оконченности** | **61/100** |
| **Вердикт** | **Alpha** |
| **Бенчмарк SR** | **13.7%** (baseline **26%**, Δ **−12.3 п.п.**) |
| **pytest** | **19/19** ✅ |

Ядро валидационного слоя реализовано и подключено к `run_turn`, unit-тесты зелёные. Однако **целевые метрики доработки не достигнуты**: SR упал почти вдвое относительно baseline, TSA просел до 69%, категория `tool_args` в отчёте бенчмарка не исчезла. Это POC с рабочей интеграцией, но не production-ready.

---

## Таблица измерений (рубрика 0–20)

| Измерение | Балл | Обоснование |
|-----------|-----:|-------------|
| **1. Покрытие спеки** | **12/20** | Архитектура из README реализована: `ToolValidator`, pre-hook, retry×3, STOP, trace. Чеклист README отмечен 5/5. Но **критерии успеха не выполнены**: `tool_args` остаётся (13–16 кейсов в отчёте), `benchmark/mcp_tool_registry.py` как единый реестр схем **не задействован** — схемы берутся только из runtime MCP `args_schema`. `SCHEMA_AUGMENTATIONS` пуст. |
| **2. Качество кода** | **16/20** | Модуль `tool_validator.py` чистый, dataclass-результаты, логирование, merge augmentations. Интеграция в `agent_core._execute_tools` / `run_turn` читаемая. Минусы: jsonschema возвращает **одну** ошибку; augmentations не заполнены для бенчмарка; `validation_failures` считает раунд с любым отказом в батче tool calls. |
| **3. Тесты** | **16/20** | `pytest tests/` — **19/19** (16 новых + 3 регрессии MCP). Покрыты: схема, типы, required, pydantic, augmentations, блокировка invoke, retry STOP. Нет тестов на реальные схемы ~25 MCP tools, нет E2E через mock MCP server, нет проверки экспорта метрик в бенчмарк. |
| **4. Интеграция в агент** | **17/20** | Hook реально в tool loop (`_execute_tools` → `validate` → `ainvoke`). `config.tools.validation.max_retries: 3`. `TurnResult.validation_rejections`, `stopped_reason`. `compare.py` прогоняет variant. Минус: бенчмарк **не читает** `validation_rejections` / `stopped_reason` — trace для отказов не попадает в `report_02.json`. |
| **5. Эффективность / production** | **2/20** | SR **13.7%** vs baseline **26%** (−12.3 п.п.) → штраф **−10** по рубрике. TSA **69%** vs **95%**, AH **45%** vs **57%**, CSR **32%** vs **43%**. TAA **89%** — единственный плюс. Latency **0.96s** vs **0.73s** (×1.3, не критично). `tool_args` failures: **13** в aggregate, **16** turns с `tool_args_ok: false`. STOP-сообщений в бенчмарке: **0** — retry почти не срабатывает на практике. |
| **Сумма** | **61/100** | |

---

## 1. Соответствие постановке (README)

### Реализовано ✅

| Требование | Статус | Где |
|------------|--------|-----|
| Schema Validator (JSON Schema / Pydantic) | ✅ | `tool_validator.py`: `ToolValidator.validate`, `extract_json_schema` |
| Pre-execution hook до MCP | ✅ | `agent_core.py`: `_execute_tools` строки 370–381 |
| Retry loop max 3 | ✅ | `run_turn` строки 493–510, `validation_limits` |
| STOP + сообщение пользователю | ✅ | `validation_stop_message`, `stopped_reason="tool_validation_exhausted"` |
| Логирование отказов | ✅ | `record_rejection`, `ValidationRejection`, `TurnResult.validation_rejections` |
| Конфиг `max_retries` | ✅ | `config.yml` → `tools.validation.max_retries: 3` |

### Не реализовано / частично ⚠️

| Требование | Статус | Проблема |
|------------|--------|----------|
| Единый реестр схем в `benchmark/mcp_tool_registry.py` | ❌ | Спека явно указывает этот файл; variant использует только runtime `tool.args_schema` |
| `tool_args → 0` на сложных сценариях | ❌ | 13–16 failures; в основном `calc_expression` — пробелы в expression (`3 * 850` vs `3*850`), схема это допускает |
| Категория failure `tool_args` исчезает | ❌ | 2.6% failure mix, 13 counts |
| Forbidden tools при обходных аргументах | ❌ | 2 `tool_forbidden` в отчёте |
| Метрики отказов в trace бенчмарка | ⚠️ | Поля есть в `TurnResult`, но `compare.py` их не сериализует (`trace_path: null`) |

### Чеклист README (5/5 отмечено)

Все пункты **в коде присутствуют**, но два последних («метрики в trace», «~25 схем») формально закрыты слабо: схемы не собраны явно, метрики не доходят до отчёта бенчмарка.

---

## 2. Качество реализации

### Архитектура (соответствует диаграмме спеки)

```
LLM → tool_calls
        ↓
  ToolValidator.validate (jsonschema / pydantic)   ← реализовано
        ↓ pass              ↓ fail
  tool.ainvoke()     error ToolMessage → LLM retry
                           ↓ failures >= 3
                     STOP + user message
```

### Сильные стороны

- Разделение ответственности: `tool_validator.py` изолирован от loop-логики.
- Поддержка dict-схем и Pydantic `BaseModel` из MCP metadata.
- Механизм `SCHEMA_AUGMENTATIONS` + deep merge — задел для доменных ограничений (пример `book_hotel guests≤10` в тестах).
- При невалидных args `tool.ainvoke` **не вызывается** (проверено тестом `test_execute_tools_blocks_invalid_args`).

### Замечания по коду

1. **`jsonschema.validate` — одна ошибка за раз** (`tool_validator.py:176–178`). LLM получает неполный feedback при нескольких нарушениях.
2. **`SCHEMA_AUGMENTATIONS = {}`** — пуст в продакшен-коде; доп. ограничения из примеров спеки (`guests max=10`) не применяются к бенчмарку.
3. **Счётчик `validation_failures`** инкрементируется на весь раунд tool calls, а не per-tool — при мульти-tool ответе LLM один невалидный вызов «съедает» попытку для всех.
4. **Unknown tool** обрабатывается как validation failure с текстом `Error: unknown tool` — семантически ближе к routing, не к schema validation.

---

## 3. Тестирование

### pytest (запущено судьёй)

```
19 passed in 1.02s
```

| Группа | Тестов | Статус |
|--------|-------:|--------|
| `test_tool_validation.py` | 16 | ✅ |
| `test_mcp_normalize.py` | 3 | ✅ |

### Покрытие по сценариям

| Сценарий | Покрыт |
|----------|--------|
| Валидные args → pass | ✅ |
| Missing required / wrong type | ✅ |
| Pydantic pattern (target_lang) | ✅ |
| Schema augmentation (guests≤10) | ✅ |
| Блок invoke при fail | ✅ |
| Retry STOP после 3 раундов | ✅ (mock LLM) |
| Реальные MCP schemas ~25 tools | ❌ |
| Бенчмарк calc_expression whitespace | ❌ |

---

## 4. Интеграция в агент

### `agent_core.py` — ключевые точки

- `AgentResources.create` → `ToolValidator(tools, max_retries=...)`
- `_execute_tools`: validate → record_rejection → format_validation_errors → skip ainvoke
- `run_turn`: `validation_failures` counter, reset на успех, STOP при `>= max_retries`
- `TurnResult`: `validation_rejections`, `stopped_reason`

### Бенчмарк (`report_02.json`)

| Метрика | Variant 02 | Baseline (original) |
|---------|------------|---------------------|
| SR (solve_rate) | **13.7%** | **26%** |
| CSR | 31.7% | 43% |
| TA (turn_accuracy) | 31.2% | 49% |
| TSA | **69.3%** | **95%** |
| TAA | 89.1% | — |
| AH | 45.5% | 57% |
| r_nta | 54.5% | — |
| r_dt | 1.5% | — |
| mean_latency | 0.96s | 0.73s |
| tool_args failures | 13 | — |
| tool_missing | 12 | — |
| under_call | 119 (24%) | — |

### Анализ регрессии SR

1. **TSA −26 п.п.** — агент реже вызывает tools; validation layer не блокирует вызовы напрямую, но error messages в контексте и дополнительные раунды могут демотивировать модель.
2. **under_call 24%** failure mix — основной вклад в падение SR; валидация не решает проблему «не вызвал нужный tool».
3. **tool_args failures** — в основном **семантическое** несовпадение args (пробелы в `calc_expression`), а не нарушение JSON Schema. Слой 02 **не адресует** эту категорию.
4. **0 STOP-сообщений** в 506 graded turns — механизм retry/STOP почти не активируется; слой добавляет кодовую сложность без измеримого эффекта на бенчмарке.

---

## 5. Документация

| Артефакт | Статус |
|----------|--------|
| `IMPLEMENTATION.md` | ✅ Достаточен для онбординга |
| `variant/.AGENTS.md` | ✅ Описаны новые классы/поля |
| `test_report_tool_validation.md` | ✅ 19/19 |
| `QUESTIONS.md` / `DECISIONS.md` | N/A (нет открытых вопросов) |

---

## Gaps (приоритет)

### Критичные для цели доработки

1. **SR −12 п.п.** — доработка ухудшает общий solve rate; нельзя считать завершённой с точки зрения production.
2. **Критерии успеха README не выполнены** — `tool_args` не обнулён, forbidden tools не заблокированы на уровне схемы.
3. **Нет связи с `mcp_tool_registry.py`** — расхождение со спекой «единый реестр схем».

### Важные

4. Метрики `validation_rejections` не экспортируются в бенчмарк/trace.
5. `SCHEMA_AUGMENTATIONS` не заполнен для реальных ограничений mock tools.
6. Нет нормализации args (например, strip spaces в `expression`) — TAA высокий, но judge бенчмарка сравнивает строки literally.

### Некритичные

7. Одна jsonschema-ошибка за валидацию.
8. Нет тестов на batch tool calls с частичным fail.

---

## Рекомендации (топ-5)

1. **Не блокировать merge по SR**, но и **не завышать готовность** — нужна итерация: ослабить friction (не считать unknown tool как validation round) или нормализовать args перед сравнением.
2. **Заполнить `SCHEMA_AUGMENTATIONS`** из `mcp_tool_registry` / mock tool signatures; синхронизировать с бенчмарком.
3. **Экспортировать `validation_rejections` в `compare.py`** — иначе пункт «метрики в trace» формальный.
4. **Добавить нормализацию** для string-полей, где бенчмарк сравнивает каноническую форму (`calc_expression`: удаление пробелов).
5. **Измерить A/B**: validation on/off на подмножестве `tool_args` сценариев — подтвердить, даёт ли слой пользу или только overhead.

---

## Итоговое решение

**⚠️ ALPHA — требуется доработка перед production**

Реализация **структурно завершена** (hook, retry, STOP, тесты), но **функциональная цель не достигнута**: SR 14% vs baseline 26%, TSA 69% vs 95%, tool_args failures сохраняются. Код можно использовать как основу для итерации, но не как готовый production-слой.
