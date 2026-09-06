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
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from tool_executor import (
    execute_tool_command,
    is_infra_error_result,
    tool_call_dedup_key,
    tool_result_to_content,
)


from buddy_agent import (
    CriteriaRegistry,
    build_criteria_registry,
    format_guide_message,
    judge_worker_output,
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


def messages_to_langchain(
    messages: List[Dict[str, Any]],
    system_prompt: str = "",
) -> List[BaseMessage]:
    """Конвертирует dict-сообщения в LangChain, добавляя system prompt целиком."""
    lc = dict_messages_to_langchain(messages)
    if system_prompt:
        return [SystemMessage(content=system_prompt), *lc]
    return lc


def dict_messages_to_langchain(messages: List[Dict[str, Any]]) -> List[BaseMessage]:
    lc: List[BaseMessage] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if role == "user":
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


def buddy_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """Настройки Buddy System из config.buddy."""
    cfg = dict(config.get("buddy") or {})
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "max_retries": int(cfg.get("max_retries", 3)),
        "symbolic_precheck": bool(cfg.get("symbolic_precheck", True)),
        # v3 fix: Tier-1 gate — вызывать дорогой LLM judge только когда
        # дешёвая эвристика находит сигнал риска (см. buddy_agent.py
        # detect_llm_judge_risk). Раньше вызывался безусловно на каждом
        # ходе — основная причина 4.38s латентности.
        "llm_gate": bool(cfg.get("llm_gate", True)),
    }


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
    criteria_registry: CriteriaRegistry

    @classmethod
    async def create(
        cls,
        config: Dict[str, Any],
        *,
        mcp_file: Optional[Path] = None,
        prompt_path: Path = Path("prompts/system_master.txt"),
    ) -> "AgentResources":
        mcp_configs = build_mcp_client_config(config, mcp_file)
        tools = await load_mcp_tools(mcp_configs)
        run_limit, thread_limit, exec_retries = tool_limits(config)
        system_prompt = render_system_prompt(config, prompt_path, bool(tools))
        return cls(
            config=config,
            tools=tools,
            tool_map={t.name: t for t in tools},
            openai_tools=tools_to_openai(tools),
            system_prompt=system_prompt,
            run_limit=run_limit,
            thread_limit=thread_limit,
            tool_exec_fail_retries=exec_retries,
            prompt_path=prompt_path,
            criteria_registry=build_criteria_registry(system_prompt, config),
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
    buddy_interventions: int = 0
    buddy_retries: int = 0
    buddy_passed: bool = True


class BasicLoopSession:
    """Цикл агента Basic: LLM → tool → LLM (_run_tool_loop)."""

    def __init__(self, resources: AgentResources, model_alias: Optional[str] = None):
        self.resources = resources
        self.model_alias = model_alias
        self._messages: List[Dict[str, Any]] = []

    def reset(self) -> None:
        self._messages = []

    @property
    def history_dicts(self) -> List[Dict[str, Any]]:
        return list(self._messages)

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
            "normalized_arguments": getattr(result, "normalized_arguments", None) or tool_args,
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


    async def _generate_turn(
        self,
        llm: ChatOpenAI,
        working: List[Dict[str, Any]],
        *,
        rounds_start: int = 0,
    ) -> Tuple[str, List[Dict[str, Any]], List[str], List[Dict[str, Any]], int, int, int]:
        """
        Один проход tool loop до финального ответа.

        Returns:
            answer, working, tool_names, tool_details, rounds, prompt_tokens, completion_tokens
        """
        round_limit = self.resources.run_limit
        rounds = rounds_start
        all_tool_names: List[str] = []
        turn_details: List[Dict[str, Any]] = []
        turn_prompt = turn_completion = 0

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

            lc_msgs = messages_to_langchain(working, self.resources.system_prompt)
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
                for tr in tool_results:
                    if tr.get("normalized_arguments") is not None:
                        t_name = tr.get("tool_name")
                        norm_args = tr.get("normalized_arguments")
                        for td in reversed(turn_details):
                            if td["name"] == t_name:
                                td["arguments"] = norm_args
                                break
                working.extend(self._tool_results_to_messages(tool_results))
                if outcome == "finalize":
                    break
                continue

            answer = str(result.content or "")
            working.append({"role": "assistant", "content": answer})
            return answer, working, all_tool_names, turn_details, rounds, turn_prompt, turn_completion

        final = await llm.ainvoke(messages_to_langchain(working, self.resources.system_prompt))
        meta = getattr(final, "response_metadata", None) or {}
        usage = meta.get("token_usage") or {}
        turn_prompt += int(usage.get("prompt_tokens") or 0)
        turn_completion += int(usage.get("completion_tokens") or 0)
        answer = str(final.content or "")
        working.append({"role": "assistant", "content": answer})
        return answer, working, all_tool_names, turn_details, rounds, turn_prompt, turn_completion

    async def run_turn(self, user_message: str) -> TurnResult:
        aliases = resolve_model_aliases(self.resources.config, self.model_alias)
        attempts = int((self.resources.config.get("llm_defaults") or {}).get("attempts_per_model", 2))
        # v2 fix: граница текущего хода в накопленной истории — нужна, чтобы
        # symbolic_precheck проверял "tool вызван В ЭТОМ ходе" не по ВСЕЙ
        # истории сессии (см. buddy_agent.symbolic_precheck docstring).
        turn_prefix_len = len(self._messages)
        messages = [*self._messages, {"role": "user", "content": user_message}]
        all_tool_names: List[str] = []
        all_tool_details: List[Dict[str, Any]] = []
        rounds = 0
        last_error: Optional[Exception] = None
        buddy_cfg = buddy_settings(self.resources.config)

        for alias in aliases:
            for _ in range(attempts):
                try:
                    t0 = time.monotonic()
                    llm = await build_llm(self.resources.config, alias)
                    working = list(messages)
                    turn_prompt = turn_completion = 0
                    buddy_interventions = 0
                    buddy_retries = 0
                    answer = ""
                    buddy_passed = True

                    for buddy_attempt in range(buddy_cfg["max_retries"] + 1):
                        (
                            answer,
                            working,
                            turn_tools,
                            turn_details,
                            rounds,
                            p_tok,
                            c_tok,
                        ) = await self._generate_turn(llm, working, rounds_start=rounds)
                        turn_prompt += p_tok
                        turn_completion += c_tok
                        all_tool_names.extend(turn_tools)
                        all_tool_details.extend(turn_details)

                        if not buddy_cfg["enabled"]:
                            break

                        guide = await judge_worker_output(
                            llm,
                            user_message=user_message,
                            worker_trace=working,
                            current_turn_trace=working[turn_prefix_len:],
                            draft_answer=answer,
                            criteria=self.resources.criteria_registry,
                            enable_symbolic_precheck=buddy_cfg["symbolic_precheck"],
                            enable_llm_gate=buddy_cfg["llm_gate"],
                        )
                        if guide is None:
                            buddy_passed = True
                            break

                        buddy_interventions += 1
                        buddy_passed = False
                        if buddy_attempt >= buddy_cfg["max_retries"]:
                            buddy_retries = buddy_attempt
                            break

                        if working and working[-1].get("role") == "assistant":
                            working.pop()
                        working.append({
                            "role": "user",
                            "content": format_guide_message(guide),
                        })
                        buddy_retries = buddy_attempt + 1

                    self._messages = working
                    latency = time.monotonic() - t0
                    return TurnResult(
                        answer=answer,
                        messages=list(self._messages),
                        tool_calls=all_tool_names,
                        tool_call_details=all_tool_details,
                        rounds=rounds,
                        latency_sec=latency,
                        prompt_tokens=turn_prompt,
                        completion_tokens=turn_completion,
                        total_tokens=turn_prompt + turn_completion,
                        buddy_interventions=buddy_interventions,
                        buddy_retries=buddy_retries,
                        buddy_passed=buddy_passed,
                    )
                except Exception as exc:
                    last_error = exc
                    logger.warning("Basic loop turn failed (%s): %s", alias, exc)

        raise RuntimeError(f"Basic loop session failed: {last_error}") from last_error


# Основная сессия агента (алиас для импортов).
BasicSession = BasicLoopSession
