# Judge Report — 01-four-section-prompt

**Дата:** 2026-07-01  
**Судья:** code-reviewer (оконченность доработки)  
**Оценка оконченности:** 81/100 — **Beta**

---

## Контекст

| Параметр | Значение |
|----------|----------|
| Спека | `improvements/01-four-section-prompt/README.md` |
| Код | `improvements/01-four-section-prompt/variant/` |
| Чеклист README | **5/6** (83%) |
| Unit-тесты | **9/9** ✅ (прогон судьи: `pytest tests/ -q`) |
| Полный бенчмарк | SR **32%** (baseline **26%**) |
| Smoke variant | 2/5 PASS (`report_basic_loop_20260701_005316.md`) |
| Smoke A/B | прогон есть (`report_original_variant_20260701_005424.md`), пункт чеклиста **не закрыт** |

---

## Таблица измерений (рубрика 5×20)

| # | Измерение | Балл | Обоснование |
|---|-----------|-----:|-------------|
| 1 | **Покрытие спеки** | **15/20** | 4 секции промпта, negation, canonical/decoy, STOP, `[POLICY_OK]` — реализованы. Описания MCP-tools для бенчмарка внесены вручную в `system_master.txt`, но **нет автосинхронизации** со схемами MCP. Чеклист: **5/6** — не закрыт smoke A/B (−5 по рубрике). |
| 2 | **Качество кода** | **17/20** | Стиль `original/`, модульность (`PROMPT_SECTION_HEADERS`, `_prompt_template_variables`, `messages_to_langchain`). Критичный фикс: system prompt теперь реально уходит в LLM. STOP — только в тексте промпта (соответствует Tier 1), не в коде loop. |
| 3 | **Тесты** | **14/20** | 9/9 зелёные: 6 тестов структуры/рендера + 3 регрессии MCP. Нет E2E с mock MCP/LLM, нет проверки runtime STOP/decoy на уровне агента. |
| 4 | **Интеграция в агент** | **19/20** | `run_turn()` использует `messages_to_langchain(..., system_prompt)`. `config.yml` согласован. `benchmark.compare --variant improvements/01-four-section-prompt/variant` работает. |
| 5 | **Production / эффективность** | **16/20** | SR +6 п.п., CSR +6, TA +5, TSA 96%. AH 55% vs 57% baseline (−2 п.п.). Latency 0.84s vs 0.73s (+15%, не критично). `IMPLEMENTATION.md` достаточен. Smoke A/B не задокументирован в чеклисте. |
| | **Итого** | **81/100** | Вердикт **Beta** (75–89): ядро готово, 1 пункт чеклиста открыт, бенчмарк выше baseline. |

---

## 1. Сверка со спекой

### Таблица «Что менять в Basic»

| Требование | Статус | Детали |
|------------|--------|--------|
| `system_master.txt` → 4 секции | ✅ | Заголовки `СЕКЦИЯ 1…4`, Jinja-ветки `is_default_bot` / `has_tools` |
| `agent_core.py` — промпт целиком | ✅ | `messages_to_langchain()` + вызов в `run_turn()` (строки 415–417, 466–467) |
| Описания MCP tools ↔ Секция 2 | ⚠️ | 25 бенчмарк-tools описаны статически в шаблоне; при добавлении tool в MCP промпт не обновится автоматически |
| Негативная область | ✅ | «Ты НЕ вызываешь…», «Ты НЕ выдумываешь…», «Ты НЕ продолжаешь после ошибки…» |
| «НЕ используй когда» per tool | ✅ | Все decoy-пары (`get_policy_fact`/`decoy_policy_fact`, и т.д.) |
| STOP при ошибке | ✅ (prompt) | Секция 3, п.4–6; **не enforced** в `_execute_tools` / loop |
| Фиксированный формат вывода | ✅ | Секция 4: markdown-ограничения, `[POLICY_OK]`, маркеры бенчмарка |

### Чеклист README (6 пунктов)

| # | Пункт | Статус |
|---|-------|--------|
| 1 | Аудит `system_master.txt` по 4 секциям | ✅ |
| 2 | Негативная область для каждой роли | ✅ |
| 3 | MCP-tool: схема + «не использовать когда» | ✅ |
| 4 | Правило STOP при ошибке tool call | ✅ |
| 5 | Фиксированный формат финального ответа | ✅ |
| 6 | Smoke-прогон и сравнение с `original` | ❌ |

**Пункт 6:** variant smoke 2/5; A/B-прогон `original` vs `variant` (limit 5) выполнен отдельно — метрики **идентичны** (SR 20%, TSA 60%), но результат **не отражён** в README/IMPLEMENTATION.md и чекбокс не закрыт.

---

## 2. Ключевые изменения в коде

### `agent_core.py` — критичный фикс интеграции

В **original** system prompt рендерился в `AgentResources`, но в `run_turn()` вызывался `dict_messages_to_langchain(working)` **без** SystemMessage — модель фактически не видела инструкции.

В **variant**:

```python
PROMPT_SECTION_HEADERS: Tuple[str, ...] = (...)

def messages_to_langchain(messages, system_prompt=""):
    lc = dict_messages_to_langchain(messages)
    if system_prompt:
        return [SystemMessage(content=system_prompt), *lc]
    return lc
```

