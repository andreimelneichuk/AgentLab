#!/usr/bin/env python3
"""Propagate original/tool_executor.py and agent_core tool execution to all variants."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "original"
ORIGINAL_AGENT = (ORIGINAL / "agent_core.py").read_text(encoding="utf-8")

# Extract shared method block from original
START = "    async def _execute_single_tool(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:"
END = "        return self._tool_results_to_messages(results)\n"
start_idx = ORIGINAL_AGENT.index(START)
end_idx = ORIGINAL_AGENT.index(END) + len(END)
STANDARD_TOOL_METHODS = ORIGINAL_AGENT[start_idx:end_idx]

VARIANTS = sorted((ROOT / "improvements").glob("*/variant"))
assert len(VARIANTS) == 14, f"expected 14 variants, got {len(VARIANTS)}"

TOOL_IMPORT = """from tool_executor import (
    execute_tool_command,
    is_infra_error_result,
    tool_call_dedup_key,
    tool_result_to_content,
)
"""

RUN_TURN_REPLACEMENT = """                            outcome, tool_results = await self._execute_tool_round(tool_calls)
                            working.extend(self._tool_results_to_messages(tool_results))
                            if outcome == "finalize":
                                break
                            continue"""

RUN_TURN_REPLACEMENT_REGISTERED = """                            outcome, tool_results = await self._execute_tool_round(registered_calls)
                            working.extend(self._tool_results_to_messages(tool_results))
                            if outcome == "finalize":
                                break
                            continue"""


def copy_tool_executor(variant_dir: Path) -> None:
    shutil.copy2(ORIGINAL / "tool_executor.py", variant_dir / "tool_executor.py")


def add_imports(content: str) -> str:
    if "from tool_executor import" in content:
        return content
    if "import asyncio\n" not in content:
        content = content.replace(
            "from __future__ import annotations\n\n",
            "from __future__ import annotations\n\nimport asyncio\n",
        )
    anchor = "from langchain_openai import ChatOpenAI\n"
    if anchor not in content:
        raise ValueError("langchain_openai import anchor not found")
    return content.replace(anchor, anchor + "\n" + TOOL_IMPORT + "\n")


def patch_tool_limits(content: str) -> str:
    old = '''def tool_limits(config: Dict[str, Any]) -> Tuple[int, int]:
    limits = (config.get("tools") or {}).get("call_limit") or {}
    return int(limits.get("basic_rounds", 5)), int(limits.get("thread", 50))'''
    new = '''def tool_limits(config: Dict[str, Any]) -> Tuple[int, int, int]:
    limits = (config.get("tools") or {}).get("call_limit") or {}
    exec_retries = int(limits.get("tool_exec_fail_retries", 3))
    return (
        int(limits.get("basic_rounds", 5)),
        int(limits.get("thread", 50)),
        exec_retries,
    )'''
    if old not in content:
        if "tool_exec_fail_retries" in content and "exec_retries = int" in content:
            return content
        raise ValueError("tool_limits block not found")
    return content.replace(old, new)


def patch_agent_resources(content: str) -> str:
    if "tool_exec_fail_retries: int" in content:
        return content

    content = re.sub(
        r"(    thread_limit: int\n)(    prompt_path: Path)",
        r"\1    tool_exec_fail_retries: int\n\2",
        content,
        count=1,
    )

    content = content.replace(
        "        run_limit, thread_limit = tool_limits(config)",
        "        run_limit, thread_limit, exec_retries = tool_limits(config)",
    )

  # handle variants where tool_limits unpacking is on same line with other vars
    content = content.replace(
        "run_limit, thread_limit = tool_limits(config)\n        max_validation_retries",
        "run_limit, thread_limit, exec_retries = tool_limits(config)\n        max_validation_retries",
    )

    content = re.sub(
        r"(            thread_limit=thread_limit,\n)(            prompt_path=prompt_path,)",
        r"\1            tool_exec_fail_retries=exec_retries,\n\2",
        content,
        count=1,
    )
    return content


def replace_execute_tools_block(content: str, replacement: str) -> str:
    pattern = re.compile(
        r"    async def _execute_tools\([^)]*\)[^:]*:.*?"
        r"(?=\n    (?:async )?def |\n    @property|\n    def _)",
        re.DOTALL,
    )
    match = pattern.search(content)
    if not match:
        raise ValueError("_execute_tools block not found")
    return content[: match.start()] + replacement.rstrip() + "\n\n" + content[match.end() :]


def patch_run_turn_standard(content: str, old_call: str) -> str:
    if "_execute_tool_round" in content and old_call not in content:
        return content
    if old_call not in content:
        raise ValueError(f"run_turn call not found: {old_call!r}")
    return content.replace(old_call, RUN_TURN_REPLACEMENT)


def patch_config(variant_dir: Path) -> None:
    cfg = variant_dir / "config.yml"
    text = cfg.read_text(encoding="utf-8")
    if "tool_exec_fail_retries" in text:
        return
    text = text.replace(
        "    thread: 50\n",
        "    thread: 50\n    tool_exec_fail_retries: 3\n",
        1,
    )
    cfg.write_text(text, encoding="utf-8")


def standard_single_tool() -> str:
    return STANDARD_TOOL_METHODS


def single_tool_05() -> str:
    return STANDARD_TOOL_METHODS.replace(
        """        tool_name = tool_call.get("name", "")
        tool_args = tool_call.get("arguments") or {}
        tool = self.resources.tool_map.get(tool_name)
        result = await execute_tool_command(tool, tool_name, tool_args)""",
        """        tool_name = tool_call.get("name", "")
        tool_args = tool_call.get("arguments") or {}
        if not is_tool_allowed(tool_name, self._current_route):
            return {
                "tool_name": tool_name,
                "success": False,
                "data": None,
                "error": (
                    f"tool '{tool_name}' is not allowed for domain "
                    f"{self._current_route.domain.value}"
                ),
                "error_kind": "validation",
            }
        tool = self.resources.tool_map.get(tool_name)
        result = await execute_tool_command(tool, tool_name, tool_args)""",
    )


def methods_02() -> str:
    shared = STANDARD_TOOL_METHODS.split("    async def _execute_tool_calls", 1)[1]
    shared = "    async def _execute_tool_calls" + shared.rsplit("    async def _execute_tools", 1)[0]
    return '''    async def _execute_single_tool(
        self,
        tool_call: Dict[str, Any],
        *,
        validation_attempt: int = 1,
    ) -> Dict[str, Any]:
        """Один tool_call: единый путь через execute_tool_command(strict=True)."""
        validator = self.resources.tool_validator
        tool_name = tool_call.get("name", "")
        tool_args = tool_call.get("arguments") or {}
        tool = self.resources.tool_map.get(tool_name)

        result = await execute_tool_command(
            tool,
            tool_name,
            tool_args,
            strict=True,
            schema_registry=validator.schema_registry,
        )

        if not result.success and result.error_kind == "validation":
            errors = list(result.validation_errors) or ([result.error] if result.error else ["validation failed"])
            validator.record_rejection(tool_name, tool_args, errors, validation_attempt)
            return {
                "tool_name": tool_name,
                "success": False,
                "data": None,
                "error": format_validation_errors(tool_name, errors),
                "error_kind": "validation",
            }

        return {
            "tool_name": tool_name,
            "success": result.success,
            "data": result.data if result.success else None,
            "error": result.error if not result.success else None,
            "error_kind": result.error_kind if not result.success else None,
        }

''' + shared + methods_02_execute_tools_wrapper()


def patch_execute_tool_calls_for_02(content: str) -> str:
    return content.replace(
        "                return await self._execute_single_tool(call)",
        "                return await self._execute_single_tool(call, validation_attempt=self._validation_attempt)",
    )


def methods_02_execute_tools_wrapper() -> str:
    return '''    async def _execute_tools(
        self,
        tool_calls: List[Dict[str, Any]],
        *,
        validation_attempt: int = 1,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Исполняет tool calls после pre-validation. Возвращает (results, had_validation_failure)."""
        self._validation_attempt = validation_attempt
        outcome, tool_results = await self._execute_tool_round(tool_calls, validation_attempt=validation_attempt)
        messages = self._tool_results_to_messages(tool_results)
        had_validation_failure = any(
            (not r.get("success")) and r.get("error_kind") == "validation"
            for r in tool_results
        )
        if outcome == "finalize":
            return messages, had_validation_failure
        return messages, had_validation_failure
