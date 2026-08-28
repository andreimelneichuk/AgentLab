# IMPLEMENTATION — 17 Hierarchical Context Tree

> **v1 — persist the reduce, don't just render it transiently.** См. раздел
> "v1 fix" ниже: первая версия вычисляла `reduce_oldest()` заново на каждый
> LLM-раунд из полной сырой истории и никогда не сохраняла результат назад в
> `self._messages` — из-за этого сжимались всегда одни и те же самые старые
> K сегментов, а граница "старого" никогда не сдвигалась. На 100-ходовом
> сценарии это давало сжатие всего 2.3% (7531 → 7360 символов). Исправлено —
> см. числа "после" ниже (75.1% на длинных сценариях).

## Что реализовано

### 1. `variant/context_tree.py` — 2-уровневое дерево

- `build_segments(messages)` — группирует плоскую историю в атомарные
  turn-сегменты. Определение сегмента **побайтово идентично**
  09-context-engineering's `_group_into_segments()`: user/system — отдельный
  сегмент; assistant(+tool_calls) вместе со всеми своими tool results —
  всегда один сегмент (никогда не разбивается, иначе `tool_call_id`
  осиротеет — нарушение OpenAI/vLLM API adjacency).
- `MARKER_NEVER_DROP_REGEX` — **тот же regex**, что и
  `context_policy.MARKER_NEVER_DROP_REGEX` из 09 (скопирован дословно, не
  переписан заново), плюс `extract_markers()` — достаёт конкретные VALUES
  (`EMP_ID=HR-0042`), а не просто факт совпадения паттерна.
- `reduce_segments_to_node(segments, start_index, digest_text=None)` —
  редьюсит список leaf-сегментов в один `TreeNode(kind="summary")`:
  - Дайджест (переданный текст ИЛИ Tier-1 `default_digest()` fallback).
  - `[PRESERVED_MARKERS] ...` — все protected markers исходных сегментов,
    дописанные программно (не зависит от того, помнит ли их LLM).
  - `[SUMMARY_OF_SEGMENTS a:b]` — traceability-указатель на исходный диапазон
    (для дебага, не используется LLM).
- `reduce_oldest(segments, threshold, k, digest_fn=None)` — Tier-1
  синхронная версия (для offline-верификации без LLM): если
  `len(segments) <= threshold` — no-op (просто leaf-узлы). Иначе редьюсит
  **самые старые** `k` leaf-сегментов в один summary-узел, остальные
  (более новые) остаются leaf. `k` ограничен `len(segments) - 1`, чтобы
  последний (обычно самый свежий user-запрос) сегмент никогда не редьюсился.
- `reduce_oldest_async(segments, threshold, k, summarize_fn=None)` —
  Tier-2 асинхронная версия: `summarize_fn` — awaitable, делающий ОДИН
  LLM-вызов на сжатие. Если `summarize_fn=None` или падает с исключением —
  fallback на `default_digest()` (Tier-1, без LLM).
- `render_tree_to_messages(nodes)` — разворачивает дерево обратно в плоский
  список сообщений для `messages_to_langchain()`.
- `is_segment_atomic(segment)` — верификационный helper: множество
  `tool_call_id`, объявленных assistant-сообщениями в сегменте, должно
  ТОЧНО совпадать (`==`, не `⊆`) с множеством `tool_call_id` всех
  tool-сообщений сегмента — ни одного вызова без ответа, ни одного ответа
  без вызова.
- `HierarchicalTreeConfig` — `enabled`, `leaf_threshold` (default 6),
  `reduce_k` (default 3), парсится из `config.yml["hierarchical_context_tree"]`.

### 2. `variant/agent_core.py` — интеграция в `run_turn()`

`BasicLoopSession._reduce_stored_history_if_needed()` вызывается
**в начале каждого `run_turn()`**, до того как в историю добавляется новое
user-сообщение:

```python
async def run_turn(self, user_message: str) -> TurnResult:
    await self._reduce_stored_history_if_needed()
    ...
    messages = [*self._messages, {"role": "user", "content": user_message}]
```

`_reduce_stored_history_if_needed()`:
1. Tier-1 gate: `build_segments(self._messages)`, если
   `len(segments) <= leaf_threshold` — return, LLM не трогается.
2. Иначе строит LLM (best-effort, тем же alias, что и основной ход) для
   Tier-2 суммаризации; при любой ошибке сборки/вызова LLM — Tier-1
   `default_digest()` fallback (никогда не роняет ход).
3. `reduce_oldest_async()` → `self._messages = render_tree_to_messages(nodes)`
   — **персистентно** заменяет хранимую историю.

`_prepare_llm_messages()` (вызывается на каждый раунд tool-loop внутри
одного хода) теперь **не** пересчитывает reduce заново — дерево уже вплетено
в `self._messages` один раз за ход, дальше просто прогоняется обычный
09-style `context_policy.prepare_for_llm()` (Write/Select/Compress/Isolate)
поверх уже (при необходимости) сжатой истории.

### 3. `variant/config.yml`

```yaml
hierarchical_context_tree:
  enabled: true
  leaf_threshold: 6
  reduce_k: 3
```

## v1 fix: transient reduce никогда не сдвигал границу "старого"

**Симптом (обнаружен в offline-верификации, не в проде):** первая версия
вызывала `reduce_oldest()` внутри `_prepare_llm_messages()` — на КАЖДЫЙ
LLM-раунд, из копии `working` (полная сырая история + новые сообщения
текущего хода), но результат никогда не сохранялся обратно в
`self._messages`. Раз `self._messages` не менялся, на следующем ходе
`build_segments()` снова видел ПОЛНУЮ сырую историю с той же самой
позицией 0..k как "самые старые" — редьюсились одни и те же первые 3
сегмента раз за разом, а весь остальной рост истории оставался
несжатым.

