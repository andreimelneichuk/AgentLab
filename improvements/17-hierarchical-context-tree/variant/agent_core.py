"""Общая логика агента: конфиг, MCP, LLM, промпт, сессии с историей."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml
from jinja2 import BaseLoader, Environment
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

from context_policy import ContextPolicy, ContextPolicyConfig
from context_tree import (
    HierarchicalTreeConfig,
    build_segments,
    reduce_oldest_async,
    render_tree_to_messages,
)
from scratch_store import ScratchStore
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from tool_executor import (
    execute_tool_command,
    is_infra_error_result,
    tool_call_dedup_key,
    tool_result_to_content,
)


logger = logging.getLogger("agent_core")


def load_config(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _normalize_global_server(name: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kind = cfg.get("type")
    if kind == "http":
        url = cfg["url"].rstrip("/")
        if not url.endswith("/mcp"):
            url = f"{url}/mcp"
        out: Dict[str, Any] = {"transport": "streamable_http", "url": url}
    elif kind == "sse":
        url = cfg["url"]
        if not (url.endswith("/sse") or url.endswith("/sse/")):
            url = url.rstrip("/") + "/sse/"
        else:
            url = url.rstrip("/") + "/"
        out = {"transport": "sse", "url": url}
    elif kind == "stdio":
        return {
            "transport": "stdio",
            "command": cfg["command"],
            "args": cfg.get("args", []),
        }
    else:
        logger.warning("MCP server %s: неизвестный type=%r", name, kind)
        return None

    headers = dict(cfg.get("headers") or {})
    api_key = (cfg.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if headers:
        out["headers"] = headers
    return out


def _normalize_platform_server(cfg: Dict[str, Any]) -> Dict[str, Any]:
    name = cfg["name"]
    proto = cfg["transport_protocol"]
    url = cfg["url"].rstrip("/")
    if proto == "streamable_http":
        if not (url.endswith("/mcp") or url.endswith("/mcp/")):
            url = url.rstrip("/") + "/mcp"
        out: Dict[str, Any] = {"transport": "streamable_http", "url": url}
    elif proto == "sse":
        if url.endswith("/sse") or url.endswith("/sse/"):
            url = url.rstrip("/") + "/"
        else:
            url = url.rstrip("/") + "/sse/"
        out = {"transport": "sse", "url": url}
    else:
        raise ValueError(f"MCP {name}: unsupported transport_protocol {proto!r}")

    headers = dict(cfg.get("headers") or {})
    api_key = (cfg.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if headers:
        out["headers"] = headers
    return out


def build_mcp_client_config(
    config: Dict[str, Any],
    mcp_file: Optional[Path] = None,
) -> Dict[str, Dict[str, Any]]:
    servers: Dict[str, Dict[str, Any]] = {}
    for name, srv in (config.get("mcp_servers") or {}).items():
        normalized = _normalize_global_server(name, srv)
        if normalized:
            servers[name] = normalized

    if mcp_file and mcp_file.exists():
        raw = json.loads(mcp_file.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("servers", [])
        for item in items:
            servers[item["name"]] = _normalize_platform_server(item)
    return servers


async def load_mcp_tools(server_configs: Dict[str, Dict[str, Any]]) -> List[BaseTool]:
    if not server_configs:
        return []
    client = MultiServerMCPClient(server_configs)
    tools = await client.get_tools()
    logger.info("MCP: %s tools from %s servers", len(tools), len(server_configs))
    return tools


async def _backend_healthy(cfg: Dict[str, Any], timeout: float) -> bool:
    url = cfg.get("health_check_url")
    if not url:
        return True
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, follow_redirects=False)
            return 200 <= r.status_code < 300
    except Exception as exc:
        logger.warning("Health check %s failed: %s", url, exc)
        return False


async def build_llm(config: Dict[str, Any], model_alias: str) -> ChatOpenAI:
    llm_cfg = config["llm"]
    model_cfg = llm_cfg["models"][model_alias]
    backends = sorted(model_cfg.get("backends", []), key=lambda b: b.get("priority", 0), reverse=True)
    if not backends:
        raise ValueError(f"No backends for model alias {model_alias!r}")

    hc_timeout = float(llm_cfg.get("timeout_healthcheck_timeout", 5))
    errors: List[str] = []
    selected = None
    for backend in backends:
        ref = backend["backend_instance_ref"]
        inst = llm_cfg["backend_instances"][ref]
        if await _backend_healthy(inst, hc_timeout):
            selected = (backend, inst, ref)
            break
        errors.append(f"{ref} unhealthy")

    if not selected:
        raise ConnectionError(f"No healthy backend for {model_alias!r}: {'; '.join(errors)}")

    backend, inst, ref = selected
    params = dict(backend.get("default_openai_params") or {})
    if params.get("max_tokens") is None and llm_cfg.get("default_max_tokens"):
        params["max_tokens"] = int(llm_cfg["default_max_tokens"])

    model_id = backend["backend_model_id"]
    if "{folder_id}" in model_id:
        model_id = model_id.format(folder_id=inst.get("folder_id", ""))

    timeout = int(inst.get("request_timeout", llm_cfg.get("request_timeout", 90)))
    return ChatOpenAI(
        model=model_id,
        base_url=inst["base_url"],
        api_key=inst.get("api_key", "EMPTY"),
        timeout=timeout,
        **params,
    )


def render_system_prompt(config: Dict[str, Any], prompt_path: Path, has_tools: bool) -> str:
    if prompt_path.exists():
        template = prompt_path.read_text(encoding="utf-8")
        if "{%" in template or "{{" in template:
            env = Environment(loader=BaseLoader(), autoescape=False)
            variables = {
                "is_default_bot": config.get("agent", {}).get("is_default_bot", True),
                "has_tools": has_tools,
                "is_strict_mode": config.get("agent", {}).get("is_strict_mode", False),
                "custom_base_role": config.get("agent", {}).get("custom_base_role", ""),
            }
            return env.from_string(template).render(**variables).strip()
        return template.strip()
    return (
        "Ты — AI-ассистент с доступом к внешним инструментам. "
        "Отвечай точно и вызывай инструменты при необходимости."
    )


def tools_to_openai(tools: List[BaseTool]) -> List[Dict[str, Any]]:
    openai_tools: List[Dict[str, Any]] = []
    for tool in tools:
        params: Dict[str, Any] = {"type": "object", "properties": {}}
        if tool.args_schema is not None:
            if isinstance(tool.args_schema, dict):
                params = tool.args_schema
            elif hasattr(tool.args_schema, "model_json_schema"):
                params = tool.args_schema.model_json_schema()
        openai_tools.append({
            "name": tool.name,
            "description": tool.description or "",
            "parameters": params,
        })
    return openai_tools


def dict_messages_to_langchain(messages: List[Dict[str, Any]]) -> List[BaseMessage]:
    lc: List[BaseMessage] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if role == "system":
            lc.append(SystemMessage(content=content))
        elif role == "user":
            lc.append(HumanMessage(content=content))
        elif role in ("assistant", "ai"):
            raw_tcs = msg.get("tool_calls")
            if raw_tcs:
                lc.append(AIMessage(
                    content=content,
                    tool_calls=[
                        {
                            "id": tc.get("id") or f"call_{uuid.uuid4().hex}",
                            "name": tc.get("name", ""),
                            "args": tc.get("arguments") or tc.get("args") or {},
                        }
                        for tc in raw_tcs
                        if isinstance(tc, dict) and tc.get("name")
                    ],
                ))
            else:
                lc.append(AIMessage(content=content))
        elif role == "tool":
            lc.append(ToolMessage(content=content, tool_call_id=msg.get("tool_call_id", "unknown")))
    return lc


def messages_to_langchain(
    messages: List[Dict[str, Any]],
    system_prompt: str = "",
) -> List[BaseMessage]:
    """Конвертирует dict-сообщения в LangChain; system prompt — если не в messages."""
    has_system = any(m.get("role") == "system" for m in messages)
    lc = dict_messages_to_langchain(messages)
    if system_prompt and not has_system:
        return [SystemMessage(content=system_prompt), *lc]
    return lc


def build_scratch_tools(scratch_store: ScratchStore) -> List[StructuredTool]:
    """Pseudo-tools save_scratch / load_scratch для агента."""

    def save_scratch(key: str, content: str) -> str:
        saved = scratch_store.save(key, content)
        return f"Saved to scratch:{saved}"

    def load_scratch(key: str) -> str:
        data = scratch_store.load(key)
        if data is None:
            return f"Error: scratch key not found: {key}"
        return data

    return [
        StructuredTool.from_function(
            save_scratch,
            name="save_scratch",
            description="Save large content to external scratch pad; returns key for later load_scratch.",
        ),
        StructuredTool.from_function(
            load_scratch,
            name="load_scratch",
            description="Load full content previously saved with save_scratch by key.",
        ),
    ]


def extract_tool_details_from_dicts(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    details: List[Dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            details.append({
                "name": tc.get("name", ""),
                "arguments": tc.get("arguments") or tc.get("args") or {},
            })
    return details


def resolve_model_aliases(config: Dict[str, Any], model_alias: Optional[str] = None) -> List[str]:
    defaults = config.get("llm_defaults") or {}
    if model_alias:
        return [model_alias]
    chain = defaults.get("model_alias_fallback_chain")
    if chain:
        return list(chain)
    return [defaults.get("model_alias", "qwen-moe-local")]


def tool_limits(config: Dict[str, Any]) -> Tuple[int, int, int]:
    limits = (config.get("tools") or {}).get("call_limit") or {}
    exec_retries = int(limits.get("tool_exec_fail_retries", 3))
    return (
        int(limits.get("tool_loop_rounds", limits.get("basic_rounds", 5))),
        int(limits.get("thread", 50)),
        exec_retries,
    )


@dataclass
class AgentResources:
    config: Dict[str, Any]
    tools: List[BaseTool]
    tool_map: Dict[str, BaseTool]
    openai_tools: List[Dict[str, Any]]
    system_prompt: str
    run_limit: int
    thread_limit: int
    tool_exec_fail_retries: int
    prompt_path: Path
    scratch_store: ScratchStore
    context_policy: ContextPolicy
    tree_config: HierarchicalTreeConfig

    @classmethod
    async def create(
        cls,
        config: Dict[str, Any],
        *,
        mcp_file: Optional[Path] = None,
        prompt_path: Path = Path("prompts/system_master.txt"),
    ) -> "AgentResources":
        mcp_configs = build_mcp_client_config(config, mcp_file)
        mcp_tools = await load_mcp_tools(mcp_configs)
        scratch_store = ScratchStore()
        scratch_tools = build_scratch_tools(scratch_store)
        tools = [*mcp_tools, *scratch_tools]
        policy_cfg = ContextPolicyConfig.from_config(config)
        context_policy = ContextPolicy(policy_cfg, scratch_store)
        tree_config = HierarchicalTreeConfig.from_config(config)
        run_limit, thread_limit, exec_retries = tool_limits(config)
        return cls(
            config=config,
            tools=tools,
            tool_map={t.name: t for t in tools},
            openai_tools=tools_to_openai(tools),
            system_prompt=render_system_prompt(config, prompt_path, bool(mcp_tools)),
            run_limit=run_limit,
            thread_limit=thread_limit,
            tool_exec_fail_retries=exec_retries,
            prompt_path=prompt_path,
            scratch_store=scratch_store,
            context_policy=context_policy,
            tree_config=tree_config,
        )


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


class BasicLoopSession:
    """Цикл агента Basic: LLM → tool → LLM (_run_tool_loop)."""

    def __init__(self, resources: AgentResources, model_alias: Optional[str] = None):
        self.resources = resources
        self.model_alias = model_alias
        self._messages: List[Dict[str, Any]] = []
        self._last_context_report: Optional[Dict[str, Any]] = None

    def reset(self) -> None:
        self._messages = []

    @property
    def history_dicts(self) -> List[Dict[str, Any]]:
        return list(self._messages)

    @property
    def last_context_report(self) -> Optional[Dict[str, Any]]:
        """Мониторинг сегментов финального prompt после последнего invoke."""
        return self._last_context_report

    async def _summarize_segments_via_llm(self, llm: ChatOpenAI, segments) -> str:
        """Tier-2: один LLM-вызов, сжимающий K самых старых leaf-сегментов.

        Вызывается ТОЛЬКО когда Tier-1 gate (leaf-count > threshold внутри
        reduce_oldest_async) уже решил, что редьюс нужен — сам этот метод
        никогда не вызывается "на всякий случай" каждый ход, в отличие от
        антипаттерна 13-buddy-system (безусловный LLM-вызов на каждом ходе,
        измеренная регрессия +4.38s latency).
        """
        from context_tree import default_digest, segment_text

        raw = "\n---\n".join(segment_text(seg) for seg in segments)
        prompt = (
            "Сожми следующий фрагмент диалога (самые старые ходы сессии) в "
            "1-3 коротких предложения на русском, сохранив ключевые факты и "
            "решения. Не придумывай ничего нового.\n\n" + raw[:4000]
        )
        try:
            result = await llm.ainvoke([HumanMessage(content=prompt)])
            return str(result.content or "").strip() or default_digest(segments)
        except Exception:
            return default_digest(segments)

    async def _prepare_llm_messages(
        self,
        working: List[Dict[str, Any]],
        current_query: str,
        llm: Optional[ChatOpenAI] = None,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """Применяет context policy (Write/Select/Compress/Isolate) перед invoke LLM.

        Дерево контекста (build_segments/reduce_oldest) уже применено ОДИН
        раз за этот ход в _reduce_stored_history_if_needed() и вплетено в
        self._messages — здесь `working` (self._messages + новые сообщения
        текущего раунда tool-loop) уже содержит summary-узлы там, где нужно,
        поэтому повторный reduce на каждый LLM-раунд внутри одного хода не
        делается (не тратим лишний Tier-2 LLM-вызов на раунд).
        """
        prepared, system_prompt, report = self.resources.context_policy.prepare_for_llm(
            working,
            system_prompt=self.resources.system_prompt,
            current_query=current_query,
        )
        self._last_context_report = report.to_dict()
        return prepared, system_prompt

    async def _execute_single_tool(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        """Один tool_call через ToolExecutor-логику (без tool_call_id)."""
        tool_name = tool_call.get("name", "")
        tool_args = tool_call.get("arguments") or {}
        tool = self.resources.tool_map.get(tool_name)
        result = await execute_tool_command(tool, tool_name, tool_args)
        return {
            "tool_name": tool_name,
            "success": result.success,
            "data": result.data if result.success else None,
            "error": result.error if not result.success else None,
            "error_kind": result.error_kind if not result.success else None,
        }

    async def _execute_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Параллельное исполнение с дедупликацией (Basic _execute_tool_calls)."""
        keyed: List[tuple] = []
        unique_calls: Dict[tuple, Dict[str, Any]] = {}
        order: List[tuple] = []
        for tool_call in tool_calls:
            if not tool_call.get("name"):
                logger.warning("Tool call without name, skipped.")
                continue
            key = tool_call_dedup_key(tool_call)
            keyed.append((tool_call, key))
            if key not in unique_calls:
                unique_calls[key] = tool_call
                order.append(key)

        if not order:
            return []

        semaphore = asyncio.Semaphore(5)

        async def _run(call: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                return await self._execute_single_tool(call)

        gathered = await asyncio.gather(*[_run(unique_calls[k]) for k in order])
        result_by_key = dict(zip(order, gathered))

        tool_results: List[Dict[str, Any]] = []
        for tool_call, key in keyed:
            base = result_by_key.get(key)
            if base is None:
                continue
            tool_results.append({**base, "tool_call_id": tool_call.get("id")})
        return tool_results

    async def _execute_tool_round(self, tool_calls: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
        """Один раунд tools с infra-retry (Basic _execute_tool_round).

        Returns:
            ("continue", results) — хотя бы один успех или non-infra fail
            ("finalize", results) — все infra fail, бюджет retry исчерпан
        """
        max_retries = self.resources.tool_exec_fail_retries
        for retry_idx in range(max_retries + 1):
            if retry_idx > 0:
                logger.info(
                    "Retrying tool execution (%s/%s) after infrastructure error.",
                    retry_idx,
                    max_retries,
                )

            tool_results = await self._execute_tool_calls(tool_calls)
            failed = [r for r in tool_results if not r.get("success")]
            all_failed = len(failed) == len(tool_results) and len(tool_results) > 0
            all_infra = all_failed and all(is_infra_error_result(r) for r in failed)

            if all_infra:
                if retry_idx < max_retries:
                    continue
                logger.error(
                    "All tool calls failed with infrastructure errors after %s retries.",
                    max_retries,
                )
                return "finalize", tool_results

            return "continue", tool_results

        return "finalize", []

    def _tool_results_to_messages(self, tool_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        for result in tool_results:
            tool_call_id = result.get("tool_call_id") or f"call_{result.get('tool_name', 'unknown')}"
            messages.append({
                "role": "tool",
                "content": tool_result_to_content(result),
                "tool_call_id": tool_call_id,
            })
        return messages

    async def _execute_tools(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Совместимость: один раунд без infra-retry (для простых вызовов)."""
        _, results = await self._execute_tool_round(tool_calls)
        return self._tool_results_to_messages(results)


    async def _reduce_stored_history_if_needed(self) -> None:
        """Персистентно применяет 2-уровневое дерево к self._messages.

        Вызывается один раз в начале каждого run_turn(), ДО добавления
        нового user-сообщения — иначе (если бы reduce применялся только к
        транзиентной копии для одного LLM-вызова, как раньше) 09-подобный
        pipeline пересчитывал бы reduce с нуля на каждом ходе и всегда
        сжимал бы одни и те же самые старые K сегментов, никогда не сдвигая
        границу "старого" дальше — эффективная компрессия длинной сессии
        стремилась бы к нулю. Персистентность в self._messages гарантирует,
        что каждая волна reduce забирает НОВУЮ порцию самых старых
        raw-сегментов (summary-узлы предыдущих волн больше не трогаются —
        только они и являются входом в grow, а не в reduce, что и держит
        дерево на глубине 2).
        """
        cfg = self.resources.tree_config
        if not cfg.enabled:
            return
        segments = build_segments(self._messages)
        if len(segments) <= cfg.leaf_threshold:
            return

        llm = None
        try:
            aliases = resolve_model_aliases(self.resources.config, self.model_alias)
            llm = await build_llm(self.resources.config, aliases[0])
        except Exception:
            llm = None  # Tier-2 LLM unavailable -> Tier-1 default_digest fallback

        summarize_fn = None
        if llm is not None:
            summarize_fn = lambda segs: self._summarize_segments_via_llm(llm, segs)  # noqa: E731

        nodes = await reduce_oldest_async(
            segments,
            threshold=cfg.leaf_threshold,
            k=cfg.reduce_k,
            summarize_fn=summarize_fn,
        )
        self._messages = render_tree_to_messages(nodes)

    async def run_turn(self, user_message: str) -> TurnResult:
        await self._reduce_stored_history_if_needed()
        aliases = resolve_model_aliases(self.resources.config, self.model_alias)
        attempts = int((self.resources.config.get("llm_defaults") or {}).get("attempts_per_model", 2))
        messages = [*self._messages, {"role": "user", "content": user_message}]
        all_tool_names: List[str] = []
        rounds = 0
        last_error: Optional[Exception] = None

        for alias in aliases:
            for _ in range(attempts):
                try:
                    t0 = time.monotonic()
                    llm = await build_llm(self.resources.config, alias)
                    working = list(messages)
                    round_limit = self.resources.run_limit
                    turn_prompt = turn_completion = 0
                    turn_details: List[Dict[str, Any]] = []

                    while True:
                        llm_bound = llm
                        if self.resources.openai_tools:
                            openai_fns = [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": t["name"],
                                        "description": t["description"],
                                        "parameters": t["parameters"],
                                    },
                                }
                                for t in self.resources.openai_tools
                            ]
                            llm_bound = llm.bind_tools(openai_fns)

                        prepared, sys_prompt = await self._prepare_llm_messages(working, user_message, llm)
                        lc_msgs = messages_to_langchain(prepared, sys_prompt)
                        result = await llm_bound.ainvoke(lc_msgs)
                        rounds += 1
                        meta = getattr(result, "response_metadata", None) or {}
                        usage = meta.get("token_usage") or {}
                        turn_prompt += int(usage.get("prompt_tokens") or 0)
                        turn_completion += int(usage.get("completion_tokens") or 0)

                        if getattr(result, "tool_calls", None):
                            if rounds >= round_limit:
                                break
                            tool_calls = [
                                {
                                    "id": tc.get("id") or f"call_{uuid.uuid4().hex}",
                                    "name": tc.get("name", ""),
                                    "arguments": tc.get("args") or {},
                                }
                                for tc in result.tool_calls
                            ]
                            all_tool_names.extend(tc["name"] for tc in tool_calls if tc["name"])
                            for tc in tool_calls:
                                turn_details.append({
                                    "name": tc["name"],
                                    "arguments": tc.get("arguments") or {},
                                })
                            working.append({
                                "role": "assistant",
                                "content": result.content or "",
                                "tool_calls": tool_calls,
                            })
                            outcome, tool_results = await self._execute_tool_round(tool_calls)
                            working.extend(self._tool_results_to_messages(tool_results))
                            if outcome == "finalize":
                                break
                            continue

                        answer = str(result.content or "")
                        working.append({"role": "assistant", "content": answer})
                        self._messages = working
                        latency = time.monotonic() - t0
                        return TurnResult(
                            answer=answer,
                            messages=list(self._messages),
                            tool_calls=all_tool_names,
                            tool_call_details=turn_details,
                            rounds=rounds,
                            latency_sec=latency,
                            prompt_tokens=turn_prompt,
                            completion_tokens=turn_completion,
                            total_tokens=turn_prompt + turn_completion,
                        )

                    prepared, sys_prompt = await self._prepare_llm_messages(working, user_message, llm)
                    final = await llm.ainvoke(messages_to_langchain(prepared, sys_prompt))
                    meta = getattr(final, "response_metadata", None) or {}
                    usage = meta.get("token_usage") or {}
                    turn_prompt += int(usage.get("prompt_tokens") or 0)
                    turn_completion += int(usage.get("completion_tokens") or 0)
                    answer = str(final.content or "")
                    working.append({"role": "assistant", "content": answer})
                    self._messages = working
                    latency = time.monotonic() - t0
                    return TurnResult(
                        answer=answer,
                        messages=list(self._messages),
                        tool_calls=all_tool_names,
                        tool_call_details=turn_details,
                        rounds=rounds,
                        latency_sec=latency,
                        prompt_tokens=turn_prompt,
                        completion_tokens=turn_completion,
                        total_tokens=turn_prompt + turn_completion,
                    )
                except Exception as exc:
                    last_error = exc
                    logger.warning("Basic loop turn failed (%s): %s", alias, exc)

        raise RuntimeError(f"Basic loop session failed: {last_error}") from last_error


# Основная сессия агента (алиас для импортов).
BasicSession = BasicLoopSession
