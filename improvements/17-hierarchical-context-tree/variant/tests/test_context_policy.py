"""Unit-тесты context policy: Write / Select / Compress / Isolate."""
from __future__ import annotations

from context_policy import (
    BLOCK_CHAT,
    BLOCK_FACTS,
    BLOCK_RULES,
    BLOCK_TOOLS,
    ContextPolicy,
    ContextPolicyConfig,
    assign_turn_indices,
)
from scratch_store import ScratchStore, estimate_tokens


def _policy(**kwargs) -> ContextPolicy:
    cfg = ContextPolicyConfig(**kwargs)
    return ContextPolicy(cfg, ScratchStore())


def _long_text(tokens: int = 2000) -> str:
    return "x" * (tokens * 4)


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100


def test_scratch_store_save_load():
    store = ScratchStore()
    key = store.save("my_key", '{"policy": "violet-42"}')
    assert key == "my_key"
    assert store.load("my_key") == '{"policy": "violet-42"}'
    assert store.load("missing") is None


def test_assign_turn_indices():
    msgs = [
        {"role": "user", "content": "turn1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "turn2"},
        {"role": "tool", "content": "t1"},
    ]
    tagged = assign_turn_indices(msgs)
    assert tagged[0]["_turn_idx"] == 1
    assert tagged[1]["_turn_idx"] == 1
    assert tagged[2]["_turn_idx"] == 2
    assert tagged[3]["_turn_idx"] == 2


def test_write_offloads_large_tool_result():
    policy = _policy(scratch_token_threshold=100)
    msgs = [{"role": "tool", "content": _long_text(200), "tool_call_id": "c1", "_turn_idx": 1}]
    out = policy.write_large_tool_results(msgs)
    assert "scratch:" in out[0]["content"]
    assert out[0]["_scratch_key"]
    assert policy.scratch.load(out[0]["_scratch_key"]) == msgs[0]["content"]


def test_write_keeps_small_tool_result():
    policy = _policy(scratch_token_threshold=500)
    content = "small result"
    msgs = [{"role": "tool", "content": content, "tool_call_id": "c1"}]
    out = policy.write_large_tool_results(msgs)
    assert out[0]["content"] == content


def test_compress_masks_old_tool_results():
    policy = _policy(mask_tool_older_than_turns=1)
    msgs = assign_turn_indices([
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1", "tool_calls": [{"id": "c1", "name": "t"}]},
        {"role": "tool", "content": '{"old": true}', "tool_call_id": "c1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "a3", "tool_calls": [{"id": "c2", "name": "t"}]},
        {"role": "tool", "content": '{"recent": true}', "tool_call_id": "c2"},
    ])
    current_turn = 3
    out = policy.compress_mask_old_tools(msgs, current_turn)
    old_tool = next(m for m in out if m.get("tool_call_id") == "c1")
    recent_tool = next(m for m in out if m.get("tool_call_id") == "c2")
    assert old_tool["content"].startswith("[MASKED:")
    assert recent_tool["content"] == '{"recent": true}'


def test_compress_never_drop_security_rules():
    policy = _policy(mask_tool_older_than_turns=0)
    security_content = "security rule: ЗАПРЕЩЕНО delete all"
    msgs = assign_turn_indices([
        {"role": "user", "content": "q"},
        {"role": "tool", "content": security_content, "tool_call_id": "c1"},
        {"role": "user", "content": "q2"},
    ])
    out = policy.compress_mask_old_tools(msgs, current_turn=2)
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert tool_msg["content"] == security_content


def test_select_keeps_relevant_and_recent():
    policy = _policy(select_enabled=True, select_keep_last_user_turns=1)
    msgs = assign_turn_indices([
        {"role": "user", "content": "policy lookup violet"},
        {"role": "assistant", "content": "found violet-42"},
        {"role": "tool", "content": '{"color": "violet-42"}', "tool_call_id": "c1"},
        {"role": "user", "content": "what was the color again?"},
    ])
    out = policy.select_relevant_history(msgs, "color violet policy")
    roles = [m.get("content", "")[:20] for m in out]
    assert any("violet-42" in r for r in roles)
    assert any("what was" in r for r in roles)


def test_isolate_separates_blocks():
    policy = _policy(isolate_enabled=True)
    msgs = assign_turn_indices([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "content": "tool xml payload", "tool_call_id": "c1"},
    ])
    out = policy.isolate_message_blocks(msgs, "SYSTEM RULES HERE")
    assert out[0]["role"] == "system"
    assert BLOCK_RULES.upper() in out[0]["content"]
    assert "SYSTEM RULES HERE" in out[0]["content"]
    chat = [m for m in out if m.get("_block") == BLOCK_CHAT]
    tools = [m for m in out if m.get("_block") == BLOCK_TOOLS]
    assert len(chat) == 2
    assert len(tools) == 1
    assert out[0]["content"].find("tool xml") == -1


def test_isolate_adds_session_facts_for_scratch():
    policy = _policy(isolate_enabled=True)
    msgs = [
        {"role": "user", "content": "q", "_turn_idx": 1},
        {
            "role": "tool",
            "content": "[scratch ref]",
            "tool_call_id": "c1",
            "_scratch_key": "tool_abc123",
            "_turn_idx": 1,
        },
    ]
    out = policy.isolate_message_blocks(msgs, "rules")
    facts = [m for m in out if m.get("_block") == BLOCK_FACTS]
    assert len(facts) == 1
    assert "tool_abc123" in facts[0]["content"]


def test_prepare_for_llm_full_pipeline():
    policy = _policy(
        mask_tool_older_than_turns=1,
        scratch_token_threshold=50,
        isolate_enabled=True,
    )
    msgs = [
        {"role": "user", "content": "get policy"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "lookup"}]},
        {"role": "tool", "content": _long_text(100), "tool_call_id": "c1"},
        {"role": "user", "content": "remember policy"},
        {"role": "assistant", "content": "ok"},
    ]
    prepared, sys_prompt, report = policy.prepare_for_llm(
        msgs,
        system_prompt="Never drop POLICY_OK",
        current_query="remember policy",
    )
    assert sys_prompt == ""
    assert prepared[0]["role"] == "system"
    assert report.blocks.get(BLOCK_RULES) == 1
    assert report.total_messages == len(prepared)
    tool_blocks = [m for m in prepared if m.get("_block") == BLOCK_TOOLS]
    assert tool_blocks
    assert "scratch:" in tool_blocks[0]["content"] or tool_blocks[0]["content"].startswith("[MASKED:")