`run_turn()` передаёт `self.resources.system_prompt` — это объясняет рост SR с 26% до 32% даже при длинном промпте.

### `prompts/system_master.txt`

- ~250 строк, 4 секции в строгом порядке
- Ветка `is_default_bot=False` — полный каталог бенчмарк-tools с canonical/decoy
- Секция 3: явные if/then, цепочки (`employee_lookup` → `leave_balance`), STOP, лимит 2 retry
- Секция 4: `[POLICY_OK]`, запрет `#` заголовков и таблиц

---

## 3. Тестирование (прогон судьи)

```bash
cd improvements/01-four-section-prompt/variant
source ../../../original/.venv/bin/activate
PYTHONPATH=. pytest tests/ -q
# 9 passed in 0.95s
```

| Файл | Тестов | Назначение |
|------|-------:|------------|
| `test_four_section_prompt.py` | 6 | 4 секции, negation, decoy, strict/no-tools, additional_instructions, SystemMessage |
| `test_mcp_normalize.py` | 3 | Регрессия MCP-конфига (не затронута) |

**Пробелы тестов:** нет smoke/E2E, нет assert на длину промпта / token budget, нет теста «STOP блокирует retry после Error».

---

## 4. Бенчмарк и метрики

### Полный прогон (176 сценариев, `report_01.md`)

| Метрика | original | variant 01 | Δ |
|---------|---------:|-----------:|--:|
| SR | 26% | **32%** | +6 п.п. |
| CSR | 43% | **49%** | +6 п.п. |
| TA | 49% | **54%** | +5 п.п. |
| TSA | 95% | **96%** | +1 п.п. |
| AH | 57% | 55% | −2 п.п. |
| lat | 0.73s | 0.84s | +15% |

Variant 01 — **единственная** доработка с SR выше baseline по `SUMMARY.md`.

### Smoke (limit 5)

| Сценарий | variant |
|----------|---------|
| s01_001 benchmark_probe | PASS |
| s01_005 empty_search | FAIL |
| s01_007 flaky_tool retry | PASS |
| s03_001 weather | FAIL |
| s03_006 translate | FAIL |

A/B smoke (`report_original_variant_20260701_005424.md`): SR/TSA **одинаковы** — на малой выборке эффект промпта не проявился; variant потребляет **~2.2×** токенов (46633 vs 21205).

---

## 5. Gaps (пробелы)

| Приоритет | Gap | Влияние |
|-----------|-----|---------|
| 🔴 | Чеклист п.6 не закрыт: нет задокументированного smoke A/B | Блокирует статус Production-ready по рубрике |
| 🟡 | STOP при ошибке — только prompt, loop продолжает `_execute_tools` и следующий раунд LLM | ~⅓ CRM-ошибок в спеке — «молчаливый recovery»; нужна пара с **02** или код в loop |
| 🟡 | Описания MCP tools статические; нет sync из `tool.description` / JSON Schema | Риск рассинхрона при изменении mock-сервера |
| 🟡 | AH −2 п.п. vs baseline | Антигаллюцинация не улучшилась, несмотря на negation |
| 🟢 | Длинный system prompt → рост prompt_tokens на smoke | Trade-off: качество vs стоимость |
| 🟢 | Нет CI-джоба smoke для variant | Регрессии промпта не ловятся автоматически |

---

## 6. Документация

| Артефакт | Статус |
|----------|--------|
| `IMPLEMENTATION.md` | ✅ Команды, описание изменений, smoke-результаты |
| `test_report_four_section_prompt.md` | ✅ 9/9, smoke 2/5 |
| `prompts/.AGENTS.md` | ✅ Обновлён под 4 секции |
| README чеклист | ⚠️ 5/6, п.6 открыт |

---

## 7. Топ-3 рекомендации

1. **Закрыть чеклист п.6:** выполнить и зафиксировать A/B smoke (`compare.py --variant original --variant improvements/01-four-section-prompt/variant --tags smoke --limit 5`), обновить README + IMPLEMENTATION.md, отметить `[x]`. Добавить таблицу delta SR/TSA/r_nta/r_dt.

2. **Добавить smoke/E2E в CI:** минимум 1 сценарий `r_dt` (decoy) + 1 `instruction` (`[POLICY_OK]`) через mock MCP — чтобы ловить регрессии промпта без полного бенчмарка.

3. **Усилить STOP на уровне кода (или связка с 02):** при `content.startswith("Error")` в ToolMessage — прерывать tool loop и возвращать пользователю ошибку без следующего LLM-раунда; сейчас правило только в тексте промпта и LLM может его игнорировать.

---

## Итоговое решение

**⚠️ Beta — готово к экспериментальной интеграции, не к production merge без закрытия чеклиста.**

Доработка **существенно улучшает** baseline (+6 п.п. SR) и исправляет критичный баг (system prompt не передавался в LLM). Структура 4-секционного промпта реализована качественно. Статус **Production-ready (90+)** не выставляется из‑за незакрытого smoke A/B в чеклисте и отсутствия runtime-enforcement STOP.
