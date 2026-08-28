# Judge Report — 07-focus-active-compression (v2: Knowledge Index)

> **Переработка с v1 (Compression → Index):** SR был 12%, ожидаем 28-32% благодаря indexed recall вместо compression.

**Оценка оконченности:** 85/100 — **Alpha (переработана, тесты ожидают)**

## Компоненты

| Компонент | Статус | Значение |
|-----------|--------|----------|
| Spec (README) | ✅ обновлена | Knowledge Index архитектура описана |
| Code (focus.py) | ✅ переписана | IndexEntry, KnowledgeIndex, auto-indexing |
| Code (agent_core интеграция) | ⏳ требует обновления | Нужно добавить KnowledgeIndexManager и auto-index calls |
| System prompt | ⏳ требует обновления | Инструкции для knowledge_index_lookup |
| Tests | ⏳ требует расширения | Старые тесты focus.py нужно переписать на новую API |
| Бенчмарк (ожидаемо) | 🔮 | SR=28-32% (было 12%, baseline 26%) |

## v1 → v2 Почему переработка

### v1 (Compression) проблемы
- SR=12% (минус 14pp от baseline)
- Recall-сценарии теряют детали при удалении истории
- Модель не обучена когда/как использовать compressed summary

### v2 (Knowledge Index) решение
- ✅ История НЕ удаляется, только индексируется
- ✅ Recall работает через indexed lookup (полный формат в индексе)
- ✅ Pseudo-tool `knowledge_index_lookup` научит модель когда запросить индекс
- ✅ Auto-indexing парсит tool response и добавляет в индекс (без ручной работы)

## Ожидаемые метрики (v2)

| Метрика | v1 (Compress) | v2 (Index) | Baseline |
|---------|---------------|-----------|----------|
| SR | 12% | **28-32%** | 26% |
| CSR | 22% | **30-35%** | 43% |
| TSA | 67% | **95%+** | 95% |
| TA | 27% | **50%+** | 49% |
| AH | 39% | **65%+** | 57% |
| Latency | 1.10s | **≈0.75s** | 0.73s |

## Что осталось (перед бенчмарком)

1. **Обновить `agent_core.py`**
   - Создать `KnowledgeIndexManager()` в `BasicLoopSession.__init__`
   - После каждого успешного tool call вызвать `auto_index_from_tool_response()`
   - Инжектировать Knowledge Index в system context
   - Добавить обработку `knowledge_index_lookup` в `_execute_tools`

2. **Обновить `system_master.txt`**
   - Добавить секцию "СЕМАНТИЧЕСКИЙ ИНДЕКС (KNOWLEDGE INDEX)"
   - Инструкции: "перед tool call поищи в индексе"
   - Примеры использования `knowledge_index_lookup`

3. **Переписать/расширить `test_focus.py`**
   - Auto-indexing тесты (parse policy_fact, employee_lookup, etc.)
   - Lookup тесты (found / not found cases)
   - Integration тесты (full workflow)
   - ~12 новых тестов должны pass

4. **E2E валидация на catalog** (требует vLLM)
   - Запустить бенчмарк и проверить что SR ≥ 28%

## Рекомендации

1. **Немедленно:** Реализовать обновления в agent_core.py + system prompt (1-2 часа)
2. **Тесты:** Расширить test_focus.py с новыми API (1 час, 12+ тестов)
3. **Валидация:** E2E бенчмарк на catalog (требует доступ к llm-server)

## Бонусные идеи (future)

- Garbage collection: удалять entries которые не запрашивались в последние N ходов
- Kategory-specific parsing: более сложный парс для разных tool-ов
- Confidence scoring: зависит от freshness и hit rate
- Knowledge block export: сохранять индекс между сессиями
