# Реализация: 4-секционный системный промпт

## Что сделано

1. **`variant/prompts/system_master.txt`** — перестроен в 4 секции:
   - Секция 1: роль + негативная область (что агент НЕ делает)
   - Секция 2: инструменты с входом/выходом и «НЕ использовать когда» (платформа или бенчмарк MCP)
   - Секция 3: явные if/then, STOP при ошибке tool call, лимит повторов
   - Секция 4: формат ответа, `[POLICY_OK]`, ограничения Markdown

2. **`variant/agent_core.py`**:
   - Константа `PROMPT_SECTION_HEADERS` для проверки структуры
   - `_prompt_template_variables()` — полный набор Jinja-переменных; для бенчмарка инжектирует `tools_section`
   - `messages_to_langchain()` — system prompt передаётся в LLM целиком как `SystemMessage`
   - `BasicLoopSession.run_turn()` использует `messages_to_langchain`

3. **`variant/prompt_tools.py`** — динамическая Секция 2 для бенчмарка:
   - Парсит `benchmark/mcp_tool_registry.py` (имена, docstring, параметры)
   - Рендерит только canonical tools; decoy упоминаются в «НЕ использовать»
   - `render_benchmark_tools_section()` вызывается при `is_default_bot=False`

4. **`variant/tests/test_four_section_prompt.py`** — unit-тесты структуры, sync registry↔prompt

## Как проверить

### Unit-тесты

```bash
cd improvements/01-four-section-prompt/variant
source .venv/bin/activate
PYTHONPATH=. pytest tests/test_four_section_prompt.py -q
```

### Регрессия agent_core

```bash
PYTHONPATH=. pytest tests/ -q
```

### Smoke-бенчмарк (нужен mock MCP и LLM)

```bash
# терминал 1
cd /path/to/AgentLab
python -m benchmark.mcp_mock_server

# терминал 2
PYTHONPATH=. python -m benchmark.compare \
  --variant improvements/01-four-section-prompt/variant \
  --tags smoke --limit 5
```

Сравнение с эталоном:

```bash
PYTHONPATH=. python -m benchmark.compare \
  --variant original \
  --variant improvements/01-four-section-prompt/variant \
  --tags smoke --limit 5
```

## Smoke A/B (2026-07-02)

Сравнение `original` vs `variant` (`--tags smoke --limit 5`):

| | original | variant |
|---|----------|---------|
| Solve rate | 40% (2/5) | 20% (1/5) |
| TSA | 80% | 60% |
| PASS | s01_001, s01_007 | s01_001 |

Отчёт: `benchmark/results/report_original_variant_20260702_120806.md`

Variant на s01_007 (flaky_tool) строго применяет STOP после первой ошибки и не делает retry — регрессия относительно original.


- Меньше `tool_args`, `r_dt`, `r_nta` на сценариях с decoy-инструментами
- Агент следует STOP при ошибках и не подставляет данные
- Сценарии `s09_instruction` / `[POLICY_OK]` опираются на Секцию 3–4
