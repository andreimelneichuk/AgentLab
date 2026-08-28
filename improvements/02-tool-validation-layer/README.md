# 02 — Валидационный слой перед вызовом инструмента

**Tier:** 1  
**Источник:** RoboRhythms, AWS (нейросимволические guardrails)  
**Проблема:** Неверные параметры tool call (~1 из 30 даже с идеальным промптом)

## Суть проблемы

LLM предлагает вызов → инструмент исполняется с невалидными аргументами → тихий провал или побочные эффекты. Промпт не может гарантировать соблюдение схемы на каждом шаге.

## Архитектура

```
LLM → proposed_tool_call
         ↓
    tool_executor.execute_tool_command(strict=True)
         normalize (Basic) → GB validate → jsonschema/pydantic (02)
         ↓ pass          ↓ fail (error_kind=validation)
    Execute tool    → error message → LLM retry (max 3)
                           ↓ fail after 3
                      log_error + STOP
```

Стек валидации: **сначала нормализация и базовая проверка Basic** (`normalize_arguments` + `validate_arguments`), **затем строгая jsonschema/pydantic** (`validate_arguments_strict`) — один проход, без дублирования в `ToolValidator` и `tool_executor`.

## Компоненты

### 1. Схема-валидатор

- Проверка типов, обязательных полей, enum, диапазонов
- Источник схем: MCP tool metadata + `SCHEMA_AUGMENTATIONS` в `tool_executor.py`
- Реализация: `execute_tool_command(..., strict=True)` — единый путь normalize → GB → jsonschema
- `ToolValidator` — тонкая обёртка: реестр схем, trace отказов (`validation_rejections`)

### 2. Цикл retry

- Максимум **3 попытки** с чётким сообщением об ошибке (какое поле, какое ограничение)
- После 3-й — **STOP**, не продолжать задачу с частичными данными

### 3. Логирование отказов

- Каждый отклонённый вызов логируется в `validation_rejections` (`TurnResult`)

## Примеры из документа

| Ошибка | Действие guardrail |
|--------|-------------------|
| `book_hotel(guests=15)` при max=10 | Блок + сообщение LLM |
| Бронирование без проверки оплаты | Блок (completeness) |
| SUCCESS без вызова валидационного tool | Блок (tool bypass) |

## Что менять в Basic

| Место | Действие |
|-------|----------|
| `agent_core.py` / tool wrapper | Pre-execution hook с валидацией |
| `benchmark/mcp_tool_registry.py` | Единый реестр схем для mock и агента |
| Тесты | Unit-тесты валидатора на типовых ошибках args |

## Критерии успеха

- Метрика `tool_args` → 0 на сценариях с намеренно сложными параметрами
- Категория failure `tool_args` исчезает из отчёта
- Агент не вызывает forbidden tools даже при «обходных» аргументах

## Отличие от 11 (neurosymbolic guardrails)

| 02 | 11 |
|----|-----|
| Схема параметров (типы, форматы) | Бизнес-правила (лимиты, обязательные шаги) |
| Универсально для всех tools | Доменно-специфичные хуки |
| Tier 1 | Tier 3 |

Оба слоя **стекируются**: сначала схема, потом бизнес-правила.

## Зависимости

- `pydantic` или `jsonschema` (вероятно уже в стеке)
- Рекомендуется после: **01**

## Чеклист

- [x] Собрать JSON Schema для ~25 инструментов бенчмарка
- [x] Interceptor/hook до MCP-вызова
- [x] Retry loop с лимитом 3
- [x] STOP + понятное сообщение пользователю
- [x] Метрики отказов в trace
