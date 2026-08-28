# Отчёт о тестировании — SCAN (06-scan-prompt-drift)

## Новые тесты

### Модульные тесты (`tests/test_scan.py`)
- ✅ `test_parse_scan_markers_returns_sorted_unique` — PASSED
- ✅ `test_parse_scan_markers_empty_for_no_markers` — PASSED
- ✅ `test_is_trivial_turn` (5 параметров) — PASSED
- ✅ `test_resolve_scan_level_by_tags` (4 параметра) — PASSED
- ✅ `test_resolve_scan_level_skip_for_trivial` — PASSED
- ✅ `test_resolve_scan_level_disabled_in_config` — PASSED
- ✅ `test_resolve_scan_level_explicit_override` — PASSED
- ✅ `test_select_markers_for_level_full` — PASSED
- ✅ `test_select_markers_for_level_anchor` — PASSED
- ✅ `test_select_markers_for_level_skip` — PASSED
- ✅ `test_build_scan_trigger_contains_markers_and_user_task` — PASSED
- ✅ `test_parse_scan_output_extracts_lines` — PASSED
- ✅ `test_format_scan_block_preserves_order` — PASSED
- ✅ `test_build_check_trigger_includes_scan_and_answer` — PASSED
- ✅ `test_parse_check_output` — PASSED
- ✅ `test_max_tokens_hint_levels` — PASSED
- ✅ `test_system_master_contains_six_markers` — PASSED
- ✅ `test_compose_visible_answer_order` — PASSED

## Регрессионные тесты

### Запущено тестов: 28
### Прошло успешно: 28
### Упало: 0

Включая `tests/test_mcp_normalize.py` (3 теста).

## Детали выполнения

### Новый функционал
Парсинг маркеров, уровни SCAN, триггеры и интеграция в `BasicLoopSession` работают согласно спецификации.

### Регрессия
Существующие тесты MCP-нормализации не затронуты.

## Итог

✅ Все тесты прошли успешно  
✅ Регрессия не обнаружена  
✅ Задача готова к ревью
