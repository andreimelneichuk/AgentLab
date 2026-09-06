"""Загрузка agent_core из папки варианта (original, my-tweak, …)."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType


def load_agent_module(variant_dir: Path) -> ModuleType:
    root = variant_dir.resolve()
    if not (root / "agent_core.py").exists():
        raise FileNotFoundError(f"agent_core.py не найден в {root}")

    path = str(root)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)

    # Invalidate variant-specific cached modules so each variant loads its own files
    for mod in list(sys.modules.keys()):
        if mod in ("agent_core", "tool_executor", "basic_agent"):
            sys.modules.pop(mod, None)

    import agent_core

    return importlib.reload(agent_core)
