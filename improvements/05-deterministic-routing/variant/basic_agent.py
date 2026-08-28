#!/usr/bin/env python3
"""CLI для Basic-агента (tool loop + MCP)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import List, Optional

from agent_core import AgentResources, BasicLoopSession, load_config


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Basic-агент (tool loop + MCP)")
    p.add_argument("message", nargs="?", help="Вопрос пользователя")
    p.add_argument("-c", "--config", type=Path, default=Path("config.yml"))
    p.add_argument("--mcp-config", type=Path, help="JSON MCP-серверов (формат платформы)")
    p.add_argument("--prompt", type=Path, default=Path("prompts/system_master.txt"))
    p.add_argument("-m", "--model", help="Переопределить model_alias")
    p.add_argument("-i", "--interactive", action="store_true", help="Диалог в терминале")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


async def _interactive(resources: AgentResources, args: argparse.Namespace) -> None:
    session = BasicLoopSession(resources, model_alias=args.model)
    print("Basic agent (Ctrl+C или пустая строка для выхода)\n")
    while True:
        try:
            line = input("Вы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            break
        result = await session.run_turn(line)
        print(f"\nАссистент: {result.answer}\n")


async def _main_async(args: argparse.Namespace) -> int:
    if not args.config.exists():
        logging.error("Конфиг не найден: %s", args.config)
        return 1

    config = load_config(args.config)
    resources = await AgentResources.create(
        config, mcp_file=args.mcp_config, prompt_path=args.prompt,
    )

    if args.interactive:
        await _interactive(resources, args)
        return 0

    if not args.message:
        print("Укажите message или --interactive", file=sys.stderr)
        return 1

    session = BasicLoopSession(resources, model_alias=args.model)
    result = await session.run_turn(args.message)
    print(result.answer)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
