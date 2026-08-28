# Реализация: 02 — tool validation layer

## Статус

Реализовано в `variant/`.

## Что сделано

### 1. `tool_executor.py` (shared baseline + strict mode)

- `SCHEMA_AUGMENTATIONS`, `merge_schema_augmentation`, `build_schema_registry`
- `validate_arguments_strict()` — jsonschema/pydantic поверх Basic baseline
- `execute_tool_command(..., strict=False)` — по умолчанию только GB; вариант 02 передаёт `strict=True`

### 2. `tool_validator.py` (тонкая обёртка)

- `ToolValidator` — реестр схем, trace отказов; `validate()` делегирует в `prepare_and_validate_arguments`
- `ValidationRejection` + `record_rejection()` — trace отклонённых вызовов
- `format_validation_errors()` — сообщение для feedback LLM
- `validation_stop_message()` — сообщение пользователю при STOP

### 3. `agent_core.py`

- `AgentResources.tool_validator` — создаётся при загрузке MCP tools
- `BasicLoopSession._execute_single_tool()` — один вызов `execute_tool_command(strict=True)`
- Retry loop в `run_turn()`: при невалидных args → error в tool message → LLM retry; max **3** попытки (`config.tools.validation.max_retries`), затем **STOP**.
- `TurnResult.validation_rejections` и `TurnResult.stopped_reason` для метрик/trace.

### 3. Конфиг

`config.yml`:

```yaml
tools:
  validation:
    max_retries: 3
```

### 4. Тесты

`variant/tests/test_tool_validation.py` — unit-тесты валидатора, `_execute_tools`, retry STOP.

## Архитектура

```
LLM → tool_calls
        ↓
  execute_tool_command(strict=True)
    normalize (GB) → validate_arguments (GB) → validate_arguments_strict (jsonschema)
        ↓ pass              ↓ fail (error_kind=validation)
  tool.ainvoke()     tool error message → LLM (attempt++)
                           ↓ attempt > max_retries
                     STOP + user message
```

`ToolValidator` больше не дублирует проверку — только `schema_registry`, `record_rejection()` и trace.

## Схемы инструментов

Источник — `tool.args_schema` при загрузке через `langchain_mcp_adapters` (MCP `inputSchema`).  
Реестр доступен как `ToolValidator.schema_registry` (~25+ инструментов бенчмарка при подключении mock MCP).

## Зависимости

- `jsonschema` (явно в `requirements.txt`; также транзитивно через pydantic/langchain).

## Запуск тестов

```bash
cd improvements/02-tool-validation-layer/variant
source .venv/bin/activate
PYTHONPATH=. pytest tests/ -q
```

## Открытые вопросы

Нет — см. `QUESTIONS.md` при появлении блокеров.
