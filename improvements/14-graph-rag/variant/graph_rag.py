"""In-memory Graph-RAG для HR/policy связей (POC без Neo4j)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

Edge = Tuple[str, str, str]  # (from_id, to_id, relation)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


@dataclass
class KnowledgeGraph:
    """Dict-based граф знаний: узлы + рёбра с типами связей."""

    nodes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    edges: List[Edge] = field(default_factory=list)

    def add_node(self, node_id: str, **attrs: Any) -> None:
        self.nodes[node_id] = {"id": node_id, **attrs}

    def add_edge(self, from_id: str, to_id: str, relation: str) -> None:
        self.edges.append((from_id, to_id, relation))

    def neighbors(
        self,
        node_id: str,
        relation: Optional[str] = None,
        direction: str = "out",
    ) -> List[str]:
        """Соседи узла по исходящим/входящим рёбрам."""
        result: List[str] = []
        if direction in ("out", "both"):
            for src, dst, rel in self.edges:
                if src == node_id and (relation is None or rel == relation):
                    result.append(dst)
        if direction in ("in", "both"):
            for src, dst, rel in self.edges:
                if dst == node_id and (relation is None or rel == relation):
                    result.append(src)
        return result

    def find_nodes(self, node_type: str, **filters: Any) -> List[str]:
        """Поиск узлов по типу и атрибутам (case-insensitive для строк)."""
        matches: List[str] = []
        for node_id, data in self.nodes.items():
            if data.get("type") != node_type:
                continue
            ok = True
            for key, expected in filters.items():
                actual = data.get(key)
                if actual is None:
                    ok = False
                    break
                if isinstance(actual, str) and isinstance(expected, str):
                    if _norm(actual) != _norm(expected):
                        ok = False
                        break
                elif actual != expected:
                    ok = False
                    break
            if ok:
                matches.append(node_id)
        return matches

    def traverse(
        self,
        start_ids: Iterable[str],
        path: List[Tuple[str, str]],
    ) -> Set[str]:
        """
        Обход графа по цепочке (relation, target_node_type).

        path: [("BELONGS_TO", "department"), ("COVERED_BY", "policy"), ...]
        """
        current = set(start_ids)
        for relation, target_type in path:
            nxt: Set[str] = set()
            for node_id in current:
                for neighbor in self.neighbors(node_id, relation=relation, direction="both"):
                    if self.nodes.get(neighbor, {}).get("type") == target_type:
                        nxt.add(neighbor)
            current = nxt
            if not current:
                break
        return current

    def employee_ids_in_department(self, department: str) -> Set[str]:
        dept_ids = self.find_nodes("department", name=department)
        if not dept_ids:
            return set()
        employees: Set[str] = set()
        for dept_id in dept_ids:
            for emp_id in self.neighbors(dept_id, relation="BELONGS_TO", direction="in"):
                if self.nodes.get(emp_id, {}).get("type") == "employee":
                    employees.add(emp_id)
        return employees

    def employees_with_active_policy(
        self,
        employee_ids: Iterable[str],
        policy_name: str,
    ) -> Set[str]:
        policy_ids = {
            pid
            for pid in self.find_nodes("policy", name=policy_name)
            if self.nodes[pid].get("status") == "active"
        }
        if not policy_ids:
            return set()

        matched: Set[str] = set()
        for emp_id in employee_ids:
            covered = set(self.neighbors(emp_id, relation="COVERED_BY"))
            if covered & policy_ids:
                matched.add(emp_id)
        return matched

    def serialize_employee(self, emp_id: str) -> Dict[str, Any]:
        data = dict(self.nodes[emp_id])
        dept_names: List[str] = []
        for dept_id in self.neighbors(emp_id, relation="BELONGS_TO"):
            dept_names.append(self.nodes[dept_id].get("name", dept_id))
        policy_names: List[str] = []
        for pol_id in self.neighbors(emp_id, relation="COVERED_BY"):
            pol = self.nodes[pol_id]
            if pol.get("status") == "active":
                policy_names.append(pol.get("name", pol_id))
        return {
            "employee_id": data.get("employee_id", emp_id),
            "name": data.get("name"),
            "department": dept_names[0] if dept_names else None,
            "active_policies": policy_names,
        }


def build_demo_graph() -> KnowledgeGraph:
    """Демо-граф HR + policy для POC и тестов."""
    g = KnowledgeGraph()

    g.add_node("dept:engineering", type="department", name="Engineering")
    g.add_node("dept:hr", type="department", name="HR")
    g.add_node("dept:sales", type="department", name="Sales")

    g.add_node("emp:alice", type="employee", name="Alice", employee_id="HR-001")
    g.add_node("emp:bob", type="employee", name="Bob", employee_id="HR-002")
    g.add_node("emp:carol", type="employee", name="Carol", employee_id="HR-003")
    g.add_node("emp:dave", type="employee", name="Dave", employee_id="HR-004")

    g.add_node("pol:remote", type="policy", name="Remote Work", status="active")
    g.add_node("pol:health", type="policy", name="Health Insurance", status="active")
    g.add_node("pol:legacy", type="policy", name="Legacy Benefits", status="inactive")

    g.add_edge("emp:alice", "dept:engineering", "BELONGS_TO")
    g.add_edge("emp:bob", "dept:engineering", "BELONGS_TO")
    g.add_edge("emp:carol", "dept:hr", "BELONGS_TO")
    g.add_edge("emp:dave", "dept:sales", "BELONGS_TO")

    g.add_edge("emp:alice", "pol:remote", "COVERED_BY")
    g.add_edge("emp:bob", "pol:health", "COVERED_BY")
    g.add_edge("emp:carol", "pol:remote", "COVERED_BY")
    g.add_edge("emp:carol", "pol:health", "COVERED_BY")
    g.add_edge("emp:dave", "pol:legacy", "COVERED_BY")

    return g


def is_empty_result(payload: Dict[str, Any]) -> bool:
    """True, если граф не вернул данных (для NTA-style отказа)."""
    if payload.get("status") == "error":
        return True
    if payload.get("status") == "empty":
        return True
    count = payload.get("count")
    if count is not None and int(count) == 0:
        return True
    results = payload.get("results")
    if isinstance(results, list) and len(results) == 0 and payload.get("query_type") != "count":
        return True
    return False


def _parse_query(raw: str) -> Dict[str, Any]:
    """Парсит JSON-запрос или упрощённый key=value DSL."""
    text = raw.strip()
    if not text:
        raise ValueError("empty query")

    if text.startswith("{"):
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("query JSON must be an object")
        return parsed

    # Упрощённый DSL: count department=Engineering policy="Remote Work"
    match = re.match(r"^(\S+)\s+(.*)$", text)
    if not match:
        return {"query_type": text}
    query_type = match.group(1)
    rest = match.group(2)
    params: Dict[str, Any] = {"query_type": query_type}
    for key, val in re.findall(r'(\w+)=(\"[^\"]*\"|\'[^\']*\'|\S+)', rest):
        params[key] = val.strip().strip('"').strip("'")
    return params


class GraphQueryEngine:
    """Исполнение domain-specific запросов к графу HR/policy."""

    def __init__(self, graph: KnowledgeGraph):
        self.graph = graph

    def execute(self, raw_query: str) -> Dict[str, Any]:
        try:
            spec = _parse_query(raw_query)
        except (json.JSONDecodeError, ValueError) as exc:
            return {
                "status": "error",
                "count": 0,
                "results": [],
                "message": f"Invalid query: {exc}",
            }

        query_type = str(spec.get("query_type") or spec.get("type") or "count").lower()
        department = spec.get("department")
        policy = spec.get("policy")
        employee_name = spec.get("employee") or spec.get("name")
        policy_status = spec.get("policy_status", "active")

        trace: Dict[str, Any] = {"query_type": query_type, "filters": {}}
        if department:
            trace["filters"]["department"] = department
        if policy:
            trace["filters"]["policy"] = policy
        if employee_name:
            trace["filters"]["employee"] = employee_name

        if query_type in ("count", "count_employees"):
            return self._count_employees(department, policy, policy_status, trace)

        if query_type in ("list", "list_employees"):
            return self._list_employees(department, policy, policy_status, trace)

        if query_type == "employee_policies":
            return self._employee_policies(employee_name, trace)

        if query_type == "traverse":
            return self._traverse(spec, trace)

        return {
            "status": "error",
            "count": 0,
            "results": [],
            "message": f"Unknown query_type: {query_type}",
            "query_trace": trace,
        }

    def _filter_employees(
        self,
        department: Optional[str],
        policy: Optional[str],
        policy_status: str,
    ) -> Set[str]:
        if department:
            employees = self.graph.employee_ids_in_department(str(department))
        else:
            employees = {
                nid for nid, data in self.graph.nodes.items()
                if data.get("type") == "employee"
            }

        if policy:
            if policy_status == "active":
                employees = self.graph.employees_with_active_policy(employees, str(policy))
            else:
                pol_ids = self.graph.find_nodes("policy", name=str(policy), status=policy_status)
                if not pol_ids:
                    return set()
                matched: Set[str] = set()
                for emp_id in employees:
                    if set(self.graph.neighbors(emp_id, relation="COVERED_BY")) & set(pol_ids):
                        matched.add(emp_id)
                employees = matched

        return employees

    def _count_employees(
        self,
        department: Optional[str],
        policy: Optional[str],
        policy_status: str,
        trace: Dict[str, Any],
    ) -> Dict[str, Any]:
        employees = self._filter_employees(department, policy, policy_status)
        count = len(employees)
        status = "empty" if count == 0 else "ok"
        payload: Dict[str, Any] = {
            "status": status,
            "count": count,
            "results": [self.graph.serialize_employee(eid) for eid in sorted(employees)],
            "query_trace": trace,
        }
        if count == 0:
            payload["message"] = "No matching nodes in knowledge graph"
        return payload

    def _list_employees(
        self,
        department: Optional[str],
        policy: Optional[str],
        policy_status: str,
        trace: Dict[str, Any],
    ) -> Dict[str, Any]:
        employees = self._filter_employees(department, policy, policy_status)
        results = [self.graph.serialize_employee(eid) for eid in sorted(employees)]
        status = "empty" if not results else "ok"
        payload: Dict[str, Any] = {
            "status": status,
            "count": len(results),
            "results": results,
            "query_trace": trace,
        }
        if not results:
            payload["message"] = "No matching nodes in knowledge graph"
        return payload

    def _employee_policies(
        self,
        employee_name: Optional[str],
        trace: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not employee_name:
            return {
                "status": "error",
                "count": 0,
                "results": [],
                "message": "employee name is required",
                "query_trace": trace,
            }
        emp_ids = self.graph.find_nodes("employee", name=str(employee_name))
        if not emp_ids:
            return {
                "status": "empty",
                "count": 0,
                "results": [],
                "message": "No matching nodes in knowledge graph",
                "query_trace": trace,
            }
        results = [self.graph.serialize_employee(emp_ids[0])]
        return {
            "status": "ok",
            "count": 1,
            "results": results,
            "query_trace": trace,
        }

    def _traverse(self, spec: Dict[str, Any], trace: Dict[str, Any]) -> Dict[str, Any]:
        start_type = spec.get("start_type", "employee")
        start_name = spec.get("start_name")
        path_raw = spec.get("path") or []

        start_ids: Set[str]
        if start_name:
            start_ids = set(self.graph.find_nodes(str(start_type), name=str(start_name)))
        else:
            start_ids = {
                nid for nid, data in self.graph.nodes.items()
                if data.get("type") == start_type
            }

        path: List[Tuple[str, str]] = []
        for step in path_raw:
            if isinstance(step, (list, tuple)) and len(step) == 2:
                path.append((str(step[0]), str(step[1])))
            elif isinstance(step, dict):
                path.append((str(step["relation"]), str(step["node_type"])))

        end_ids = self.graph.traverse(start_ids, path) if path else start_ids
        results = []
        for nid in sorted(end_ids):
            node = dict(self.graph.nodes[nid])
            node.pop("id", None)
            results.append(node)

        count = len(results)
        status = "empty" if count == 0 else "ok"
        payload: Dict[str, Any] = {
            "status": status,
            "count": count,
            "results": results,
            "query_trace": {**trace, "path": path_raw},
        }
        if count == 0:
            payload["message"] = "No matching nodes in knowledge graph"
        return payload


_DEFAULT_ENGINE: Optional[GraphQueryEngine] = None


def get_query_engine(graph: Optional[KnowledgeGraph] = None) -> GraphQueryEngine:
    global _DEFAULT_ENGINE
    if graph is not None:
        return GraphQueryEngine(graph)
    if _DEFAULT_ENGINE is None:
        _DEFAULT_ENGINE = GraphQueryEngine(build_demo_graph())
    return _DEFAULT_ENGINE


def run_graph_query(query: str, graph: Optional[KnowledgeGraph] = None) -> str:
    """Выполняет graph query и возвращает JSON-строку."""
    engine = get_query_engine(graph)
    payload = engine.execute(query)
    return json.dumps(payload, ensure_ascii=False)


class GraphQueryInput(BaseModel):
    query: str = Field(
        description=(
            "Graph query as JSON or DSL. Examples: "
            '\'{"query_type":"count","department":"Engineering","policy":"Remote Work"}\' '
            'or count department=Engineering policy="Remote Work"'
        ),
    )


def create_graph_tools(graph: Optional[KnowledgeGraph] = None) -> List[BaseTool]:
    """LangChain tools для Graph-RAG."""
    engine = get_query_engine(graph)

    def _graph_query(query: str) -> str:
        return json.dumps(engine.execute(query), ensure_ascii=False)

    tool = StructuredTool.from_function(
        func=_graph_query,
        name="graph_query",
        description=(
            "Query the HR/policy knowledge graph for aggregates, counts, and entity relationships. "
            "Use for questions like 'how many employees in department X with policy Y'. "
            "Returns structured JSON with count/results; empty graph = status empty. "
            "Do NOT invent statistics — only report graph results."
        ),
        args_schema=GraphQueryInput,
    )
    return [tool]

