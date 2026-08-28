"""Unit-тесты 2-уровневого дерева контекста (context_tree.py)."""
from __future__ import annotations

import asyncio

import pytest

from context_tree import (
    HierarchicalTreeConfig,
    TreeNode,
    build_segments,
    default_digest,
    extract_markers,
    is_segment_atomic,
    reduce_oldest,
    reduce_oldest_async,
    reduce_segments_to_node,
    render_tree_to_messages,
)


def _user(text: str):
    return {"role": "user", "content": text}


def _assistant_with_tool(call_id: str, name: str = "employee_lookup"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "name": name, "args": {}}],
    }


def _tool_result(call_id: str, content: str):
    return {"role": "tool", "content": content, "tool_call_id": call_id}


def _assistant_reply(text: str):
    return {"role": "assistant", "content": text}


def _make_turn(idx: int, marker: bool = False) -> list:
    """Строит один полный ход (user -> assistant+tool_call -> tool -> assistant)."""
    call_id = f"call_{idx}"
    content = f"EMP_ID=HR-{idx:04d} found employee_{idx}" if marker else f"lookup result {idx}"
    return [
        _user(f"Найди сотрудника employee_{idx}"),
        _assistant_with_tool(call_id),
        _tool_result(call_id, content),
        _assistant_reply(f"Готово, employee_{idx} найден."),
    ]


def _flatten(turns):
    out = []
    for t in turns:
        out.extend(t)
    return out


# ---------------------------------------------------------------------------
# build_segments / atomicity
# ---------------------------------------------------------------------------

def test_build_segments_groups_atomic_units():
    messages = _flatten([_make_turn(1), _make_turn(2)])
    segments = build_segments(messages)
    # Each turn = user segment + (assistant+tool+assistant reply) segment(s).
    # Ensure no segment splits an assistant tool_call from its tool result.
    for seg in segments:
        assert is_segment_atomic(seg)


def test_is_segment_atomic_detects_orphaned_tool_call():
    orphan = [_assistant_with_tool("call_x")]  # no matching tool result
    assert not is_segment_atomic(orphan)


def test_is_segment_atomic_detects_orphaned_tool_result():
    orphan = [_tool_result("call_x", "content")]  # tool msg without leading assistant
    assert not is_segment_atomic(orphan)


def test_build_segments_never_splits_tool_call_from_result():
    messages = _flatten([_make_turn(i) for i in range(1, 5)])
    segments = build_segments(messages)
    for seg in segments:
        has_call = seg[0].get("role") == "assistant" and seg[0].get("tool_calls")
        if has_call:
            ids = {tc["id"] for tc in seg[0]["tool_calls"]}
            got = {m.get("tool_call_id") for m in seg if m.get("role") == "tool"}
            assert ids.issubset(got), "tool_call without its matching tool result in same segment"


# ---------------------------------------------------------------------------
# marker extraction / preservation
# ---------------------------------------------------------------------------

def test_extract_markers_finds_verbatim_values():
    text = "Сотрудник найден, EMP_ID=HR-0042, баланс LEAVE_DAYS=12.5 дней."
    markers = extract_markers(text)
    assert "EMP_ID=HR-0042," == markers[0] or "EMP_ID=HR-0042" in markers[0]
    assert any("LEAVE_DAYS=12.5" in m for m in markers)


def test_extract_markers_empty_for_plain_text():
    assert extract_markers("просто обычный текст без маркеров") == []


def test_reduce_segments_to_node_preserves_all_markers_verbatim():
    turns = [_make_turn(i, marker=True) for i in range(1, 4)]
    segments = build_segments(_flatten(turns))
    node = reduce_segments_to_node(segments, start_index=0)
    assert node.kind == "summary"
    combined = "\n".join(m.get("content", "") for m in node.messages)
    for seg in segments:
        raw = "\n".join(m.get("content") or "" for m in seg)
        for marker in extract_markers(raw):
            assert marker in combined, f"marker {marker!r} lost in reduced summary"


def test_reduce_segments_to_node_keeps_traceability_pointer():
    turns = [_make_turn(i) for i in range(1, 3)]
    segments = build_segments(_flatten(turns))
    node = reduce_segments_to_node(segments, start_index=0)
    assert node.source_segment_range == (0, len(segments))
    assert any("SUMMARY_OF_SEGMENTS" in (m.get("content") or "") for m in node.messages)


# ---------------------------------------------------------------------------
# reduce_oldest threshold behavior
# ---------------------------------------------------------------------------

def test_reduce_oldest_noop_below_threshold():
    turns = [_make_turn(i) for i in range(1, 3)]
    segments = build_segments(_flatten(turns))
    assert len(segments) <= 6
    nodes = reduce_oldest(segments, threshold=6, k=3)
    assert all(n.kind == "leaf" for n in nodes)
    assert len(nodes) == len(segments)


def test_reduce_oldest_triggers_above_threshold():
    turns = [_make_turn(i, marker=True) for i in range(1, 6)]
    segments = build_segments(_flatten(turns))
    assert len(segments) > 6
    nodes = reduce_oldest(segments, threshold=6, k=3)
    assert nodes[0].kind == "summary"
    assert sum(1 for n in nodes if n.kind == "summary") == 1
    # Remaining nodes are leaves, and total leaf count shrank.
    leaf_count = sum(1 for n in nodes if n.kind == "leaf")
    assert leaf_count == len(segments) - 3


