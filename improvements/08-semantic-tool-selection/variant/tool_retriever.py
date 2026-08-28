"""Гибридный retriever инструментов: keyword routing + semantic top-k выбор."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, FrozenSet

from langchain_core.tools import BaseTool

from routing import RouterState, route_turn, filter_tools_by_route

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[а-яА-ЯёЁ]+", re.UNICODE)


def is_decoy_tool(name: str) -> bool:
    """True для adversarial-приманок с префиксом decoy_."""
    return name.startswith("decoy_")


def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _schema_to_text(schema: Any) -> str:
    if schema is None:
        return ""
    if isinstance(schema, dict):
        return json.dumps(schema, ensure_ascii=False, sort_keys=True)
    if hasattr(schema, "model_json_schema"):
        return json.dumps(schema.model_json_schema(), ensure_ascii=False, sort_keys=True)
    return str(schema)


@dataclass(frozen=True)
class ToolDocument:
    """Текстовое представление инструмента для индексации."""

    name: str
    description: str
    schema_text: str = ""

    @property
    def corpus_text(self) -> str:
        parts = [self.name.replace("_", " "), self.description]
        if self.schema_text:
            parts.append(self.schema_text)
        return " ".join(p for p in parts if p)


def tool_document_from_base(tool: BaseTool) -> ToolDocument:
    """Собрать документ из LangChain BaseTool."""
    schema = None
    if tool.args_schema is not None:
        schema = tool.args_schema
    return ToolDocument(
        name=tool.name,
        description=tool.description or "",
        schema_text=_schema_to_text(schema),
    )


def tool_document_from_openai(tool: Mapping[str, Any]) -> ToolDocument:
    """Собрать документ из OpenAI-style dict (name, description, parameters)."""
    return ToolDocument(
        name=str(tool.get("name") or ""),
        description=str(tool.get("description") or ""),
        schema_text=_schema_to_text(tool.get("parameters")),
    )


@dataclass
class ToolSelectionConfig:
    """Настройки фильтрации инструментов перед bind_tools."""

    top_k: int = 5
    mandatory: List[str] = field(default_factory=list)
    deny: List[str] = field(default_factory=list)
    enabled: bool = True

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "ToolSelectionConfig":
        raw = dict(config.get("tool_selection") or {})
        return cls(
            top_k=int(raw.get("top_k", 5)),
            mandatory=list(raw.get("mandatory") or []),
            deny=list(raw.get("deny") or []),
            enabled=bool(raw.get("enabled", True)),
        )


class TfidfToolRetriever:
    """Offline-индекс описаний tools и top-k поиск по cosine similarity."""

    def __init__(self, documents: Sequence[ToolDocument]):
        self.documents: List[ToolDocument] = list(documents)
        self._names: List[str] = [d.name for d in self.documents]
        self._name_to_idx: Dict[str, int] = {n: i for i, n in enumerate(self._names)}
        self._idf: Dict[str, float] = {}
        self._vectors: List[Dict[str, float]] = []
        self._build_index()

    @classmethod
    def from_tools(cls, tools: Sequence[BaseTool]) -> "TfidfToolRetriever":
        return cls([tool_document_from_base(t) for t in tools])

    @classmethod
    def from_openai_tools(cls, tools: Sequence[Mapping[str, Any]]) -> "TfidfToolRetriever":
        return cls([tool_document_from_openai(t) for t in tools])

    def _build_index(self) -> None:
        doc_tokens: List[List[str]] = [_tokenize(d.corpus_text) for d in self.documents]
        df: Dict[str, int] = {}
        for tokens in doc_tokens:
            for tok in set(tokens):
                df[tok] = df.get(tok, 0) + 1
        n_docs = max(len(self.documents), 1)
        self._idf = {tok: math.log((1 + n_docs) / (1 + count)) + 1.0 for tok, count in df.items()}
        self._vectors = [self._tfidf_vector(tokens) for tokens in doc_tokens]

    def _tfidf_vector(self, tokens: Sequence[str]) -> Dict[str, float]:
        if not tokens:
            return {}
        tf: Dict[str, float] = {}
        for tok in tokens:
            tf[tok] = tf.get(tok, 0.0) + 1.0
        norm = float(len(tokens))
        vec: Dict[str, float] = {}
        for tok, count in tf.items():
            weight = (count / norm) * self._idf.get(tok, 0.0)
            if weight:
                vec[tok] = weight
        return vec

    @staticmethod
    def _cosine(a: Mapping[str, float], b: Mapping[str, float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in set(a) | set(b))
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / (na * nb)

    def search(self, query: str, k: int = 5) -> List[Tuple[str, float]]:
        """Top-k инструментов по релевантности запросу."""
        if not self.documents or k <= 0:
            return []
        q_vec = self._tfidf_vector(_tokenize(query))
        scored = [
            (self._names[i], self._cosine(q_vec, self._vectors[i]))
            for i in range(len(self._names))
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    def search_names(self, query: str, k: int = 5) -> List[str]:
        """Имена top-k без score."""
        return [name for name, _ in self.search(query, k=k)]


def effective_deny_set(config: ToolSelectionConfig, all_tool_names: Iterable[str]) -> Set[str]:
    """Deny-list: явный deny + все decoy_* (decoy исключаются всегда)."""
    denied = set(config.deny)
    denied.update(name for name in all_tool_names if is_decoy_tool(name))
    return denied


def select_tool_names(
    retriever: TfidfToolRetriever,
    query: str,
    *,
    all_tool_names: Sequence[str],
    config: ToolSelectionConfig,
) -> List[str]:
    """Семантический top-k + mandatory (устарело, используй hybrid_select_tool_names)."""
    if not config.enabled:
        return list(all_tool_names)

    denied = effective_deny_set(config, all_tool_names)
    available = [n for n in all_tool_names if n not in denied]

    selected: List[str] = []
    seen: Set[str] = set()

    for name in config.mandatory:
        if name in all_tool_names and name not in denied and name not in seen:
            selected.append(name)
            seen.add(name)

    for name in retriever.search_names(query, k=config.top_k):
        if name in denied or name not in available:
            continue
        if name not in seen:
            selected.append(name)
            seen.add(name)

    if not selected:
        return list(available)
    return selected


def hybrid_select_tool_names(
    retriever: TfidfToolRetriever,
    query: str,
    router_state: RouterState,
    *,
    all_tool_names: Sequence[str],
    config: ToolSelectionConfig,
) -> tuple[List[str], str]:
    """
    Гибридный выбор инструментов: keyword routing + semantic retrieval.

    1. Routing: определить активные домены по keywords
    2. Фильтр: ограничить инструменты до разрешённых доменом
    3. Keywords: добавить явно названные инструменты (если есть в сообщении)
    4. Semantic: используется только для ПОРЯДКА/подрезки, если candidate
       list аномально большой (см. "v2 fix" в IMPLEMENTATION.md) — иначе
       ВСЕ tools, разрешённые routing, идут в LLM как есть.

    v1 hybrid бага (найдено при 189-scenario offline-прогоне, TSA упал
    96%→67%): здесь раньше был `semantic top-k` cut ПОВЕРХ routing —
    `len(selected) >= config.top_k + len(config.mandatory)` резал
    already-routing-approved candidates (включая CORE tools, которых
    всегда >= 12, что уже больше top_k=5). TF-IDF часто даёт score=0.0
    для перефразированных RU-запросов (нет стемминга), и stable-sort
    tie-break на 0.0 отдавал предпочтение alphabetически более ранним
    tools, вырезая нужный (employee_lookup, org_chart_dept, sales_quote,
    invoice_get, ...). Routing (Tier-1, keyword-based) уже является
    достаточным фильтром "релевантно или нет" — семантика не должна его
    ещё раз резать до 5 инструментов.

    Returns:
        (список имён инструментов, prompt fragment для контекста)
    """
    if not config.enabled:
        return list(all_tool_names), ""

    # Routing: определить домены и разрешённые tools
    route_result = route_turn(router_state, query)
    allowed_by_routing = route_result.allowed_tools

    # Intersection: только tools которые разрешены routing И не в deny-list
    denied = effective_deny_set(config, all_tool_names)
    candidate_tools = [
        n for n in all_tool_names
        if n in allowed_by_routing and n not in denied
    ]

    if not candidate_tools:
        # Fallback: все доступные инструменты если routing не разрешил ничего
        candidate_tools = [n for n in all_tool_names if n not in denied]

    selected: List[str] = []
    seen: Set[str] = set()

    # 1. Mandatory tools всегда первыми
    for name in config.mandatory:
        if name in candidate_tools and name not in seen:
            selected.append(name)
            seen.add(name)

    # 2. Явно названные в query инструменты (сильный сигнал, всегда включаем)
    query_lower = query.lower()
    for name in candidate_tools:
        name_variants = [name, name.replace("_", " "), name.replace("_", "-")]
        if any(v in query_lower for v in name_variants) and name not in seen:
            selected.append(name)
            seen.add(name)

    # 3. Остальные routing-approved tools: включаем ВСЕ, не режем semantic top-k.
    #    Safety cap: если из-за sticky-доменов кандидатов накопилось заметно
    #    больше, чем нужно (несколько доменов активны одновременно), режем
    #    ТОЛЬКО хвост по semantic score — но cap заведомо шире top_k, чтобы
    #    не повторить баг с обрезкой CORE tools.
    remaining = [n for n in candidate_tools if n not in seen]
    cap = max(config.top_k, 1) * 4  # напр. top_k=5 -> cap=20 (>= полного CORE+1 домен)

    if len(remaining) <= cap:
        selected.extend(remaining)
        seen.update(remaining)
    else:
        scored = retriever.search(query, k=len(remaining))
        ordered = [n for n, _ in scored if n in remaining]
        ordered += [n for n in remaining if n not in ordered]  # безопасность: не потерять tools
        for name in ordered[:cap]:
            selected.append(name)
            seen.add(name)

    if not selected:
        return candidate_tools[:config.top_k], route_result.prompt_fragment
    return selected, route_result.prompt_fragment


def filter_openai_tools(
    openai_tools: Sequence[Mapping[str, Any]],
    selected_names: Sequence[str],
) -> List[Dict[str, Any]]:
    """Оставить только tools из selected_names, сохраняя порядок selected_names."""
    by_name = {str(t["name"]): dict(t) for t in openai_tools}
    return [by_name[n] for n in selected_names if n in by_name]


def build_query_text(user_message: str, recent_messages: Optional[Sequence[Mapping[str, Any]]] = None) -> str:
    """Запрос для retriever: текущее сообщение + недавний контекст."""
    parts = [user_message or ""]
    for msg in recent_messages or ():
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if role in ("user", "assistant") and content:
            parts.append(str(content))
    return "\n".join(parts)
