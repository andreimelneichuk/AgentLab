"""Семантический (keyword) поиск релевантных фактов."""
from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable, Optional, Set

from memory.models import MemoryFact

_TOKEN_RE = re.compile(r"[a-zа-яё0-9_]+", re.I)
_STOP_WORDS = frozenset({
    "и", "в", "на", "что", "как", "для", "по", "the", "a", "an", "is", "to", "of",
    "мне", "мой", "моя", "это", "или", "не", "да", "нет", "при", "из", "у", "о",
})


def keyword_tokens(text: str) -> Set[str]:
    tokens = {t.lower() for t in _TOKEN_RE.findall(text or "")}
    return {t for t in tokens if len(t) > 1 and t not in _STOP_WORDS}


def token_overlap_score(query_tokens: Set[str], doc_tokens: Set[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    intersection = query_tokens & doc_tokens
    if intersection:
        return len(intersection) / math.sqrt(len(query_tokens) * len(doc_tokens))
    # Частичное совпадение (кратко ↔ краткие)
    partial = 0
    for qt in query_tokens:
        for dt in doc_tokens:
            if len(qt) >= 4 and len(dt) >= 4 and (qt.startswith(dt[:4]) or dt.startswith(qt[:4])):
                partial += 1
                break
    if partial:
        return partial / math.sqrt(len(query_tokens) * len(doc_tokens))
    return 0.0


def _stable_token_hash(token: str) -> int:
    """Детерминированный хеш токена (не зависит от PYTHONHASHSEED процесса).

    Python's builtin hash() для str рандомизирован per-process — эмбеддинг,
    сохранённый в SQLite одним процессом, после рестарта сравнивался бы с
    эмбеддингом того же текста от НОВОГО процесса с другим hash seed, давая
    некоррелированные индексы bucket'ов и бессмысленный cosine score.
    """
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def simple_embedding(text: str, dim: int = 64) -> list[float]:
    """Лёгкий bag-of-hashes embedding без внешних зависимостей."""
    vec = [0.0] * dim
    for token in keyword_tokens(text):
        idx = _stable_token_hash(token) % dim
        vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine_similarity(a: Iterable[float], b: Iterable[float]) -> float:
    va, vb = list(a), list(b)
    if len(va) != len(vb) or not va:
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va)) or 1.0
    nb = math.sqrt(sum(x * x for x in vb)) or 1.0
    return dot / (na * nb)


def score_fact(query: str, fact: MemoryFact, *, use_embedding: bool = True) -> float:
    q_tokens = keyword_tokens(query)
    content_tokens = keyword_tokens(fact.content)
    key_tokens = keyword_tokens(fact.key.replace("_", " "))
    overlap = max(
        token_overlap_score(q_tokens, content_tokens),
        token_overlap_score(q_tokens, key_tokens) * 1.1,
    )
    if use_embedding and fact.embedding:
        emb_score = cosine_similarity(simple_embedding(query), fact.embedding)
        return max(overlap, emb_score)
    return overlap


def retrieve_top_k(
    query: str,
    facts: list[MemoryFact],
    *,
    k: int = 5,
    min_score: float = 0.05,
    use_embedding: bool = True,
) -> list[MemoryFact]:
    """Возвращает top-k релевантных неистёкших фактов."""
    scored: list[tuple[float, MemoryFact]] = []
    for fact in facts:
        if fact.is_expired:
            continue
        s = score_fact(query, fact, use_embedding=use_embedding)
        if s >= min_score:
            scored.append((s, fact))
    scored.sort(key=lambda x: (-x[0], -x[1].updated_at))
    return [f for _, f in scored[:k]]
