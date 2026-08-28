"""Unit-тесты memory formation: extract, dedup, store, multi-turn recall."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from memory import MemoryManager, SqliteMemoryStore
from memory.dedup import find_duplicate
from memory.extractor import FactExtractor, _extract_by_rules
from memory.models import ExtractedFact, MemoryFact
from memory.policy import should_never_store
from memory.retriever import _stable_token_hash, retrieve_top_k, simple_embedding

VARIANT_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def tmp_store(tmp_path: Path) -> SqliteMemoryStore:
    return SqliteMemoryStore(tmp_path / "test_memory.db")


@pytest.fixture
def manager(tmp_store: SqliteMemoryStore) -> MemoryManager:
    return MemoryManager(tmp_store, top_k=3)


def test_extractor_preference_from_dialog():
    extractor = FactExtractor()
    messages = [
        {"role": "user", "content": "Я предпочитаю Python для примеров кода."},
    ]
    facts = extractor.extract_from_messages_sync(messages)
    assert len(facts) >= 1
    assert facts[0].fact_type == "preference"
    assert "Python" in facts[0].content


def test_extractor_skips_tool_errors():
    extractor = FactExtractor()
    messages = [
        {"role": "tool", "content": "Error executing get_policy_fact: timeout"},
        {"role": "user", "content": "Повтори запрос"},
    ]
    facts = extractor.extract_from_messages_sync(messages)
    assert facts == []


def test_never_store_tool_xml():
    assert should_never_store('<tool_call name="get_policy_fact">')
    assert should_never_store("Error: unknown tool 'foo'")


def test_dedup_updates_same_key(manager: MemoryManager):
    user_id = "user_a"
    session_1 = "sess_1"
    msgs_1 = [{"role": "user", "content": "Я предпочитаю Python"}]
    saved_1 = manager.ingest_messages(user_id=user_id, session_id=session_1, messages=msgs_1)
    assert len(saved_1) == 1
    assert manager.store.count_facts(user_id) == 1

    session_2 = "sess_2"
    msgs_2 = [{"role": "user", "content": "Используй Python, не JavaScript"}]
    saved_2 = manager.ingest_messages(user_id=user_id, session_id=session_2, messages=msgs_2)

    assert manager.store.count_facts(user_id) == 1
    assert saved_2[0].id == saved_1[0].id
    assert "JavaScript" in saved_2[0].content or "Python" in saved_2[0].content


def test_user_isolation(manager: MemoryManager):
    manager.ingest_messages(
        user_id="alice",
        session_id="s1",
        messages=[{"role": "user", "content": "employee_id=ALICE-42"}],
    )
    manager.ingest_messages(
        user_id="bob",
        session_id="s1",
        messages=[{"role": "user", "content": "employee_id=BOB-99"}],
    )

    alice_facts = manager.store.list_facts("alice")
    bob_facts = manager.store.list_facts("bob")
    assert len(alice_facts) == 1
    assert len(bob_facts) == 1
    assert "ALICE" in alice_facts[0].content
    assert "BOB" in bob_facts[0].content


def test_ttl_expiration(tmp_store: SqliteMemoryStore):
    user_id = "ttl_user"
    past = time.time() - 10
    tmp_store.upsert_fact(
        user_id=user_id,
        session_id="s1",
        key="policy_snapshot",
        content="policy_code=OLD",
        fact_type="policy",
        expires_at=past,
    )
    facts = tmp_store.list_facts(user_id, include_expired=False)
    assert facts == []
    deleted = tmp_store.delete_expired(user_id)
    assert deleted == 1


def test_upsert_fact_enforces_unique_key_at_db_level(tmp_store: SqliteMemoryStore):
    """
    Regression: (user_id, fact_key) — теперь UNIQUE index в SQLite, а не только
    инвариант на уровне manager.find_duplicate(). Два upsert_fact() без
    fact_id для одного (user_id, fact_key) имитируют race между двумя
    процессами, оба не увидевшими чужой insert до дедупа на уровне manager —
    должны разрешиться в ОДНУ каноническую запись (ON CONFLICT DO UPDATE),
    а не бросить IntegrityError и не породить два разных id.
    """
    user_id = "race_user"
    first = tmp_store.upsert_fact(
        user_id=user_id, session_id="s1", key="employee_id",
        content="employee_id=EMP-1", fact_type="entity_id",
    )
    second = tmp_store.upsert_fact(
        user_id=user_id, session_id="s2", key="employee_id",
        content="employee_id=EMP-2", fact_type="entity_id",
    )
    assert second.id == first.id
    assert tmp_store.count_facts(user_id) == 1
    facts = tmp_store.list_facts(user_id)
    assert len(facts) == 1
    assert "EMP-2" in facts[0].content


def test_retrieve_relevant_facts(manager: MemoryManager):
    user_id = "recall_user"
    manager.ingest_messages(
        user_id=user_id,
        session_id="s1",
        messages=[{"role": "user", "content": "Я предпочитаю Python"}],
    )
    manager.ingest_messages(
        user_id=user_id,
        session_id="s1",
        messages=[{"role": "user", "content": "employee_id=EMP-1001"}],
    )

    hits = manager.retrieve_for_query(user_id=user_id, query="Python язык программирования")
    assert len(hits) >= 1
    assert any("Python" in f.content for f in hits)


def test_multi_turn_recall_across_sessions(manager: MemoryManager):
    """Сессия 2 вспоминает employee_id из сессии 1 без повторного извлечения из диалога."""
    user_id = "multi_turn"
    session_1 = "day_1"

    manager.ingest_messages(
        user_id=user_id,
        session_id=session_1,
        messages=[
            {"role": "user", "content": "Запомни: мой employee_id EMP-7777"},
            {"role": "assistant", "content": "Записал employee_id."},
        ],
    )

    session_2 = "day_2"
    recalled = manager.retrieve_for_query(
        user_id=user_id,
        query="Какой у меня employee_id?",
    )
    assert len(recalled) >= 1
    assert any("EMP-7777" in f.content for f in recalled)

    block = manager.format_context_block(recalled)
    assert "EMP-7777" in block
    assert "## Память пользователя" in block


def test_inject_context_into_system_prompt(manager: MemoryManager):
    user_id = "inject_user"
    manager.ingest_messages(
        user_id=user_id,
        session_id="s1",
        messages=[{"role": "user", "content": "Я предпочитаю краткие ответы"}],
    )
    augmented = manager.build_augmented_system_prompt(
        "Base system prompt.",
        user_id=user_id,
        query="Ответь кратко",
    )
    assert "Base system prompt." in augmented
    assert "Память пользователя" in augmented


def test_find_duplicate_by_similarity():
    existing = [
        MemoryFact(
            id="1",
            user_id="u",
            key="preference_language",
            content="Пользователь предпочитает язык: Python",
            fact_type="preference",
            created_at=0.0,
            updated_at=0.0,
        )
    ]
    candidate = ExtractedFact(
        key="preference_language",
        content="Пользователь предпочитает Python, не JavaScript",
        fact_type="preference",
    )
    dup = find_duplicate(candidate, existing)
    assert dup is not None
    assert dup.id == "1"


def test_entity_id_extraction():
    extractor = FactExtractor()
    facts = extractor.extract_from_messages_sync(
        [{"role": "user", "content": "policy_code=VAC-2024"}],
    )
    assert len(facts) == 1
    assert facts[0].fact_type == "entity_id"
    assert "VAC-2024" in facts[0].content


def test_extractor_preserves_distinct_preferences_sharing_key_template():
    """
    Regression: до фикса три разных regex-паттерна (язык программирования,
    "используй X не Y", "отвечай на X") делили один key_template
    "preference_language" — seen_keys внутри _extract_by_rules() дедупил
    ПЕРВОЕ совпадение и молча терял остальные, даже если это были два разных
    факта. Подтверждено на реальном каталоге: benchmark/scenarios/catalog/
    s11_drift_hr.yaml::s11_drift_hr_001 (3 "используй X не Y" + 1 "отвечай
    из") терял все, кроме первого совпадения.
    """
    text = "Я предпочитаю python, не java. Отвечай на английском."
    facts = _extract_by_rules(text)
    keys = {f.key for f in facts}
    assert len(facts) == 2
    assert "preference_language" in keys
    assert "preference_response_language" in keys


def test_extractor_tool_choice_preference_has_own_key():
    """v2: "используй X, не Y" — отдельный key_template, не путается с языком ответа."""
    facts = _extract_by_rules("Используй employee_lookup, не decoy_employee_search.")
    assert len(facts) == 1
    assert facts[0].key == "preference_tool_choice"
    assert "employee_lookup" in facts[0].content
    assert "decoy_employee_search" in facts[0].content


def test_simple_embedding_deterministic_across_calls():
    """
    Regression: simple_embedding() использовал builtin hash(token), рандо-
    мизированный per-process (PYTHONHASHSEED). Эмбеддинг факта, сохранённый
    одним процессом, после рестарта сравнивался бы с эмбеддингом того же
    запроса от нового процесса с другим hash seed — некоррелированные
    bucket-индексы, бессмысленный cosine score. _stable_token_hash() должен
    быть детерминирован (использует hashlib, не builtin hash()).
    """
    assert _stable_token_hash("python") == _stable_token_hash("python")
    vec1 = simple_embedding("Пользователь предпочитает Python")
    vec2 = simple_embedding("Пользователь предпочитает Python")
    assert vec1 == vec2


def test_retrieve_top_k_ranking():
    facts = [
        MemoryFact(
            id="a", user_id="u", key="k1",
            content="Пользователь предпочитает Python",
            fact_type="preference", created_at=1.0, updated_at=1.0,
        ),
        MemoryFact(
            id="b", user_id="u", key="k2",
            content="Любит котиков",
            fact_type="preference", created_at=2.0, updated_at=2.0,
        ),
    ]
    top = retrieve_top_k("код на Python", facts, k=1)
    assert len(top) == 1
    assert "Python" in top[0].content
