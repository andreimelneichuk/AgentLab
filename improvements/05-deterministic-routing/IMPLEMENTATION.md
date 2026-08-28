# Реализация: 05 — Детерминированный routing (v2)

## v1 провалился — почему

Первая версия делала **hard per-turn gate**: один домен на весь ход,
first-match-wins по порядку правил, узкий unknown-fallback (2 tool),
роутинг пересчитывался с нуля на каждой реплике без памяти прошлых ходов.

Результат на бенчмарке: SR 26% → **14%**, TSA 95% → **64%** — router стал
хуже самой модели в выборе инструмента. Офлайн-проверка (без LLM, только
router против ground truth из `benchmark/scenarios/catalog/*.yaml`)
подтвердила причину: **router физически прятал ожидаемый tool в 24.5%
ходов** (200/265 покрытие) — TSA не мог быть выше этого потолка вне
зависимости от качества модели. Юнит-тесты v1 (37/37 зелёных) ничего не
поймали, потому что были написаны на тех же фразах, что и сам router
(`s08_multiserver.yaml`) — самопроверка без независимого источника истины.

## v2 — архитектура

Три структурных изменения, каждое напрямую бьёт по причине провала v1:

1. **CORE — не домен, а всегда доступный базовый набор.** Общие утилиты
   (погода, calc, invoice, inventory, python_doc, healthcheck, ...)
   никогда не фильтруются — именно их скрытие давало основную часть
   провалов v1 при непопадании ни в одно keyword-правило.
2. **Sticky-домены + union вместо first-match.** Специализированный домен
   (HR/CRM/SSE/Policy), once matched, остаётся активным до конца сессии
   (`RouterState.activated_domains`), а не переопределяется с нуля на
   каждой реплике. Несколько совпавших доменов объединяются, а не
   конкурируют за приоритет в списке правил.
3. **Нет "crippled" fallback.** Если ни одно правило не сработало —
   доступен полный CORE (11 tools), а не safe subset из 2 инструментов.
   Router может только **расширять** набор специализированных tools,
   никогда не может дать меньше, чем baseline минус decoy.

```
user_message
    → route_turn(state, message)     # union совпавших доменов + sticky memory
    → allowed = CORE ∪ activated_domains_tools − decoy
    → filter_tools_by_route()        # подмножество BaseTool
    → tools_to_openai()
    → system_prompt + prompt_fragment (только для доменов, matched НА ЭТОМ ходу)
    → LLM
```

Единственное, что router по-прежнему безусловно убирает — `decoy_*`
инструменты. Это единственная часть v1, которая была чистым плюсом без
даунсайда, и в v2 сохранена без изменений.

## Домены и MCP-серверы

| Домен | MCP-сервер | Инструменты (без decoy) | Тип |
|-------|------------|--------------------------|-----|
| `core` (не Domain enum) | benchmark_core | weather, translate, invoice, calc, inventory, python_doc, healthcheck, get_policy_fact, … | всегда доступен |
| `hr` | benchmark_hr | employee_lookup, leave_balance, org_chart_dept | sticky, additive |
| `crm` | benchmark_crm | customer_get, ticket_create, sales_quote | sticky, additive |
| `sse` | benchmark_sse | benchmark_sse_probe, sse_audit_log | sticky, additive |
| `policy` | benchmark_core | get_policy_fact | sticky, additive |

`no_tools`/`unknown` как отдельные домены убраны: recall-эвристика — это
теперь только текстовая подсказка в prompt (`NO_TOOLS_HINT`), она не режет
tools; а "unknown" — это просто "ни один домен не matched", что уже
эквивалентно `allowed_tools == CORE_TOOLS`.

## Правила маршрутизации

`_ROUTING_RULES` больше не имеет приоритета по порядку — каждое совпавшее
правило добавляет свой домен в множество `matched`, все совпадения
учитываются. Ключевые слова сознательно расширены шире, чем в v1:
поскольку домены теперь только добавляют tools (никогда не режут), цена
false positive ≈ 0, а cost недомата (false negative) — потерянный TSA. Это
асимметрия, которую v1 не учитывал (там keyword-промах стоил всего hard
domain).

## Интеграция в agent_core.py

- `BasicLoopSession.__init__` / `reset()` — создают `RouterState()` на
  весь диалог (не пересоздаются на каждый turn).
- `run_turn()` — `route_turn(self._router_state, user_message)` мутирует
  state (добавляет matched-домены в sticky-память) и возвращает
  `RouteResult` для текущего хода.
- `_execute_single_tool()` — guard `is_tool_allowed()` (decoy или tool вне
  когда-либо активированных доменов).
- System prompt: базовый + fragment только для доменов, matched **на этом
  ходу** (не повторяет sticky-фрагменты прошлых ходов на каждой реплике).
- Также убран мёртвый дублирующийся блок методов
  `_execute_single_tool`/`_execute_tool_calls`/`_execute_tool_round`,
  оставшийся в файле от предыдущей правки (не исполнялся, второе
  определение в теле класса перекрывало первое).

## Тесты

```bash
cd improvements/05-deterministic-routing/variant
PYTHONPATH=. pytest tests/test_routing.py -q
```

Главный тест — `test_router_covers_full_catalog`: реплеит ВСЕ ~265 ходов
с `expect_tool_called` из `benchmark/scenarios/catalog/*.yaml` через
`route_turn` и проверяет, что ожидаемый tool не спрятан. Это
ground-truth тест на реальных данных бенчмарка, а не на фразах,
подобранных под сам router (как было в v1). Дополнительно: sticky-домен
без повтора keyword, union нескольких доменов, decoy-исключение на всём
каталоге, отсутствие "crippled" fallback.

Офлайн-результат (без LLM, чистая проверка router'а):

| | v1 | v2 |
|---|---|---|
| Покрытие ожидаемых tools в каталоге | 75.5% (200/265) | **100.0% (265/265)** |
| Decoy-утечка | 0 | 0 |

## Бенчмарк

```bash
cd experiments
PYTHONPATH=. python -m benchmark.compare --variant improvements/05-deterministic-routing/variant
```

100% офлайн-покрытие снимает структурный потолок TSA, который душил v1
(64% при потолке 75.5%). Дальше TSA/SR зависят уже от самой модели —
E2E-прогон требует доступа к `localhost` (недоступен
из текущего окружения на момент этой правки), поэтому финальные SR/TSA
нужно снять отдельным прогоном `benchmark.compare` и обновить
`report_05.md`/`SUMMARY.md`.

## Файлы

| Файл | Назначение |
|------|------------|
| `variant/routing.py` | Router v2: sticky-домены, union-match, CORE always-on |
| `variant/agent_core.py` | Per-turn роутинг с sticky state + guard на execute (дублирующийся мёртвый блок удалён) |
| `variant/tests/test_routing.py` | Ground-truth тест на полном каталоге + unit-тесты |
