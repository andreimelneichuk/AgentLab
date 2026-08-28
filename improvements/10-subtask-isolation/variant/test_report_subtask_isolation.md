# Отчёт о тестировании: subtask isolation

## Новые тесты

### Unit-тесты (`tests/test_orchestrator.py`)
- ✅ `test_handoff_schema_roundtrip` — PASSED
- ✅ `test_domain_tool_isolation_hr_vs_crm` — PASSED
- ✅ `test_subagent_factory_excludes_foreign_decoys` — PASSED
- ✅ `test_infer_domain_from_explicit_tool` — PASSED
- ✅ `test_infer_domain_from_keywords` — PASSED
- ✅ `test_is_recall_turn_detection` — PASSED
- ✅ `test_decompose_execute_plan` — PASSED
- ✅ `test_decompose_recall_plan` — PASSED
- ✅ `test_all_benchmark_tools_mapped_to_domain` — PASSED
- ✅ `test_orchestrator_delegates_without_direct_tools` — PASSED
- ✅ `test_orchestrator_recall_uses_recall_session` — PASSED
- ✅ `test_basic_orchestrator_session_reset` — PASSED

## Регрессионные тесты

### `tests/test_mcp_normalize.py`
- ✅ `test_normalize_http_adds_mcp_path` — PASSED
- ✅ `test_build_mcp_empty` — PASSED
- ✅ `test_build_mcp_from_config` — PASSED

### Запущено тестов: 15
### Прошло успешно: 15
### Упало: 0

## Детали выполнения

### Новый функционал
Orchestrator декомпозирует запросы, делегирует доменным субагентам с изолированными tools, формирует structured JSON handoffs и ведёт trace. Recall-ходы идут через сессию без tools.

### Регрессия
Существующие тесты `agent_core` (MCP normalize) не затронуты.

## Итог

✅ Все тесты прошли успешно
✅ Регрессия не обнаружена
✅ Задача готова к ревью
