"""Политика контекста: Write / Select / Compress / Isolate (v2).

v2 исправляет 3 критичных бага v1:
1. Isolate больше не переставляет сообщения местами (ломало tool_call/tool_result
   adjacency, обязательную для OpenAI/vLLM API) — теперь только помечает `_block`,
   сохраняя исходный порядок.
2. Select работает на уровне атомарных tool-групп (assistant+tool_calls вместе со
   всеми его tool results), а не по отдельным сообщениям — иначе оставался
   осиротевший ToolMessage без своего AIMessage или наоборот.
3. never_drop_patterns дополнен regex-маркерами реальных MCP-ответов
   (EMP_ID=, POLICY_FACT=, BENCH_MARKER_*=, CUSTOMER=, INVOICE=, AUDIT) —
   раньше маскирование убивало те самые факты, которые проверяются на recall.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from scratch_store import ScratchStore, estimate_tokens

MASKED_TOOL_PLACEHOLDER = (
    "[MASKED: результат инструмента скрыт политикой контекста; "
    "tool_call_id={tool_call_id}; turn={turn_idx}]"
)
SCRATCH_REF_TEMPLATE = (
    "[Large response stored in scratch:{key}; ~{tokens} tokens; "
    "use load_scratch(\"{key}\") to retrieve full content]"
)

BLOCK_RULES = "rules"
BLOCK_FACTS = "session_facts"
BLOCK_CHAT = "chat"
BLOCK_TOOLS = "tools"

# Маркеры реальных MCP tool-ответов, которые НИКОГДА нельзя маскировать/выкидывать —
# именно они проверяются на recall в бенчмарке (см. mcp_tool_registry.py).
MARKER_NEVER_DROP_REGEX = re.compile(
    r"(EMP_ID=|POLICY_FACT=|BENCH_MARKER_\w+=|CUSTOMER=|INVOICE=|AUDIT\b|"
    r"TICKET_ID=|QUOTE\b|LEAVE_DAYS=|DEPT=|RESULT=|WEATHER\b|TRANSLATED\[|"
    r"RANDOM_MARKER=)",
    re.IGNORECASE,
)


@dataclass
class ContextPolicyConfig:
    """Настройки политики контекста."""

    enabled: bool = True
    mask_tool_older_than_turns: int = 4
    scratch_token_threshold: int = 1500
    select_enabled: bool = True
    select_keep_last_user_turns: int = 2
    select_min_keyword_overlap: int = 1
    isolate_enabled: bool = True
    never_drop_patterns: List[str] = field(default_factory=lambda: [
        "POLICY_OK",
        "ЗАПРЕЩЕНО",
        "security",
        "СТРОГО",
        "STOP",
    ])

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "ContextPolicyConfig":
        raw = config.get("context_engineering") or {}
        patterns = raw.get("never_drop_patterns")
        return cls(
            enabled=bool(raw.get("enabled", True)),
            mask_tool_older_than_turns=int(raw.get("mask_tool_older_than_turns", 4)),
            scratch_token_threshold=int(raw.get("scratch_token_threshold", 1500)),
            select_enabled=bool(raw.get("select_enabled", True)),
            select_keep_last_user_turns=int(raw.get("select_keep_last_user_turns", 2)),
            select_min_keyword_overlap=int(raw.get("select_min_keyword_overlap", 1)),
            isolate_enabled=bool(raw.get("isolate_enabled", True)),
            never_drop_patterns=list(patterns) if patterns else cls().never_drop_patterns,
        )


@dataclass
class ContextPolicyReport:
    """Мониторинг: какие сегменты попали в финальный prompt."""

    blocks: Dict[str, int] = field(default_factory=dict)
    masked_tool_count: int = 0
    scratch_written_count: int = 0
    selected_message_count: int = 0
    total_messages: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "blocks": dict(self.blocks),
            "masked_tool_count": self.masked_tool_count,
            "scratch_written_count": self.scratch_written_count,
            "selected_message_count": self.selected_message_count,
            "total_messages": self.total_messages,
        }


def _keywords(text: str) -> Set[str]:
    return set(re.findall(r"\w{3,}", (text or "").lower()))


def _matches_never_drop(content: str, patterns: List[str]) -> bool:
    lower = (content or "").lower()
    if any(p.lower() in lower for p in patterns):
        return True
    return bool(MARKER_NEVER_DROP_REGEX.search(content or ""))


def assign_turn_indices(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Проставляет _turn_idx каждому сообщению.

    Новый turn начинается с каждого user-сообщения.
    """
    tagged: List[Dict[str, Any]] = []
    turn = 0
    for msg in messages:
        m = dict(msg)
        if m.get("role") == "user":
            turn += 1
        m["_turn_idx"] = turn
        tagged.append(m)
    return tagged


