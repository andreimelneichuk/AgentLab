# Judge Report — 09-context-engineering (v2)

> **Историческая заметка по v1:** оценка 100/100 (checklist 5/5, tests 15/15)
> оказалась **ложноположительной** — тесты проверяли каждую стратегию
> изолированно (`test_isolate_separates_blocks` проверял только count
> сообщений в блоке), но не end-to-end инвариант порядка. Реальный e2e прогон
> дал SR=13%, TA=23% (худшее среди всех 14 вариантов) из-за физической
> пересортировки сообщений в Isolate, ломающей OpenAI/vLLM tool_call adjacency.
> Все три причины исправлены в v2 — см. IMPLEMENTATION.md.

**Оценка оконченности v2:** 90/100 — **Alpha (исправлены критичные баги, ожидает e2e)**

| Измерение | v1 | v2 |
|-----------|----|----|
| Checklist | 5/5 | 6/6 (добавлен regression-тест adjacency) |
| Tests | 15/15 (не покрывали инвариант) | 21/21 (+6 на инвариант order/marker-safety) |
| Известные баги | 3 критичных, не найдены тестами | 0 известных |
| Бенчмарк SR | 13% | ожидается 22-26% (требует e2e на vLLM) |

## Три бага v1 и их исправление в v2

1. **Isolate reordering** (главная причина провала)
   - Было: `isolate_message_blocks()` собирал chat и tool сообщения раздельно,
     склеивал в порядке `[system, chat..., tools...]` — теряя причинно-следственную
     связь `tool_call → tool_result`
   - Стало: помечает `_block` без изменения порядка; проверено тестом
     `test_isolate_no_reorder_matches_input_order`

2. **Select orphaning**
   - Было: фильтрация по отдельным сообщениям, `assistant` с пустым content
     (data в tool_calls) почти всегда терял keyword-overlap и дропался, оставляя
     осиротевший `tool` result
   - Стало: `_group_into_segments()` — атомарные группы, проверено тестом
     `test_select_never_orphans_tool_result_without_its_assistant_call`

3. **Never-drop не защищал recall-маркеры**
   - Было: только текстовые security-паттерны, реальные MCP-маркеры
     (`EMP_ID=`, `violet-42`, ...) маскировались на 3-м ходу
   - Стало: `MARKER_NEVER_DROP_REGEX` + порог `mask_tool_older_than_turns` 2→4,
     проверено тестами `test_compress_never_masks_benchmark_markers` и
     `test_compress_masks_non_marker_old_tool_results`

## Незакрытые пункты (перед full production)

- [ ] E2E бенчмарк на `localhost` для подтверждения SR 22-26%
- [ ] Опционально: замена статичного `MARKER_NEVER_DROP_REGEX` на интеграцию
  с Knowledge Index (эксперимент 07) для более общего решения вне рамок этого
  конкретного бенчмарка

## Рекомендации

1. Прогнать e2e на catalog для подтверждения ожидаемых метрик
2. Взять `_group_into_segments()` + adjacency-invariant тест как **паттерн**
   для будущих доработок, трогающих message list (07, 10, 13) — этот класс
   багов легко пропустить, если тестировать стратегии изолированно от e2e
   message flow