def test_reduce_oldest_never_reduces_last_segment():
    turns = [_make_turn(i) for i in range(1, 8)]
    segments = build_segments(_flatten(turns))
    # k larger than available minus 1 should be clamped.
    nodes = reduce_oldest(segments, threshold=1, k=len(segments))
    assert nodes[-1].kind == "leaf"
    assert nodes[-1].messages == segments[-1]


def test_reduce_oldest_preserves_markers_end_to_end():
    turns = [_make_turn(i, marker=True) for i in range(1, 10)]
    flat = _flatten(turns)
    segments = build_segments(flat)
    nodes = reduce_oldest(segments, threshold=6, k=4)
    rendered = render_tree_to_messages(nodes)
    rendered_text = "\n".join(m.get("content") or "" for m in rendered)

    all_original_markers = set()
    for seg in segments:
        raw = "\n".join(m.get("content") or "" for m in seg)
        all_original_markers.update(extract_markers(raw))

    for marker in all_original_markers:
        assert marker in rendered_text, f"marker {marker!r} lost after reduce_oldest"


def test_reduce_oldest_output_is_shorter():
    # Realistic-size tool payloads (short unit-test strings would make the
    # summary-node bookkeeping overhead outweigh the savings).
    filler = "x" * 300
    turns = []
    for i in range(1, 12):
        turn = _make_turn(i, marker=True)
        turn[2]["content"] = turn[2]["content"] + " " + filler
        turns.append(turn)
    segments = build_segments(_flatten(turns))
    before_chars = sum(len(m.get("content") or "") for seg in segments for m in seg)

    nodes = reduce_oldest(segments, threshold=6, k=6)
    rendered = render_tree_to_messages(nodes)
    after_chars = sum(len(m.get("content") or "") for m in rendered)

    assert after_chars < before_chars


# ---------------------------------------------------------------------------
# render_tree_to_messages / atomicity preserved after render
# ---------------------------------------------------------------------------

def test_render_tree_to_messages_preserves_atomicity_of_remaining_leaves():
    turns = [_make_turn(i) for i in range(1, 10)]
    segments = build_segments(_flatten(turns))
    nodes = reduce_oldest(segments, threshold=6, k=4)
    rendered = render_tree_to_messages(nodes)

    # Re-split rendered messages back into segments (skipping the synthetic
    # summary system message) and confirm no orphaned tool_call remains.
    non_summary = [m for m in rendered if not m.get("_summary_node")]
    for seg in build_segments(non_summary):
        assert is_segment_atomic(seg)


# ---------------------------------------------------------------------------
# async reduce (Tier-2 gate)
# ---------------------------------------------------------------------------

def test_reduce_oldest_async_skips_summarize_fn_below_threshold():
    calls = []

    async def fake_summarize(segs):
        calls.append(segs)
        return "should not be called"

    turns = [_make_turn(i) for i in range(1, 2)]
    segments = build_segments(_flatten(turns))

    nodes = asyncio.run(reduce_oldest_async(segments, threshold=6, k=3, summarize_fn=fake_summarize))
    assert calls == []  # Tier-1 gate: below threshold, Tier-2 LLM never invoked
    assert all(n.kind == "leaf" for n in nodes)


def test_reduce_oldest_async_invokes_summarize_fn_above_threshold():
    calls = []

    async def fake_summarize(segs):
        calls.append(segs)
        return "LLM digest text"

    turns = [_make_turn(i, marker=True) for i in range(1, 6)]
    segments = build_segments(_flatten(turns))
    assert len(segments) > 6

    nodes = asyncio.run(reduce_oldest_async(segments, threshold=6, k=3, summarize_fn=fake_summarize))
    assert len(calls) == 1
    summary = nodes[0]
    assert summary.kind == "summary"
    combined = "\n".join(m.get("content", "") for m in summary.messages)
    assert "LLM digest text" in combined


def test_reduce_oldest_async_falls_back_to_default_digest_without_summarize_fn():
    turns = [_make_turn(i, marker=True) for i in range(1, 6)]
    segments = build_segments(_flatten(turns))
    nodes = asyncio.run(reduce_oldest_async(segments, threshold=6, k=3, summarize_fn=None))
    assert nodes[0].kind == "summary"


# ---------------------------------------------------------------------------
# config parsing
# ---------------------------------------------------------------------------

def test_hierarchical_tree_config_from_config_defaults():
    cfg = HierarchicalTreeConfig.from_config({})
    assert cfg.enabled is True
    assert cfg.leaf_threshold == 6
    assert cfg.reduce_k == 3


def test_hierarchical_tree_config_from_config_overrides():
    cfg = HierarchicalTreeConfig.from_config({
        "hierarchical_context_tree": {"enabled": False, "leaf_threshold": 10, "reduce_k": 5}
    })
    assert cfg.enabled is False
    assert cfg.leaf_threshold == 10
    assert cfg.reduce_k == 5


def test_default_digest_is_deterministic_and_nonempty():
    turns = [_make_turn(1), _make_turn(2)]
    segments = build_segments(_flatten(turns))
    d1 = default_digest(segments)
    d2 = default_digest(segments)
    assert d1 == d2
    assert d1
