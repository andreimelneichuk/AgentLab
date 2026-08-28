"""Генерация Секции 2 промпта для бенчмарка из benchmark/mcp_tool_registry."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_PATH = REPO_ROOT / "benchmark" / "mcp_tool_registry.py"

# Порядок canonical-инструментов в Секции 2 (decoy_* исключены).
CANONICAL_TOOL_ORDER: Tuple[str, ...] = (
    "benchmark_probe",
    "get_policy_fact",
    "python_doc_lookup",
    "random_marker_probe",
    "flaky_tool",
    "empty_search",
    "inventory_lookup",
    "invoice_get",
    "weather_city",
    "translate_text",
    "calc_expression",
    "employee_lookup",
    "leave_balance",
    "org_chart_dept",
    "customer_get",
    "ticket_create",
    "sales_quote",
    "benchmark_sse_probe",
    "sse_audit_log",
)

# Дополнительные поля промпта (выход, «НЕ использовать») — не хранятся в registry.
PROMPT_EXTRAS: Dict[str, Dict[str, str]] = {
    "benchmark_probe": {
        "output": "строка `BENCH_MARKER_STREAMABLE=orchid-17`.",
        "do_not_use": "когда нужен SSE-маркер — `benchmark_sse_probe`.",
    },
    "get_policy_fact": {
        "output": "`POLICY_FACT=violet-42` и правило формата `[POLICY_OK]`.",
        "do_not_use": "`decoy_policy_fact` (устаревшая приманка с violet-99).",
    },
    "python_doc_lookup": {
        "output": "`Python topic=...: REF=PYDOC-778`.",
        "do_not_use": "`decoy_python_lookup` (приманка с FAKE-999).",
    },
    "random_marker_probe": {
        "output": "`RANDOM_MARKER=<hex>`.",
        "do_not_use": "для фиксированных бенчмарк-маркеров — `benchmark_probe` / `benchmark_sse_probe`.",
    },
    "flaky_tool": {
        "output": "`flaky_ok query=...` со второй попытки.",
        "do_not_use": "когда нужен стабильный результат с первого раза.",
    },
    "empty_search": {
        "output": "`{\"results\": []}`.",
        "do_not_use": "когда ожидаются данные — выбери предметный инструмент.",
    },
    "inventory_lookup": {
        "output": "JSON `sku`, `qty`, `warehouse`.",
        "do_not_use": "`decoy_inventory_lookup` (всегда qty=0).",
    },
    "invoice_get": {
        "output": "`INVOICE=... AMOUNT=... STATUS=...`.",
        "do_not_use": "для клиентских карточек — `customer_get`.",
    },
    "weather_city": {
        "output": "`WEATHER city=... temp_c=...`.",
        "do_not_use": "для бизнес-данных компании.",
    },
    "translate_text": {
        "output": "`TRANSLATED[target_lang]=...`.",
        "do_not_use": "для поиска фактов.",
    },
    "calc_expression": {
        "output": "`RESULT=...` или `ERROR: ...`.",
        "do_not_use": "для произвольного кода или текста.",
    },
    "employee_lookup": {
        "output": "`EMP_ID=HR-... NAME=... DEPT=...`.",
        "do_not_use": "`decoy_employee_search` (всегда NOT_FOUND).",
    },
    "leave_balance": {
        "output": "`LEAVE_DAYS=...`.",
        "do_not_use": "без предварительного `employee_lookup`, если emp_id неизвестен.",
    },
    "org_chart_dept": {
        "output": "`DEPT=... ROLES=... HEAD=...`.",
        "do_not_use": "для одного сотрудника — `employee_lookup`.",
    },
    "customer_get": {
        "output": "`CUSTOMER=... TIER=... REGION=...`.",
        "do_not_use": "`decoy_customer_lookup` (неверный tier).",
    },
    "ticket_create": {
        "output": "`TICKET_ID=TK-... status=open`.",
        "do_not_use": "для чтения данных без явного запроса на создание.",
    },
    "sales_quote": {
        "output": "`QUOTE product=... price_usd=...`.",
        "do_not_use": "для счетов — `invoice_get`.",
    },
    "benchmark_sse_probe": {
        "output": "`BENCH_MARKER_SSE=amber-91`.",
        "do_not_use": "для streamable — `benchmark_probe`.",
    },
    "sse_audit_log": {
        "output": "`AUDIT date=... events=...`.",
        "do_not_use": "`decoy_sse_cache`.",
    },
}

_SKIP_FUNCTIONS = frozenset({
    "record",
    "reset_stats",
    "_hash_marker",
    "register_all",
})


@dataclass
class RegistryTool:
    """Инструмент, извлечённый из mcp_tool_registry.py."""

    name: str
    description: str
    parameters: List[Tuple[str, str]] = field(default_factory=list)


def _annotation_label(annotation: Optional[ast.expr]) -> str:
    if annotation is None:
        return "string"
    text = ast.unparse(annotation)
    if text in {"str", "string"}:
        return "string"
    return text


def load_registry_tools(registry_path: Path = REGISTRY_PATH) -> Dict[str, RegistryTool]:
    """Парсит benchmark/mcp_tool_registry.py: имена, docstring, параметры."""
    source = registry_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    tools: Dict[str, RegistryTool] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name.startswith("_") or node.name in _SKIP_FUNCTIONS:
            continue
        if node.name.startswith("register_"):
            continue

        params: List[Tuple[str, str]] = []
        for arg in node.args.args:
            if arg.arg in {"self", "mcp", "cls"}:
                continue
            params.append((arg.arg, _annotation_label(arg.annotation)))

        tools[node.name] = RegistryTool(
            name=node.name,
            description=(ast.get_docstring(node) or "").strip(),
            parameters=params,
        )

    return tools


def is_decoy_tool(name: str) -> bool:
    return name.startswith("decoy_")


def get_canonical_tool_names(
    registry: Optional[Dict[str, RegistryTool]] = None,
) -> List[str]:
    """Canonical-инструменты (без decoy_*) в фиксированном порядке."""
    if registry is None:
        registry = load_registry_tools()
    known = set(registry) - {n for n in registry if is_decoy_tool(n)}
    ordered = [name for name in CANONICAL_TOOL_ORDER if name in known]
    extra = sorted(known - set(ordered))
    return ordered + extra


def _format_input_schema(tool: RegistryTool) -> str:
    if not tool.parameters:
        return "без параметров."
    parts = [f"`{name}` ({ptype}, обязательный)" for name, ptype in tool.parameters]
    if len(parts) == 1:
        return f"{parts[0]}."
    return ", ".join(parts[:-1]) + f", {parts[-1]}."


def _render_tool_entry(tool: RegistryTool) -> str:
    extra = PROMPT_EXTRAS.get(tool.name, {})
    lines = [
        f"**{tool.name}**",
        f"- Назначение: {tool.description}.",
        f"- Вход: {_format_input_schema(tool)}",
        f"- Выход: {extra.get('output', 'см. ответ инструмента.')}",
    ]
    do_not_use = extra.get("do_not_use")
    if do_not_use:
        lines.append(f"- НЕ использовать: {do_not_use}")
    return "\n".join(lines)


def render_benchmark_tools_section(
    registry_path: Path = REGISTRY_PATH,
) -> str:
    """Рендерит блок canonical-инструментов для Секции 2 бенчмарка."""
    registry = load_registry_tools(registry_path)
    entries = []
    for name in get_canonical_tool_names(registry):
        tool = registry.get(name)
        if tool is None:
            continue
        entries.append(_render_tool_entry(tool))
    return "\n\n".join(entries)
