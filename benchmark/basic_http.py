"""HTTP-сессия к production basic_assistant (фаза 2)."""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

import httpx

from benchmark.types import TurnResult
from benchmark.webhook_mock import drain_events, wait_for_message

logger = logging.getLogger("benchmark.basic_http")


class BasicHttpSession:
    """Клиент к basic_assistant API с webhook callback."""

    def __init__(
        self,
        base_url: str,
        webhook_url: str,
        *,
        configuration_id: str = "DEFAULT_AI_ASSISTANT",
        api_key: Optional[str] = None,
        message_timeout_s: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.webhook_url = webhook_url.rstrip("/")
        self.configuration_id = configuration_id
        self.api_key = api_key
        self.message_timeout_s = message_timeout_s
        self.bot_id: Optional[str] = None
        self.chat_id = f"bench-{uuid.uuid4().hex[:8]}"
        self._history: List[Dict[str, str]] = []

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    async def ensure_bot(self, client: httpx.AsyncClient) -> None:
        if self.bot_id:
            return
        payload = {
            "configuration_id": self.configuration_id,
            "callback": {
                "url": self.webhook_url,
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
            },
        }
        r = await client.post(
            f"{self.base_url}/bots",
            json=payload,
            headers=self._headers(),
            timeout=60.0,
        )
        r.raise_for_status()
        data = r.json()
        self.bot_id = data.get("bot_id")
        if not self.bot_id:
            raise RuntimeError(f"No bot_id in activation response: {data}")

    def reset(self) -> None:
        self._history = []
        self.chat_id = f"bench-{uuid.uuid4().hex[:8]}"

    @property
    def history_dicts(self) -> List[Dict[str, Any]]:
        return list(self._history)

    async def run_turn(self, user_message: str) -> TurnResult:
        drain_events()
        async with httpx.AsyncClient() as client:
            await self.ensure_bot(client)
            payload = {
                "message": {
                    "text": user_message,
                    "chat": {"id": self.chat_id},
                    "from": {"id": "benchmark-user"},
                },
                "use_tools": True,
            }
            r = await client.post(
                f"{self.base_url}/bots/{self.bot_id}/message",
                json=payload,
                headers=self._headers(),
                timeout=30.0,
            )
            r.raise_for_status()

        event = wait_for_message(self.message_timeout_s)
        if not event:
            raise TimeoutError("Webhook message callback not received")

        answer = ""
        if isinstance(event.get("message"), dict):
            answer = event["message"].get("text") or event["message"].get("content") or ""
        answer = answer or str(event.get("content") or event.get("text") or "")

        self._history.append({"role": "user", "content": user_message})
        self._history.append({"role": "assistant", "content": answer})

        return TurnResult(
            answer=answer,
            messages=list(self._history),
            tool_calls=[],
            rounds=0,
        )
