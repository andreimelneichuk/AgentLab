# Отчёт о тестировании: semantic tool selection

## Новые тесты

### End-to-end / integration (select_tool_names)
- ✅ `test_python_doc_query_prefers_real_tool` — PASSED
- ✅ `test_decoys_never_selected_via_select_tool_names` — PASSED
- ✅ `test_mandatory_tools_always_included` — PASSED
- ✅ `test_deny_list_excludes_tools` — PASSED
- ✅ `test_disabled_selection_returns_all` — PASSED

### Модульные тесты
- ✅ `test_corpus_includes_decoys_and_real_tools` — PASSED
- ✅ `test_is_decoy_tool` — PASSED
- ✅ `test_policy_query_prefers_real_tool` — PASSED
- ✅ `test_inventory_query_prefers_real_tool` — PASSED
- ✅ `test_hr_employee_query` — PASSED
- ✅ `test_crm_customer_query` — PASSED
- ✅ `test_effective_deny_includes_all_decoys` — PASSED
- ✅ `test_filter_openai_tools_preserves_order` — PASSED
- ✅ `test_top_k_limits_results` — PASSED
- ✅ `test_search_returns_sorted_by_score` — PASSED

## Регрессионные тесты

### Запущено тестов: 18
### Прошло успешно: 18
### Упало: 0

## Детали выполнения

### Новый функционал
TF-IDF retriever индексирует corpus из `benchmark/mcp_tool_registry.py`. Top-k фильтрация в `run_turn` отдаёт LLM 5 инструментов; decoy_* исключаются через deny-set; mandatory tools всегда включаются.

### Регрессия
Существующие тесты `test_mcp_normalize.py` не затронуты.

## Итог

✅ Все тесты прошли успешно
✅ Регрессия не обнаружена
✅ Задача готова к ревью