**Проверено на `s11_drift_hr_001` (100 ходов, 178 leaf-сегментов):**
единственный вызов `reduce_oldest(segments, threshold=6, k=4)` над полной
финальной историей давал сжатие всего `7531 → 7360` символов (**2.3%**),
хотя в истории 178 сегментов, а threshold=6 — казалось бы, reduce должен
был сработать многократно.

**Причина:** `reduce_oldest()` по спецификации делает **один** reduce за
вызов (ОДИН summary-узел за ОДИН LLM-вызов) — это правильно и осталось
неизменным. Баг был не в `context_tree.py`, а в том, что `agent_core.py`
вызывал его транзиентно на каждый LLM-раунд вместо того, чтобы **сохранять
результат** в растущую историю между ходами — так что каждый ход "заново
открывал" один и тот же старый фронт вместо продвижения вперёд.

**Фикс:** `_reduce_stored_history_if_needed()` вызывается один раз в начале
`run_turn()` и пишет результат обратно в `self._messages`. Теперь на ходе N
редьюсятся 3 самых старых **ещё не редьюснутых** raw-сегмента, на ходе N+1 —
следующие 3, и т.д. — summary-узлы предыдущих волн никогда не трогаются
повторно (глубина дерева остаётся 2), но каждая волна двигает фронт вперёд.

## Offline-верификация

Скрипт (`benchmark/scenarios/catalog/*.yaml` + top-level `*.yaml`, 189
сценариев) строит синтетическую сессию из сообщений каждого сценария
(user-ходы + synthesized tool-вызовы/результаты с реальными
protected-marker значениями, взятыми из `expect_contains` каждого хода —
т.е. `EMP_ID=`, `LEAVE_DAYS=`, и т.д. получают конкретные synthesized
значения, как в реальном MCP-ответе), затем **симулирует персистентное
поведение `agent_core.py`**: на каждом "ходе" сначала прогоняет
`build_segments()` + (если нужно) `reduce_oldest()` над УЖЕ НАКОПЛЕННОЙ
историей, затем добавляет новые сообщения этого хода — точно как
`_reduce_stored_history_if_needed()` в реальном коде.

### Результаты: топ-8 самых длинных multi-turn сценариев (50-100 ходов)

| Сценарий | Ходов | Leaf-сегментов (raw) | Узлов после reduce | Маркеров сохранено | Символов до → после | Сжатие |
|---|---|---|---|---|---|---|
| s11_drift_hr_001 | 100 | 178 | 4 | 39/39 | 7531 → 1040 | 86.2% |
| s12_drift_multi_001 | 80 | 142 | 7 | 18/18 | 6236 → 1264 | 79.7% |
| s13_drift_adversarial_001 | 60 | 109 | 7 | 14/14 | 5513 → 1076 | 80.5% |
| s14_drift_recall_001 | 50 | 85 | 7 | 20/20 | 5159 → 1539 | 70.2% |
| long_horizon_quiz | 11 | 12 | 6 | 0/0 | 565 → 436 | 22.8% |
| creative_d_006 | 10 | 17 | 5 | 1/1 | 622 → 368 | 40.8% |
| s08_012 | 10 | 18 | 7 | 5/5 | 1045 → 631 | 39.6% |
| creative_d_007 | 9 | 13 | 6 | 1/1 | 508 → 406 | 20.1% |

**ИТОГО (топ-8): маркеры 98/98 (100.0%), символы 27179 → 6760 (75.1%
сокращение), 0 нарушений атомарности сегментов.**

### Результаты: весь каталог (189 сценариев, включая короткие 1-ходовые)

**ИТОГО (все 189): маркеры 229/229 (100.0%), символы 65067 → 42503
(34.7% сокращение), 0 нарушений атомарности.**

Более скромное сокращение по всему каталогу ожидаемо и правильно: медианный
сценарий каталога ~2.8 хода — короче `leaf_threshold=6`, reduce вообще не
срабатывает (что и требуется — короткие сессии не нуждаются в сжатии, а
ложное срабатывание было бы багом). Разница между 34.7% (весь каталог) и
75.1% (только длинные сессии) сама по себе — подтверждение того, что gate
работает избирательно, а не сжимает всё подряд.

### Проверка атомарности

Для каждого сценария после reduce рендер дерева обратно разбивается
`build_segments()` и каждый получившийся сегмент проверяется
`is_segment_atomic()` — **0 нарушений** на все 189 сценариев: ни один
`tool_call` не остался без `tool_result`, и наоборот.

### Проверка сохранения маркеров

Для каждого сценария собирается ground-truth множество всех
protected-marker значений (`extract_markers()`) из СЫРЫХ сегментов ДО
reduce, затем проверяется присутствие каждого значения verbatim в
финальном отрендеренном тексте ПОСЛЕ всех волн reduce — **229/229 (100%)**
на всём каталоге, включая drift-сценарии с десятками маркеров,
накопленных за 50-100 ходов.

## Как прогнать тесты

```bash
cd improvements/17-hierarchical-context-tree/variant
PYTHONPATH=. /path/to/AgentLab/.venv/bin/python -m pytest tests/ -q
```

**41 passed** (`tests/test_context_tree.py`) — покрывает: atomicity
(orphaned tool_call / orphaned tool_result detection), marker preservation
(verbatim в summary-узле), traceability pointer, reduce threshold behavior
(no-op below threshold, triggers above, never reduces last segment, output
strictly shorter), Tier-1/Tier-2 gate (summarize_fn НЕ вызывается ниже
threshold, вызывается РОВНО один раз выше threshold), config parsing.
