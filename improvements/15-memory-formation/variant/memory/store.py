"""SQLite-хранилище фактов с изоляцией по user_id."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

from memory.models import MemoryFact
from memory.retriever import keyword_tokens, simple_embedding


class SqliteMemoryStore:
    """Файловое хранилище фактов, scoped по user_id."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_facts (
                  id TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL,
                  session_id TEXT NOT NULL DEFAULT '',
                  fact_key TEXT NOT NULL,
                  content TEXT NOT NULL,
                  fact_type TEXT NOT NULL,
                  keywords TEXT NOT NULL DEFAULT '',
                  embedding_json TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  expires_at REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_user ON memory_facts(user_id)"
            )
            # UNIQUE, а не просто индекс: (user_id, fact_key) — каноническая
            # запись факта. Инвариант уже подразумевался на уровне manager/
            # dedup.find_duplicate() (key-match всегда возвращает СУЩЕСТВУЮЩИЙ
            # факт для UPDATE, а не INSERT), но не был закреплён в БД — при
            # двух конкурентных процессах, оба не увидевших чужой ещё не
            # закоммиченный INSERT, получались бы два разных id с одинаковым
            # (user_id, fact_key). upsert_fact() полагается на этот constraint
            # через "INSERT ... ON CONFLICT DO UPDATE".
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_user_key_unique "
                "ON memory_facts(user_id, fact_key)"
            )

    def _row_to_fact(self, row: sqlite3.Row) -> MemoryFact:
        emb_raw = row["embedding_json"]
        embedding = json.loads(emb_raw) if emb_raw else None
        return MemoryFact(
            id=row["id"],
            user_id=row["user_id"],
            session_id=row["session_id"] or "",
            key=row["fact_key"],
            content=row["content"],
            fact_type=row["fact_type"],
            keywords=row["keywords"] or "",
            embedding=embedding,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            expires_at=float(row["expires_at"]) if row["expires_at"] is not None else None,
        )

    def list_facts(
        self,
        user_id: str,
        *,
        session_id: Optional[str] = None,
        include_expired: bool = False,
    ) -> list[MemoryFact]:
        """Все факты пользователя; session_id фильтрует только при явной передаче."""
        query = "SELECT * FROM memory_facts WHERE user_id = ?"
        params: list[object] = [user_id]
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        facts = [self._row_to_fact(r) for r in rows]
        if include_expired:
            return facts
        return [f for f in facts if not f.is_expired]

    def upsert_fact(
        self,
        *,
        user_id: str,
        session_id: str,
        key: str,
        content: str,
        fact_type: str,
        fact_id: Optional[str] = None,
        expires_at: Optional[float] = None,
    ) -> MemoryFact:
        """Создаёт или обновляет каноническую запись."""
        now = time.time()
        keywords = " ".join(sorted(keyword_tokens(f"{key} {content}")))
        embedding = simple_embedding(content)
        embedding_json = json.dumps(embedding)

        with self._connect() as conn:
            if fact_id:
                row = conn.execute(
                    "SELECT * FROM memory_facts WHERE id = ? AND user_id = ?",
                    (fact_id, user_id),
                ).fetchone()
                if row:
                    conn.execute(
                        """
                        UPDATE memory_facts
                        SET content = ?, fact_type = ?, session_id = ?, keywords = ?,
                            embedding_json = ?, updated_at = ?, expires_at = ?
                        WHERE id = ? AND user_id = ?
                        """,
                        (
                            content, fact_type, session_id, keywords, embedding_json,
                            now, expires_at, fact_id, user_id,
                        ),
                    )
                    conn.commit()
                    updated = conn.execute(
                        "SELECT * FROM memory_facts WHERE id = ?", (fact_id,)
                    ).fetchone()
                    return self._row_to_fact(updated)

            # ON CONFLICT DO UPDATE, не plain INSERT: caller (manager.py) уже
            # проверил дубликаты по своей копии `existing`, но между этой
            # проверкой и коммитом здесь конкурентный процесс мог вставить
            # свою запись с тем же (user_id, fact_key) первым — UNIQUE index
            # в _init_db() иначе привёл бы к IntegrityError вместо тихого
            # разрешения гонки через canonical UPDATE.
            new_id = fact_id or str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO memory_facts (
                  id, user_id, session_id, fact_key, content, fact_type,
                  keywords, embedding_json, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, fact_key) DO UPDATE SET
                  content = excluded.content,
                  fact_type = excluded.fact_type,
                  session_id = excluded.session_id,
                  keywords = excluded.keywords,
                  embedding_json = excluded.embedding_json,
                  updated_at = excluded.updated_at,
                  expires_at = excluded.expires_at
                """,
                (
                    new_id, user_id, session_id, key, content, fact_type,
                    keywords, embedding_json, now, now, expires_at,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM memory_facts WHERE user_id = ? AND fact_key = ?",
                (user_id, key),
            ).fetchone()
            return self._row_to_fact(row)

    def delete_expired(self, user_id: Optional[str] = None) -> int:
        """Удаляет истёкшие факты; возвращает число удалённых."""
        now = time.time()
        with self._connect() as conn:
            if user_id:
                cur = conn.execute(
                    "DELETE FROM memory_facts WHERE user_id = ? "
                    "AND expires_at IS NOT NULL AND expires_at < ?",
                    (user_id, now),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM memory_facts WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (now,),
                )
            conn.commit()
            return cur.rowcount

    def count_facts(self, user_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_facts WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return int(row["c"]) if row else 0
