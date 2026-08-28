"""Структурированный JSON-handoff между субагентами."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal


HandoffStatus = Literal["ok", "error", "skipped"]


@dataclass
class SubtaskHandoff:
  """Контракт передачи результата между субагентами."""

  subtask: str
  status: HandoffStatus
  data: Dict[str, Any] = field(default_factory=dict)
  tools_used: List[str] = field(default_factory=list)
  errors: List[str] = field(default_factory=list)

  def to_dict(self) -> Dict[str, Any]:
    """Сериализация в JSON-совместимый словарь."""
    return asdict(self)

  def to_json(self) -> str:
    """Сериализация в JSON-строку."""
    return json.dumps(self.to_dict(), ensure_ascii=False)

  @classmethod
  def from_dict(cls, payload: Dict[str, Any]) -> "SubtaskHandoff":
    """Десериализация из словаря."""
    return cls(
      subtask=str(payload.get("subtask", "")),
      status=payload.get("status", "error"),
      data=dict(payload.get("data") or {}),
      tools_used=list(payload.get("tools_used") or []),
      errors=list(payload.get("errors") or []),
    )

  @classmethod
  def ok(
    cls,
    subtask: str,
    *,
    data: Dict[str, Any] | None = None,
    tools_used: List[str] | None = None,
  ) -> "SubtaskHandoff":
    """Успешный handoff."""
    return cls(
      subtask=subtask,
      status="ok",
      data=dict(data or {}),
      tools_used=list(tools_used or []),
      errors=[],
    )

  @classmethod
  def error(cls, subtask: str, message: str) -> "SubtaskHandoff":
    """Handoff с ошибкой."""
    return cls(subtask=subtask, status="error", data={}, tools_used=[], errors=[message])
