"""Общий клиент LLM для бенчмарка (агент, судья)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

import httpx
from langchain_openai import ChatOpenAI

logger = logging.getLogger("benchmark.llm_client")


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


async def build_chat_llm(
    config: Dict[str, Any],
    model_alias: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    llm_cfg = config["llm"]
    model_cfg = llm_cfg["models"][model_alias]
    backends = sorted(
        model_cfg.get("backends", []),
        key=lambda b: b.get("priority", 0),
        reverse=True,
    )
    if not backends:
        raise ValueError(f"No backends for model alias {model_alias!r}")

    hc_timeout = float(llm_cfg.get("timeout_healthcheck_timeout", 5))
    errors: List[str] = []
    selected: Tuple[Dict[str, Any], Dict[str, Any], str] | None = None
    for backend in backends:
        ref = backend["backend_instance_ref"]
        inst = llm_cfg["backend_instances"][ref]
        if await _backend_healthy(inst, hc_timeout):
            selected = (backend, inst, ref)
            break
        errors.append(f"{ref} unhealthy")

    if not selected:
        raise ConnectionError(f"No healthy backend for {model_alias!r}: {'; '.join(errors)}")

    backend, inst, _ref = selected
    params = dict(backend.get("default_openai_params") or {})
    if temperature is not None:
        params["temperature"] = temperature
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    elif params.get("max_tokens") is None and llm_cfg.get("default_max_tokens"):
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
