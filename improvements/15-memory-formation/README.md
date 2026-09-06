# 15 — Формирование памяти (Memory Formation)

**Tier:** 3  
**Источник:** Mem0 Blog (октябрь 2025)  
**Проблема:** Потеря памяти между сессиями; суммаризация истории — lossy и без семантического поиска

> **v4 — все 4 пробела из первого Judge-отчёта закрыты:**
> 1. `retriever.py`: builtin `hash()` для эмбеддинга рандомизирован
>    per-process (`PYTHONHASHSEED`) — эмбеддинг факта не сопоставим с
>    эмбеддингом того же текста после рестарта сервера. Fix: `hashlib.blake2b`.
> 2. `extractor.py`: три разных типа предпочтений делили один
>    `key_template` → extraction молча терял 2-е и 3-е совпадение в одном
>    вызове. Подтверждено на `s11_drift_hr_001` (3 факта из 4 терялись).
>    Fix: разделили на отдельные key_template.
> 3. Не было dedicated 2-session сценария в бенчмарке — добавлен
>    `benchmark/scenarios/memory_recall.yaml` + аддитивный флаг хода
>    `new_session: true` в `benchmark/compare.py`.
> 4. Не было `UNIQUE(user_id, fact_key)` в SQLite — добавлен unique index +
>    atomic `ON CONFLICT DO UPDATE` в `store.py`.
>
> См. IMPLEMENTATION.md/JUDGE.md для деталей верификации каждого пункта.

## Эволюция подходов

| Уровень | Подход | Проблема |
|---------|--------|----------|
| 1 | Last-N сообщений | Теряется начало сессии |
| 2 | Лимит по токенам | Выбрасывается нужное |
| 3 | Суммаризация (Microsoft-style) | Lossy, нет точечного recall |
| 4 | **Memory Formation (Mem0)** | Дискретные факты, UPDATE не ADD |

## Ключевое различие

**Суммаризация:** всё сжато в один текст → потери, дубли, нет поиска по факту.

**Memory Formation:** извлечение **дискретных фактов** → хранение как обновляемых единиц → semantic retrieval при новом запросе.

Пример:

```
Сессия 1: "Я предпочитаю Python"
Сессия 5: "Используй Python, не JavaScript"
→ Одна каноническая запись: preference_language=Python (UPDATED)
```

## Pipeline Mem0

```
1. Extraction — из диалога извлечь факты
      ("пользователь предпочитает Python")

2. Dedup + Update — похожий факт есть? → UPDATE, не ADD

3. Semantic retrieval — при новом запросе top-k фактов в контекст

4. Compact memory — база не растёт бесконечно
```

## З заявленные результаты (Mem0)

- −80–90% токенов vs полная история
- +26% качество ответов (их бенчмарк)

## Что менять в Baseline Agent

| Место | Действие |
|-------|----------|
| `memory/` модуль | Extractor + store + retriever |
| `agent_core.py` | Inject retrieved facts в system/user context |
| Multi-session тесты | Сессия N помнит факты из сессии 1 |

### Типы фактов для Baseline Agent

- Предпочтения пользователя (язык, формат ответа)
- Уже полученные entity IDs (employee_id, policy_code)
- Решения по прошлым задачам («в прошлый раз использовали get_policy_fact»)
- **Не** хранить: сырые tool XML, временные ошибки

## Критерии успеха

- Multi-turn across sessions: корректный recall без повторного tool call
- Нет дубликатов противоречивых фактов
- Изоляция по `user_id` (privacy)
- Лучше чем Level-3 суммаризация на `s04_003`-подобных сценариях

## Антипаттерн (из документа)

**Только суммаризация как память** — теряет факты, не поддерживает семантический поиск, не обновляет канонически.

Связь с **07 Focus:** Focus сжимает **внутри сессии**; Mem0 — **между сессиями**.

## Компромиссы

| Ось | Выбор |
|-----|-------|
| Точность vs стоимость | Лимит top-k фактов в контекст |
| Персонализация vs privacy | user_id isolation + retention policy |
| Авто vs ручное | Какие типы фактов разрешено извлекать |

## Зависимости

- Mem0 SDK или собственная реализация (vector store + extractor LLM)
- **04** — не «вспоминать» факты, которые требуют свежего tool call (policy может измениться)

## Чеклист

- [x] Политика: какие факты extract / never store
- [x] Extractor prompt + dedup logic
- [x] Vector store с user scope
- [x] TTL / invalidation для time-sensitive facts (policy!)
- [x] Benchmark: 2-session сценарий с recall
