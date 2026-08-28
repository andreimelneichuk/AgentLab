"""Оркестратор memory formation: extract → dedup → store → retrieve."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from memory.dedup import find_duplicate
from memory.extractor import FactExtractor
from memory.models import ExtractedFact, MemoryFact
from memory.policy import ttl_for_type
from memory.retriever import retrieve_top_k
from memory.store import SqliteMemoryStore

MEMORY_CONTEXT_HEADER = "## Память пользователя (из прошлых сессий)\n"


class MemoryManager:
    """Управляет долговременной памятью между сессиями."""

    def __init__(
        self,
        store: SqliteMemoryStore,
        *,
        extractor: Optional[FactExtractor] = None,
        top_k: int = 5,
        similarity_threshold: float = 0.55,
    ):
        self.store = store
        self.extractor = extractor or FactExtractor(use_llm=False)
        self.top_k = top_k
        self.similarity_threshold = similarity_threshold

    @classmethod
    def from_config(cls, config: Dict[str, Any], base_dir: Optional[Path] = None) -> "MemoryManager":
        mem_cfg = config.get("memory") or {}
        if not mem_cfg.get("enabled", True):
            raise ValueError("memory disabled in config")

        root = base_dir or Path(".")
        db_rel = mem_cfg.get("store_path", "data/memory.db")
        store = SqliteMemoryStore(root / db_rel)
        extractor = FactExtractor(use_llm=bool(mem_cfg.get("use_llm_extractor", False)))
        return cls(
            store,
            extractor=extractor,
            top_k=int(mem_cfg.get("top_k", 5)),
            similarity_threshold=float(mem_cfg.get("similarity_threshold", 0.55)),
        )

    def _expires_at(self, fact: ExtractedFact) -> Optional[float]:
        ttl = ttl_for_type(fact.fact_type, fact.ttl_seconds)
        if ttl is None:
            return None
        return time.time() + ttl

    def ingest_messages(
        self,
        *,
        user_id: str,
        session_id: str,
        messages: List[Dict[str, Any]],
        extracted: Optional[List[ExtractedFact]] = None,
    ) -> list[MemoryFact]:
        """Извлекает факты из диалога и сохраняет с dedup UPDATE."""
        self.store.delete_expired(user_id)
        candidates = extracted
        if candidates is None:
            candidates = self.extractor.extract_from_messages_sync(messages)

        existing = self.store.list_facts(user_id)
        saved: list[MemoryFact] = []

        for candidate in candidates:
            duplicate = find_duplicate(
                candidate,
                existing,
                similarity_threshold=self.similarity_threshold,
            )
            expires_at = self._expires_at(candidate)
            if duplicate:
                fact = self.store.upsert_fact(
                    user_id=user_id,
                    session_id=session_id,
                    key=candidate.key,
                    content=candidate.content,
                    fact_type=candidate.fact_type,
                    fact_id=duplicate.id,
                    expires_at=expires_at,
                )
                for i, e in enumerate(existing):
                    if e.id == duplicate.id:
                        existing[i] = fact
                        break
            else:
                fact = self.store.upsert_fact(
                    user_id=user_id,
                    session_id=session_id,
                    key=candidate.key,
                    content=candidate.content,
                    fact_type=candidate.fact_type,
                    expires_at=expires_at,
                )
                existing.append(fact)
            saved.append(fact)
        return saved

    def retrieve_for_query(self, *, user_id: str, query: str) -> list[MemoryFact]:
        """Семантический top-k recall для текущего запроса."""
        self.store.delete_expired(user_id)
        facts = self.store.list_facts(user_id)
        return retrieve_top_k(query, facts, k=self.top_k)

    def format_context_block(self, facts: list[MemoryFact]) -> str:
        if not facts:
            return ""
        lines = [f.to_context_line() for f in facts]
        return MEMORY_CONTEXT_HEADER + "\n".join(lines) + "\n"

    def build_augmented_system_prompt(
        self,
        base_prompt: str,
        *,
        user_id: str,
        query: str,
    ) -> str:
        """Инжектирует релевантные факты в system context."""
        facts = self.retrieve_for_query(user_id=user_id, query=query)
        block = self.format_context_block(facts)
        if not block:
            return base_prompt
        return f"{base_prompt.rstrip()}\n\n{block.rstrip()}\n"