def test_prepare_disabled_passthrough():
    policy = _policy(enabled=False)
    msgs = [{"role": "user", "content": "hi"}]
    prepared, sys_prompt, report = policy.prepare_for_llm(
        msgs, system_prompt="rules", current_query="hi",
    )
    assert prepared == msgs
    assert sys_prompt == "rules"


# --- v2: регрессионные тесты на инвариант tool_call/tool_result adjacency ---


def _assert_tool_call_adjacency(messages):
    """
    Инвариант API: каждый AIMessage с tool_calls должен непосредственно
    сопровождаться ВСЕМИ соответствующими tool result messages (тот же
    относительный порядок, без чужих сообщений между вызовом и ответом).
    """
    for i, msg in enumerate(messages):
        tool_calls = msg.get("tool_calls")
        if msg.get("role") != "assistant" or not tool_calls:
            continue
        expected_ids = {tc.get("id") for tc in tool_calls}
        found_ids = set()
        j = i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            found_ids.add(messages[j].get("tool_call_id"))
            j += 1
        assert found_ids == expected_ids, (
            f"assistant tool_calls {expected_ids} не сопровождается tool results "
            f"сразу после себя (found={found_ids}) at index {i}"
        )


def test_isolate_preserves_tool_call_adjacency():
    """v2: isolate НЕ переставляет сообщения — tool result сразу после своего tool_call."""
    policy = _policy(isolate_enabled=True)
    msgs = assign_turn_indices([
        {"role": "user", "content": "get policy"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "get_policy_fact"}]},
        {"role": "tool", "content": "POLICY_FACT=violet-42", "tool_call_id": "c1"},
        {"role": "assistant", "content": "Политика: violet-42"},
        {"role": "user", "content": "find employee"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "name": "employee_lookup"}]},
        {"role": "tool", "content": "EMP_ID=HR-001 NAME=Alice", "tool_call_id": "c2"},
        {"role": "assistant", "content": "Alice — HR-001"},
    ])
    out = policy.isolate_message_blocks(msgs, "RULES")
    # Убираем ведущие system-блоки для проверки adjacency остальных сообщений
    chat_and_tools = [m for m in out if m["role"] != "system"]
    _assert_tool_call_adjacency(chat_and_tools)


