"""Фабрика графа: memory (default), snapshot JSON, optional Neo4j."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from graph_rag import KnowledgeGraph, build_demo_graph

logger = logging.getLogger("graph_store")


def graph_from_snapshot(path: Path) -> KnowledgeGraph:
    """Загрузка графа из nightly ETL snapshot (JSON)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    g = KnowledgeGraph()
    for node in raw.get("nodes") or []:
        node_id = node.pop("id")
        g.add_node(node_id, **node)
    for edge in raw.get("edges") or []:
        g.add_edge(edge["from"], edge["to"], edge["relation"])
    return g


def graph_from_neo4j(uri: str, user: str, password: str, database: str = "neo4j") -> KnowledgeGraph:
    """
    Минимальный read-only импорт из Neo4j.
    Требует: pip install neo4j
  Cypher: MATCH (n) OPTIONAL MATCH (n)-[r]->(m) ...
    """
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError("neo4j package not installed") from exc

    g = KnowledgeGraph()
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            nodes = session.run(
                "MATCH (n) RETURN id(n) AS internal_id, labels(n) AS labels, properties(n) AS props"
            )
            id_map: Dict[int, str] = {}
            for rec in nodes:
                props = dict(rec["props"] or {})
                node_type = (rec["labels"] or ["node"])[0].lower()
                node_id = props.get("id") or f"{node_type}:{rec['internal_id']}"
                id_map[rec["internal_id"]] = node_id
                g.add_node(node_id, type=node_type, **props)

            rels = session.run(
                "MATCH (a)-[r]->(b) RETURN id(a) AS a, id(b) AS b, type(r) AS rel"
            )
            for rec in rels:
                src = id_map.get(rec["a"])
                dst = id_map.get(rec["b"])
                if src and dst:
                    g.add_edge(src, dst, rec["rel"])
    finally:
        driver.close()
    return g


def load_knowledge_graph(config: Dict[str, Any]) -> KnowledgeGraph:
    """Выбор backend по конфигу с fallback на memory/demo."""
    cfg = config.get("graph_rag") or {}
    backend = (cfg.get("backend") or "memory").lower()

    snapshot = cfg.get("snapshot_path")
    if snapshot:
        path = Path(snapshot)
        if path.is_file():
            logger.info("Graph-RAG: loading snapshot %s", path)
            return graph_from_snapshot(path)

    if backend == "neo4j":
        uri = os.environ.get("NEO4J_URI") or cfg.get("neo4j_uri")
        if uri:
            user = os.environ.get("NEO4J_USER") or cfg.get("neo4j_user") or "neo4j"
            password = os.environ.get("NEO4J_PASSWORD") or cfg.get("neo4j_password") or ""
            database = cfg.get("neo4j_database") or "neo4j"
            try:
                logger.info("Graph-RAG: loading from Neo4j %s", uri)
                return graph_from_neo4j(uri, user, password, database)
            except Exception as exc:
                logger.warning("Neo4j unavailable (%s), fallback to demo graph", exc)

    logger.info("Graph-RAG: using in-memory demo graph")
    return build_demo_graph()