'''


def methods_11() -> str:
    single = '''    async def _execute_single_tool(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        """Один tool_call с neurosymbolic guardrails."""
        tool_name = tool_call.get("name", "")
        tool_args = tool_call.get("arguments") or {}

        violation = self.guardrails.pre_tool(self._guard_ctx, tool_name, tool_args)
        if violation:
            self._guard_ctx.record_block(tool_name, tool_args, violation)
            return {
                "tool_name": tool_name,
                "success": False,
                "data": None,
                "error": format_violation(violation),
                "error_kind": "validation",
            }

        tool = self.resources.tool_map.get(tool_name)
        result = await execute_tool_command(tool, tool_name, tool_args)
        if not result.success:
            self._guard_ctx.record_execution(tool_name, tool_args, result.error or "", ok=False)
            return {
                "tool_name": tool_name,
                "success": False,
                "data": None,
                "error": result.error,
                "error_kind": result.error_kind,
            }

        content = tool_result_to_content({
            "success": True,
            "data": result.data,
        })
        post_violation = self.guardrails.post_tool(self._guard_ctx, tool_name, tool_args, content)
        if post_violation:
            self._guard_ctx.record_block(tool_name, tool_args, post_violation)
            return {
                "tool_name": tool_name,
                "success": False,
                "data": None,
                "error": format_violation(post_violation),
                "error_kind": "validation",
            }

        self._guard_ctx.record_execution(
            tool_name, tool_args, content, ok=not tool_result_is_error(content),
        )
        return {
            "tool_name": tool_name,
            "success": True,
            "data": result.data,
            "error": None,
            "error_kind": None,
        }

'''
    rest = STANDARD_TOOL_METHODS.split("    async def _execute_tool_calls", 1)[1]
    return single + "    async def _execute_tool_calls" + rest


def methods_07() -> str:
    without_compat = STANDARD_TOOL_METHODS.rsplit("    async def _execute_tools", 1)[0]
    return without_compat + '''    async def _execute_tools(
        self,
        tool_calls: List[Dict[str, Any]],
        working: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Focus pseudo-tools + MCP tools с Basic execution."""
        updated_working = working
        focus_results: List[Dict[str, Any]] = []
        mcp_calls: List[Dict[str, Any]] = []
        mcp_positions: List[int] = []

        for idx, tc in enumerate(tool_calls):
            name = tc.get("name", "")
            if self._focus_active() and is_focus_tool(name):
                args = tc.get("arguments") or {}
                content, do_withdraw = self._handle_focus_tool(name, args, updated_working)
                if do_withdraw:
                    try:
                        updated_working = self.focus.withdraw(updated_working)
                    except FocusError as exc:
                        content = f"Error: {exc}"
                focus_results.append((idx, {
                    "role": "tool",
                    "content": content,
                    "tool_call_id": tc.get("id") or f"call_{uuid.uuid4().hex}",
                }))
            else:
                mcp_calls.append(tc)
                mcp_positions.append(idx)

        outcome, mcp_tool_results = await self._execute_tool_round(mcp_calls)
        mcp_messages = self._tool_results_to_messages(mcp_tool_results)
        mcp_by_idx = dict(zip(mcp_positions, mcp_messages))

        combined: List[Dict[str, Any]] = []
        focus_map = dict(focus_results)
        for i in range(len(tool_calls)):
            if i in focus_map:
                combined.append(focus_map[i])
            elif i in mcp_by_idx:
                combined.append(mcp_by_idx[i])

        if outcome == "finalize":
            self._last_tool_round_finalize = True
        else:
            self._last_tool_round_finalize = False

        return combined, updated_working
'''


def patch_run_turn_07(content: str) -> str:
    old = """                            tool_results, working = await self._execute_tools(tool_calls, working)
                            working.extend(tool_results)
                            continue"""
    new = """                            tool_results, working = await self._execute_tools(tool_calls, working)
                            working.extend(tool_results)
                            if getattr(self, "_last_tool_round_finalize", False):
                                break
                            continue"""
    if old not in content:
        if "_last_tool_round_finalize" in content:
            return content
        raise ValueError("run_turn 07 block not found")
    return content.replace(old, new)


def patch_run_turn_02(content: str) -> str:
    old = """                            tool_results, had_validation_failure = await self._execute_tools(
                                tool_calls,
                                validation_attempt=validation_failures + 1,
                            )
                            working.extend(tool_results)

                            if had_validation_failure:"""
    new = """                            outcome, raw_tool_results = await self._execute_tool_round(
                                tool_calls,
                                validation_attempt=validation_failures + 1,
                            )
                            tool_results = self._tool_results_to_messages(raw_tool_results)
                            working.extend(tool_results)
                            had_validation_failure = any(
                                (not r.get("success")) and r.get("error_kind") == "validation"
                                for r in raw_tool_results
                            )

                            if outcome == "finalize":
                                break

                            if had_validation_failure:"""
    if old not in content:
        if "_execute_tool_round" in content and "validation_attempt=validation_failures" in content:
            return content
        raise ValueError("run_turn 02 block not found")
    return content.replace(old, new)


def patch_execute_tool_round_02(content: str) -> str:
    old_sig = "    async def _execute_tool_round(self, tool_calls: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:"
    new_sig = "    async def _execute_tool_round(self, tool_calls: List[Dict[str, Any]], *, validation_attempt: int = 1) -> Tuple[str, List[Dict[str, Any]]]:"
    if old_sig in content and new_sig not in content:
        content = content.replace(old_sig, new_sig)
        content = content.replace(
            "        max_retries = self.resources.tool_exec_fail_retries\n        for retry_idx in range(max_retries + 1):",
            "        max_retries = self.resources.tool_exec_fail_retries\n        self._validation_attempt = validation_attempt\n        for retry_idx in range(max_retries + 1):",
            1,
        )
    return content


VARIANT_HANDLERS = {
    "01-four-section-prompt": ("standard", None),
    "02-tool-validation-layer": ("custom", "02"),
    "05-deterministic-routing": ("custom", "05"),
    "06-scan-prompt-drift": ("standard", None),
    "07-focus-active-compression": ("custom", "07"),
    "08-semantic-tool-selection": ("standard", None),
    "09-context-engineering": ("standard", None),
    "10-subtask-isolation": ("standard", None),
    "11-neurosymbolic-guardrails": ("custom", "11"),
    "12-multi-agent-validation": ("standard", None),
    "13-buddy-system": ("standard", None),
    "14-graph-rag": ("standard", None),
    "15-memory-formation": ("standard", None),
}


def process_variant(variant_dir: Path) -> str:
    name = variant_dir.parent.name
    kind, custom = VARIANT_HANDLERS[name]
    agent_path = variant_dir / "agent_core.py"
    content = agent_path.read_text(encoding="utf-8")

    copy_tool_executor(variant_dir)
    patch_config(variant_dir)

    content = add_imports(content)
    content = patch_tool_limits(content)
    content = patch_agent_resources(content)

    if kind == "standard":
        methods = standard_single_tool()
        content = replace_execute_tools_block(content, methods)
        if name == "13-buddy-system":
            content = content.replace(
                "                working.extend(await self._execute_tools(tool_calls))",
                """                outcome, tool_results = await self._execute_tool_round(tool_calls)
                working.extend(self._tool_results_to_messages(tool_results))
                if outcome == "finalize":
                    break
                continue""",
            )
        else:
            content = patch_run_turn_standard(
                content,
                "                            working.extend(await self._execute_tools(tool_calls))",
            )
    elif custom == "02":
        methods = methods_02()
        content = replace_execute_tools_block(content, methods)
        content = patch_execute_tool_calls_for_02(content)
        content = patch_execute_tool_round_02(content)
        content = patch_run_turn_02(content)
    elif custom == "05":
        content = replace_execute_tools_block(content, single_tool_05())
        content = patch_run_turn_standard(
            content,
            "                            working.extend(await self._execute_tools(tool_calls))",
        )
    elif custom == "07":
        content = replace_execute_tools_block(content, methods_07())
        content = patch_run_turn_07(content)
    elif custom == "11":
        content = replace_execute_tools_block(content, methods_11())
        content = patch_run_turn_standard(
            content,
            "                            working.extend(await self._execute_tools(tool_calls))",
        )
    else:
        raise ValueError(f"unknown custom handler {custom}")

    agent_path.write_text(content, encoding="utf-8")
    return name


def main() -> None:
    updated = []
    for variant_dir in VARIANTS:
        updated.append(process_variant(variant_dir))
        print(f"OK {variant_dir.parent.name}")
    print(f"\nUpdated {len(updated)} variants")


if __name__ == "__main__":
    main()
