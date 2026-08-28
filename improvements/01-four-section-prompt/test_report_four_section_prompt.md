# Отчёт о тестировании: four-section-prompt

## Новые тесты

### End-to-end / структурные
- ✅ `test_template_contains_four_sections_in_order` — PASSED
- ✅ `test_render_default_bot_with_tools` — PASSED
- ✅ `test_render_benchmark_with_tools` — PASSED (decoy только в «НЕ использовать», без отдельных секций)
- ✅ `test_registry_canonical_tools_match_benchmark_prompt` — PASSED
- ✅ `test_render_without_tools_strict_mode` — PASSED
- ✅ `test_render_includes_additional_instructions` — PASSED
- ✅ `test_messages_to_langchain_prepends_system_prompt` — PASSED

### Модульные
- (включены в файле выше)

## Регрессионные тесты

### Запущено тестов: 10
### Прошло успешно: 10
### Упало: 0

Файлы: `tests/test_four_section_prompt.py`, `tests/test_mcp_normalize.py`

## Smoke A/B: original vs variant (2026-07-02)

Команда:
```bash
PYTHONPATH=. python -m benchmark.compare \
  --variant original \
  --variant improvements/01-four-section-prompt/variant \
  --tags smoke --limit 5
```

| Сценарий | original | variant |
|----------|----------|---------|
| s01_001 benchmark_probe | PASS | PASS |
| s01_005 empty_search | FAIL | FAIL |
| s01_007 flaky_tool retry | PASS | FAIL |
| s03_001 weather | FAIL | FAIL |
| s03_006 translate | FAIL | FAIL |

### Scorecard

| Метрика | original | variant |
|---------|----------|---------|
| Solve rate (SR) | 40% (2/5) | 20% (1/5) |
| Turn accuracy (TA) | 40% | 20% |
| TSA (tool selection) | 80% (4/5) | 60% (3/5) |
| TAA (tool args) | 100% (2/2) | 100% (2/2) |
| Latency (avg) | 1.93s | 1.10s |

Отчёт: `benchmark/results/report_original_variant_20260702_120806.md`

### Наблюдения

- **B/C:** Секция 2 бенчмарка генерируется из `mcp_tool_registry`; decoy-инструменты не попадают в позитивный список, но упоминаются в строках «НЕ использовать» у canonical tools.
- **s01_007:** variant строго следует правилу STOP при ошибке и не делает retry `flaky_tool` (original — PASS с 2 вызовами).
- **s01_005, s03_001, s03_006:** оба варианта FAIL (галлюцинации / нецитирование маркеров WEATHER/TRANSLATED) — зависит от LLM, не от структуры промпта.

## Детали выполнения

### Новый функционал
Промпт рендерится в 4 секции; для бенчмарка `tools_section` строится динамически через `prompt_tools.py`. System prompt передаётся в LLM через `SystemMessage`.

### Регрессия
Существующие тесты `test_mcp_normalize` не затронуты.

## Итог

✅ Все unit-тесты прошли успешно (10/10)
✅ Регрессия не обнаружена
✅ Smoke A/B выполнен и задокументирован
⚠️ Variant на smoke: 1/5 vs original 2/5 (s01_007 регрессия из-за STOP без retry)
✅ Задача готова к ревью
