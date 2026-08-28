# Отчёт о тестировании: deterministic routing

## Новые тесты

### Unit-тесты routing (`tests/test_routing.py`)
- ✅ `test_route_s08_phrases` (28 параметризованных кейсов) — PASSED
- ✅ `test_route_is_deterministic` — PASSED
- ✅ `test_decoys_never_in_allowed_sets` — PASSED
- ✅ `test_domain_tool_sets_match_registry` — PASSED
- ✅ `test_filter_tools_by_route` — PASSED
- ✅ `test_is_tool_allowed_respects_domain` — PASSED
- ✅ `test_unknown_fallback_narrow_subset` — PASSED
- ✅ `test_no_tools_has_prompt_fragment` — PASSED

### Регрессионные тесты (`tests/test_mcp_normalize.py`)
- ✅ `test_normalize_http_adds_mcp_path` — PASSED
- ✅ `test_build_mcp_empty` — PASSED
- ✅ `test_build_mcp_from_config` — PASSED

## Регрессионные тесты

### Запущено тестов: 37
### Прошло успешно: 37
### Упало: 0

## Детали выполнения

### Новый функционал
Rule-based router корректно маршрутизирует ключевые фразы из `s08_multiserver.yaml`. Decoy-инструменты исключены из allowed sets. Per-turn фильтрация tools интегрирована в `run_turn`.

### Регрессия
Существующие тесты MCP-нормализации не затронуты.

## Итог

✅ Все тесты прошли успешно  
✅ Регрессия не обнаружена  
✅ Задача готова к ревью
