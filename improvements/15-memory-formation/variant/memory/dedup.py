"""Дедупликация: похожий факт → UPDATE, не ADD."""
from __future__ import annotations

import re
from typing import Optional

from memory.models import ExtractedFact, MemoryFact
from memory.retriever import keyword_tokens, token_overlap_score


def normalize_key(key: str) -> str:
    return re.sub(r"\s+", "_", key.strip().lower())


def find_duplicate(
    candidate: ExtractedFact,
    existing: list[MemoryFact],
    *,
    key_match: bool = True,
    similarity_threshold: float = 0.55,
) -> Optional[MemoryFact]:
    """
    Ищет существующий факт для обновления.

    Приоритет: точное совпадение key → семантическая близость content.
    """
    norm_key = normalize_key(candidate.key)
    for fact in existing:
        if key_match and normalize_key(fact.key) == norm_key:
            return fact

    cand_tokens = keyword_tokens(candidate.content)
    best: Optional[MemoryFact] = None
    best_score = 0.0
    for fact in existing:
        if fact.fact_type != candidate.fact_type:
            continue
        score = token_overlap_score(cand_tokens, keyword_tokens(fact.content))
        if score > best_score:
            best_score = score
            best = fact

    if best_score >= similarity_threshold:
        return best
    return None