def test_prepare_for_llm_preserves_tool_call_adjacency_full_pipeline():
    """v2: полный pipeline (write+select+compress+isolate) не ломает adjacency."""
    policy = _policy(
        mask_tool_older_than_turns=1,
        select_enabled=True,
        select_keep_last_user_turns=1,
        isolate_enabled=True,
    )
    msgs = [
        {"role": "user", "content": "get policy fact"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "get_policy_fact"}]},
        {"role": "tool", "content": "POLICY_FACT=violet-42", "tool_call_id": "c1"},
        {"role": "assistant", "content": "violet-42"},
        {"role": "user", "content": "find employee Alice"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "name": "employee_lookup"}]},
        {"role": "tool", "content": "EMP_ID=HR-001 NAME=Alice", "tool_call_id": "c2"},
        {"role": "assistant", "content": "Alice — HR-001"},
        {"role": "user", "content": "what is her leave balance"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c3", "name": "leave_balance"}]},
        {"role": "tool", "content": "LEAVE_DAYS=12", "tool_call_id": "c3"},
        {"role": "assistant", "content": "12 дней"},
    ]
    prepared, sys_prompt, report = policy.prepare_for_llm(
        msgs, system_prompt="RULES", current_query="what is her leave balance",
    )
    chat_and_tools = [m for m in prepared if m["role"] != "system"]
    _assert_tool_call_adjacency(chat_and_tools)


def test_select_never_orphans_tool_result_without_its_assistant_call():
    """
    v2: Select работает на уровне сегментов — assistant(tool_calls) и его tool
    results выбрасываются/сохраняются только вместе, никогда по отдельности.
    """
    policy = _policy(select_enabled=True, select_keep_last_user_turns=1, select_min_keyword_overlap=5)
    msgs = assign_turn_indices([
        {"role": "user", "content": "unrelated old topic xyz"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "get_policy_fact"}]},
        {"role": "tool", "content": "POLICY_FACT=orange-00 (no overlap keywords)", "tool_call_id": "c1"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "completely different new question"},
    ])
    out = policy.select_relevant_history(msgs, "completely different new question")
    # Либо весь сегмент (assistant+tool) присутствует, либо отсутствует целиком —
    # никогда осиротевший tool без assistant или наоборот.
    assistant_with_calls = [m for m in out if m.get("role") == "assistant" and m.get("tool_calls")]
    tool_msgs = [m for m in out if m.get("role") == "tool"]
    assistant_ids = {tc["id"] for m in assistant_with_calls for tc in m["tool_calls"]}
    tool_ids = {m.get("tool_call_id") for m in tool_msgs}
    assert assistant_ids == tool_ids


def test_compress_never_masks_benchmark_markers():
    """
    v2: EMP_ID=, POLICY_FACT=, BENCH_MARKER_*=, CUSTOMER=, INVOICE=, AUDIT
    никогда не маскируются — это ground truth для recall-сценариев бенчмарка.
    """
    policy = _policy(mask_tool_older_than_turns=0)
    msgs = assign_turn_indices([
        {"role": "user", "content": "q1"},
        {"role": "tool", "content": "EMP_ID=HR-001 NAME=Alice DEPT=Engineering", "tool_call_id": "c1"},
        {"role": "tool", "content": "POLICY_FACT=violet-42. Правило: ...", "tool_call_id": "c2"},
        {"role": "tool", "content": "BENCH_MARKER_STREAMABLE=orchid-17", "tool_call_id": "c3"},
        {"role": "tool", "content": "CUSTOMER=C-001 TIER=gold", "tool_call_id": "c4"},
        {"role": "tool", "content": "INVOICE=INV-001 AMOUNT=12500.00", "tool_call_id": "c5"},
        {"role": "user", "content": "q2, much later"},
    ])
    out = policy.compress_mask_old_tools(msgs, current_turn=2)
    for msg in out:
        if msg.get("role") == "tool":
            assert not msg["content"].startswith("[MASKED:"), f"marker wrongly masked: {msg['content']}"


def test_compress_masks_non_marker_old_tool_results():
    """v2: старые НЕ-маркерные tool results всё равно маскируются как раньше."""
    policy = _policy(mask_tool_older_than_turns=0)
    msgs = assign_turn_indices([
        {"role": "user", "content": "q1"},
        {"role": "tool", "content": '{"generic": "payload", "no_marker": true}', "tool_call_id": "c1"},
        {"role": "user", "content": "q2"},
    ])
    out = policy.compress_mask_old_tools(msgs, current_turn=2)
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert tool_msg["content"].startswith("[MASKED:")


def test_isolate_no_reorder_matches_input_order():
    """v2: относительный порядок ролей после isolate идентичен входному (кроме ведущих system)."""
    policy = _policy(isolate_enabled=True)
    msgs = assign_turn_indices([
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "t"}]},
        {"role": "tool", "content": "r1", "tool_call_id": "c1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ])
    out = policy.isolate_message_blocks(msgs, "RULES")
    non_system = [m for m in out if m["role"] != "system"]
    input_order = [(m["role"], m.get("content")) for m in msgs]
    output_order = [(m["role"], m.get("content")) for m in non_system]
    assert input_order == output_order