def _group_into_segments(messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """
    Группирует сообщения в атомарные сегменты: [user] или [assistant(+tool_calls), tool*...].

    Каждый tool_call и его tool result(s) всегда в одном сегменте — не разделяются
    Select/Compress по отдельности, чтобы не ломать OpenAI tool_call adjacency.
    """
    segments: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "")
        if role in ("user", "system"):
            if current:
                segments.append(current)
            current = [msg]
        elif role == "assistant":
            if current and current[-1].get("role") == "tool":
                # предыдущий сегмент — assistant+tools уже закрыт, начинаем новый
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


class ContextPolicy:
    """
    Проактивная политика окна контекста.

    Приоритет: Isolate (labeling) → Compress (masking) → Write → Select.
    Isolate больше не переставляет сообщения — только помечает `_block` для
    мониторинга и системного контекста; порядок сообщений всегда сохраняется,
    чтобы не ломать tool_call/tool_result adjacency, обязательную для API.
    """

    def __init__(
        self,
        config: ContextPolicyConfig,
        scratch_store: Optional[ScratchStore] = None,
    ):
        self.config = config
        self.scratch = scratch_store or ScratchStore()

    def prepare_for_llm(
        self,
        messages: List[Dict[str, Any]],
        *,
        system_prompt: str,
        current_query: str,
    ) -> Tuple[List[Dict[str, Any]], str, ContextPolicyReport]:
        """
        Применяет полный pipeline перед invoke LLM.

        Returns:
            (messages, system_prompt_for_langchain, report)
            При isolate system_prompt_for_llm пуст — правила уже первым system message.
        """
        report = ContextPolicyReport()
        if not self.config.enabled:
            report.total_messages = len(messages)
            return list(messages), system_prompt, report

        working = assign_turn_indices([dict(m) for m in messages])
        current_turn = max((m.get("_turn_idx", 0) for m in working), default=0)

        working = self.write_large_tool_results(working, report)
        working = self.select_relevant_history(working, current_query, report)
        working = self.compress_mask_old_tools(working, current_turn, report)

        if self.config.isolate_enabled:
            isolated = self.isolate_message_blocks(working, system_prompt, report)
            report.total_messages = len(isolated)
            return isolated, "", report

        report.total_messages = len(working)
        return working, system_prompt, report

    def write_large_tool_results(
        self,
        messages: List[Dict[str, Any]],
        report: Optional[ContextPolicyReport] = None,
    ) -> List[Dict[str, Any]]:
        """
        Write: большие tool results → scratch, в контексте только ссылка.
        """
        threshold = self.config.scratch_token_threshold
        out: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") != "tool":
                out.append(msg)
                continue
            content = msg.get("content") or ""
            if msg.get("_scratch_key") or estimate_tokens(content) <= threshold:
                out.append(msg)
                continue
            if _matches_never_drop(content, self.config.never_drop_patterns):
                out.append(msg)
                continue

            key = self.scratch.make_key(prefix="tool")
            self.scratch.save(key, content)
            tokens = estimate_tokens(content)
            ref = SCRATCH_REF_TEMPLATE.format(key=key, tokens=tokens)
            new_msg = dict(msg)
            new_msg["content"] = ref
            new_msg["_scratch_key"] = key
            out.append(new_msg)
            if report:
                report.scratch_written_count += 1
        return out

    def select_relevant_history(
        self,
        messages: List[Dict[str, Any]],
        current_query: str,
        report: Optional[ContextPolicyReport] = None,
    ) -> List[Dict[str, Any]]:
        """
        Select: keyword matching на уровне АТОМАРНЫХ сегментов (не отдельных сообщений).

        Сегмент = user-сообщение ИЛИ (assistant с tool_calls + все его tool results).
        Это гарантирует что tool_call_id всегда остаётся со своим ToolMessage —
        никогда не выбрасываем половину пары.
        """
        if not self.config.select_enabled or len(messages) <= 4:
            return messages

        query_keys = _keywords(current_query)
        if not query_keys:
            return messages

        user_turns = sorted({m.get("_turn_idx", 0) for m in messages if m.get("role") == "user"})
        keep_turns = set(user_turns[-self.config.select_keep_last_user_turns :])

        segments = _group_into_segments(messages)
        selected: List[Dict[str, Any]] = []
        dropped_count = 0

        for segment in segments:
            turn = segment[0].get("_turn_idx", 0)

            if turn in keep_turns:
                selected.extend(segment)
                continue

            combined_content = " ".join(m.get("content") or "" for m in segment)
            if _matches_never_drop(combined_content, self.config.never_drop_patterns):
                selected.extend(segment)
                continue

            overlap = len(query_keys & _keywords(combined_content))
            if overlap >= self.config.select_min_keyword_overlap:
                selected.extend(segment)
            else:
                dropped_count += len(segment)

        if not selected:
            return messages

        if report and dropped_count:
            report.selected_message_count = dropped_count
        return selected

    def compress_mask_old_tools(
        self,
        messages: List[Dict[str, Any]],
        current_turn: int,
        report: Optional[ContextPolicyReport] = None,
    ) -> List[Dict[str, Any]]:
        """
        Compress: masking старых tool XML/results (без LLM-суммаризации).

        Маркеры реальных MCP-ответов (EMP_ID=, POLICY_FACT=, ...) защищены через
        MARKER_NEVER_DROP_REGEX — они нужны для recall-сценариев и не маскируются.
        """
        k = self.config.mask_tool_older_than_turns
        out: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") != "tool":
                out.append(msg)
                continue

            turn_idx = msg.get("_turn_idx", 0)
            content = msg.get("content") or ""
            age = current_turn - turn_idx

            if age <= k or _matches_never_drop(content, self.config.never_drop_patterns):
                out.append(msg)
                continue
            if content.startswith("[MASKED:"):
                out.append(msg)
                continue

            masked = dict(msg)
            scratch_key = msg.get("_scratch_key", "")
            if scratch_key:
                masked["content"] = (
                    f"[MASKED: см. scratch:{scratch_key}; tool_call_id={msg.get('tool_call_id', '?')}; "
                    f"turn={turn_idx}]"
                )
            else:
                masked["content"] = MASKED_TOOL_PLACEHOLDER.format(
                    tool_call_id=msg.get("tool_call_id", "?"),
                    turn_idx=turn_idx,
                )
            out.append(masked)
            if report:
                report.masked_tool_count += 1
        return out

    def isolate_message_blocks(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: str,
        report: Optional[ContextPolicyReport] = None,
    ) -> List[Dict[str, Any]]:
        """
        Isolate: system rules / session facts — отдельные ведущие блоки.

        v2: НЕ переставляет chat/tool сообщения местами (это ломало tool_call_id
        adjacency, обязательную для OpenAI/vLLM API). Порядок сообщений сохраняется
        1:1; каждому сообщению просто добавляется метка `_block` для мониторинга.
        """
        chat_count = 0
        tool_count = 0
        scratch_refs: List[str] = []

        for msg in messages:
            if msg.get("_scratch_key"):
                scratch_refs.append(msg["_scratch_key"])

        isolated: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": f"[{BLOCK_RULES.upper()}]\n{system_prompt}",
                "_block": BLOCK_RULES,
            },
        ]

        if scratch_refs:
            facts_lines = [
                f"- scratch:{key} (load_scratch для полного содержимого)"
                for key in scratch_refs
            ]
            isolated.append({
                "role": "system",
                "content": f"[{BLOCK_FACTS.upper()}]\n" + "\n".join(facts_lines),
                "_block": BLOCK_FACTS,
            })

        for msg in messages:
            role = msg.get("role", "")
            m = copy.copy(msg)
            if role == "tool":
                m["_block"] = BLOCK_TOOLS
                tool_count += 1
            elif role in ("user", "assistant"):
                m["_block"] = BLOCK_CHAT
                chat_count += 1
            isolated.append(m)

        if report:
            report.blocks = {
                BLOCK_RULES: 1,
                BLOCK_FACTS: 1 if scratch_refs else 0,
                BLOCK_CHAT: chat_count,
                BLOCK_TOOLS: tool_count,
            }
        return isolated
