"""Минимальный webhook mock для приёма callback Basic."""
from __future__ import annotations

import queue
import threading
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request

message_events: "queue.Queue[dict]" = queue.Queue()
status_events: "queue.Queue[dict]" = queue.Queue()

app = FastAPI(title="benchmark-webhook-mock")


@app.post("/message")
async def message(request: Request):
    data = await request.json()
    message_events.put(data)
    return {"status": "ok"}


@app.post("/status")
async def status(request: Request):
    data = await request.json()
    status_events.put(data)
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}


def drain_events() -> None:
    for q in (message_events, status_events):
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break


def wait_for_message(timeout_s: float = 120.0) -> Optional[Dict[str, Any]]:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            return message_events.get(timeout=1.0)
        except queue.Empty:
            continue
    return None


def start_webhook_server(host: str = "127.0.0.1", port: int = 19110) -> threading.Thread:
    import uvicorn

    def _run():
        uvicorn.run(app, host=host, port=port, log_level="warning")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread
