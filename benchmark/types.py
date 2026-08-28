"""Общие типы бенчмарка (без зависимости от варианта агента)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class TurnResult:
    answer: str
    messages: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[str] = field(default_factory=list)
    tool_call_details: List[Dict[str, Any]] = field(default_factory=list)
    rounds: int = 0
    latency_sec: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
