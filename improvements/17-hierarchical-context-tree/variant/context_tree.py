"""Иерархическое дерево контекста (2-уровневый block-tree MapReduce).

Источник идеи: emergentmind.com, "Hierarchical MapReduce Structures" —
вместо одного плоского Compress/Select прохода (см. 09-context-engineering/
context_policy.py) история сообщений организуется в 2-уровневое дерево:

  Level 0 (leaves)  — атомарные turn-сегменты (тот же atomic-unit, что и
                       09's _group_into_segments(): user-сообщение ИЛИ
                       assistant(+tool_calls) вместе со всеми tool results —
                       никогда не разбиваются).
  Level 1 (summary) — как только число leaf-сегментов превышает threshold,
                       САМЫЕ СТАРЫЕ K leaf-сегментов "редьюсятся" одним LLM
                       вызовом в один compact summary-узел. Summary-узел
                       обязан verbatim сохранить любые protected markers
                       (см. MARKER_NEVER_DROP_REGEX, импортирован из 09 —
                       не переизобретаем regex) плюс traceability-указатель
                       на исходные сегменты (для дебага, не используется LLM).

Явное ограничение объёма (см. README.md "Scope decision"): дерево НЕ
рекурсивное. Summary-узлы никогда сами не редьюсятся в summary-of-summaries
(N-уровневый MapReduce) — сессии бенчмарка редко превышают ~10-15 ходов,
поэтому вторая волна reduce практически никогда не сработает, а поддержка
рекурсии добавила бы сложность без измеримой пользы. Если суммарных leaf-
сегментов после одного reduce-прохода снова накопится больше threshold,
reduce_oldest() просто редьюсит их в ЕЩЁ ОДИН summary-узел verbatim-уровня
(summary-узлы никогда не становятся входом reduce), т.е. глубина дерева
всегда <= 2.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

# Переиспользуем ТОЧНО тот же regex, что и 09-context-engineering, вместо
# того чтобы заново придумывать список маркеров (EMP_ID=, POLICY_FACT=, ...).
MARKER_NEVER_DROP_REGEX = re.compile(
    r"(EMP_ID=|POLICY_FACT=|BENCH_MARKER_\w+=|CUSTOMER=|INVOICE=|AUDIT\b|"
    r"TICKET_ID=|QUOTE\b|LEAVE_DAYS=|DEPT=|RESULT=|WEATHER\b|TRANSLATED\[|"
    r"RANDOM_MARKER=)",
    re.IGNORECASE,
)

DEFAULT_LEAF_THRESHOLD = 6
DEFAULT_REDUCE_K = 3

SummarizeFn = Callable[[List["Segment"]], Awaitable[str]]


Segment = List[Dict[str, Any]]


@dataclass
class HierarchicalTreeConfig:
    """Настройки 2-уровневого дерева контекста."""

    enabled: bool = True
    leaf_threshold: int = DEFAULT_LEAF_THRESHOLD
    reduce_k: int = DEFAULT_REDUCE_K

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "HierarchicalTreeConfig":
        raw = (config.get("hierarchical_context_tree") or {})
        return cls(
            enabled=bool(raw.get("enabled", True)),
            leaf_threshold=int(raw.get("leaf_threshold", DEFAULT_LEAF_THRESHOLD)),
            reduce_k=int(raw.get("reduce_k", DEFAULT_REDUCE_K)),
        )


@dataclass
class TreeNode:
    """Один узел дерева контекста."""

    kind: str  # "leaf" | "summary"
    messages: Segment = field(default_factory=list)
    # Для summary-узлов: индексы исходных leaf-сегментов, которые он заменил
    # (для traceability/debugging — LLM их не видит и не использует).
    source_segment_range: Optional[tuple] = None
    preserved_markers: List[str] = field(default_factory=list)


def extract_markers(text: str) -> List[str]:
    """Достаёт все protected-marker подстроки (verbatim) из текста.

    Возвращает не сам regex-match (короткий тег типа "EMP_ID="), а всё
    "слово" вокруг него (до ближайшего разделителя), чтобы в дайджест
    попадало конкретное значение, например "EMP_ID=HR-42117", а не голый
    префикс.
    """
    if not text:
        return []
    markers: List[str] = []
    # Токен = непрерывная последовательность без пробелов/переносов строк,
    # содержащая маркер-паттерн где-то внутри.
    for token in re.findall(r"\S+", text):
        if MARKER_NEVER_DROP_REGEX.search(token):
            # Отрезаем висящую пунктуацию по краям (запятые, скобки, точки).
            cleaned = token.strip(").,;:]}\"'")
            if cleaned:
                markers.append(cleaned)
    return markers


def segment_text(segment: Segment) -> str:
    return "\n".join(str(m.get("content") or "") for m in segment)


def build_segments(messages: List[Dict[str, Any]]) -> List[Segment]:
    """Группирует плоскую историю в атомарные turn-сегменты.

    Идентичное определение атомарного сегмента, что и в 09's
    _group_into_segments(): user-сообщение отдельно; assistant(+tool_calls)
    вместе со ВСЕМИ своими tool results в одном сегменте — никогда не
    разбиваются (иначе tool_call_id останется без пары, что ломает
    OpenAI/vLLM API adjacency-требование).
    """
    segments: List[Segment] = []
    current: Segment = []

    for msg in messages:
        role = msg.get("role", "")
        if role in ("user", "system"):
            if current:
                segments.append(current)
            current = [msg]
        elif role == "assistant":
            if current and current[-1].get("role") == "tool":
                segments.append(current)
                current = [msg]
            elif current and current[0].get("role") == "assistant":
                segments.append(current)
                current = [msg]
            else:
                current.append(msg)
        elif role == "tool":
            current.append(msg)
        else:
            current.append(msg)

    if current:
        segments.append(current)
    return segments


def build_leaf_nodes(messages: List[Dict[str, Any]]) -> List[TreeNode]:
    """Строит Level-0 (leaf) узлы дерева из плоской истории."""
    return [TreeNode(kind="leaf", messages=seg) for seg in build_segments(messages)]


def default_digest(segments: List[Segment]) -> str:
    """Tier-1 (без LLM) fallback-суммаризатор: детерминированный digest.

    Используется когда summarize_fn не передан (например, в offline-
    верификации без живого LLM) — просто перечисляет роли/длины сегментов.
    Формат совпадает с тем, что ожидал бы Tier-2 LLM-редьюс, чтобы модульные
    тесты могли проверять пайплайн без сети.
    """
    parts = []
    for seg in segments:
        roles = "+".join(m.get("role", "?") for m in seg)
        parts.append(f"[{roles}: {len(segment_text(seg))} chars]")
    return "Ранее в диалоге: " + "; ".join(parts) + "."


def reduce_segments_to_node(
    segments: List[Segment],
    start_index: int,
    digest_text: Optional[str] = None,
) -> TreeNode:
    """Редьюсит один список leaf-сегментов в ОДИН summary-узел.

    Гарантирует: любой protected marker (EMP_ID=, POLICY_FACT=, ...),
    встречавшийся verbatim в исходных сегментах, дословно присутствует в
    итоговом summary-content — они дописываются отдельным блоком после
    сжатого дайджеста, а не полагаются на то, что LLM их случайно сохранит.
    """
    all_markers: List[str] = []
    seen = set()
    for seg in segments:
        for m in extract_markers(segment_text(seg)):
            if m not in seen:
                seen.add(m)
                all_markers.append(m)

    digest = digest_text if digest_text is not None else default_digest(segments)

    content_lines = [digest]
    if all_markers:
        content_lines.append(
            "[PRESERVED_MARKERS] " + " ".join(all_markers)
        )
    content_lines.append(
        f"[SUMMARY_OF_SEGMENTS {start_index}:{start_index + len(segments)}]"
    )

    summary_msg = {
        "role": "system",
        "content": "\n".join(content_lines),
        "_summary_node": True,
        "_source_range": [start_index, start_index + len(segments)],
    }

    return TreeNode(
        kind="summary",
        messages=[summary_msg],
        source_segment_range=(start_index, start_index + len(segments)),
        preserved_markers=all_markers,
    )


def reduce_oldest(
    segments: List[Segment],
    threshold: int = DEFAULT_LEAF_THRESHOLD,
    k: int = DEFAULT_REDUCE_K,
    digest_fn: Optional[Callable[[List[Segment]], str]] = None,
) -> List[TreeNode]:
    """Строит 2-уровневое дерево из leaf-сегментов.

    Если len(segments) <= threshold: дерево — просто leaf-узлы, reduce не
    срабатывает (нечего сжимать).

    Иначе: самые старые K leaf-сегментов редьюсятся в ОДИН summary-узел
    (Level 1), остальные (более новые) leaf-сегменты остаются как есть
    (Level 0). K ограничен len(segments) - 1, чтобы хотя бы один свежий
    leaf-сегмент (обычно последний user-запрос) никогда не редьюсился.

    Намеренно НЕ рекурсивно: summary-узел, попав в результат, никогда сам
    не становится входом повторного reduce_oldest() в рамках одного вызова —
    только вызывающий код (agent_core.run_turn) может на следующем ходе
    заново скормить сюда leaf-сегменты (summary-узел к тому моменту уже
    "затвердел" как один системный message и просто останется первым
    элементом истории). Если новых leaf-сегментов снова накопится больше
    threshold, они будут отдельно редьюситься в ещё один summary-узел —
    глубина дерева остаётся 2, а не растёт (summary-of-summaries не
    строится).
    """
    if len(segments) <= threshold:
        return [TreeNode(kind="leaf", messages=seg) for seg in segments]

    k = max(1, min(k, len(segments) - 1))
    oldest = segments[:k]
    rest = segments[k:]

    digest_text = digest_fn(oldest) if digest_fn else None
    summary_node = reduce_segments_to_node(oldest, start_index=0, digest_text=digest_text)

    nodes: List[TreeNode] = [summary_node]
    nodes.extend(TreeNode(kind="leaf", messages=seg) for seg in rest)
    return nodes


async def reduce_oldest_async(
    segments: List[Segment],
    threshold: int = DEFAULT_LEAF_THRESHOLD,
    k: int = DEFAULT_REDUCE_K,
    summarize_fn: Optional[SummarizeFn] = None,
) -> List[TreeNode]:
    """Async-вариант reduce_oldest для Tier-2 (LLM) суммаризации.

    summarize_fn(segments) -> awaitable[str] делает один LLM-вызов, чтобы
    сжать самые старые K сегментов в короткий дайджест. Protected markers
    всё равно дописываются verbatim в reduce_segments_to_node() независимо
    от того, что вернул LLM — так что даже если LLM их "забудет" в
    суммаризации, они не теряются.
    """
    if len(segments) <= threshold:
        return [TreeNode(kind="leaf", messages=seg) for seg in segments]

    k = max(1, min(k, len(segments) - 1))
    oldest = segments[:k]
    rest = segments[k:]

    digest_text = await summarize_fn(oldest) if summarize_fn else None
    summary_node = reduce_segments_to_node(oldest, start_index=0, digest_text=digest_text)

    nodes: List[TreeNode] = [summary_node]
    nodes.extend(TreeNode(kind="leaf", messages=seg) for seg in rest)
    return nodes


def render_tree_to_messages(nodes: List[TreeNode]) -> List[Dict[str, Any]]:
    """Разворачивает дерево обратно в плоский список сообщений для LLM.

    Порядок сохраняется (summary-узлы, если есть, всегда старше leaf-узлов,
    т.к. reduce_oldest всегда редьюсит только самые старые сегменты) —
    tool_call/tool_result adjacency внутри leaf-сегментов не затрагивается,
    т.к. leaf-сегменты копируются как есть.
    """
    flat: List[Dict[str, Any]] = []
    for node in nodes:
        flat.extend(node.messages)
    return flat


def is_segment_atomic(segment: Segment) -> bool:
    """Проверяет, что сегмент не содержит "осиротевший" tool_call/result.

    Правило (позиционно-независимое, т.к. сегмент может быть
    [user, assistant+tool_calls, tool*] — user идёт первым): множество
    tool_call id, объявленных assistant-сообщениями внутри сегмента, должно
    ТОЧНО совпадать с множеством tool_call_id всех tool-сообщений в этом же
    сегменте — ни один вызов не остался без ответа, ни один ответ не
    "осиротел" без своего вызова.
    """
    if not segment:
        return True
    call_ids: set = set()
    for m in segment:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            call_ids.update(tc.get("id") for tc in m["tool_calls"] if tc.get("id"))
    tool_ids = {m.get("tool_call_id") for m in segment if m.get("role") == "tool"}
    if not call_ids and not tool_ids:
        return True
    return call_ids == tool_ids
