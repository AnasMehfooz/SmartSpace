"""OpenAI-compatible client for llama-server (/v1/chat/completions, streaming, JSON-schema mode)."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable

import httpx
from starlette.concurrency import run_in_threadpool

from app.errors import AppError
from app.services.indexer import Preempted

if TYPE_CHECKING:
    from app.context import AppContext


class InteractiveGate:
    """Counts interactive LLM users (chat, query rewrite). Summary jobs yield while it is busy."""

    def __init__(self) -> None:
        self._count = 0
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()

    def enter(self) -> None:
        with self._lock:
            self._count += 1
            self._idle.clear()

    def exit(self) -> None:
        with self._lock:
            self._count = max(0, self._count - 1)
            if self._count == 0:
                self._idle.set()

    def busy(self) -> bool:
        return not self._idle.is_set()

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)


def _parse_sse_line(line: str) -> dict[str, Any] | None | str:
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload == "[DONE]":
        return "DONE"
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def _delta(obj: dict[str, Any]) -> str:
    choices = obj.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    return delta.get("content") or ""


def _usage(obj: dict[str, Any], usage: dict[str, int]) -> None:
    u = obj.get("usage")
    if isinstance(u, dict):
        if u.get("prompt_tokens") is not None:
            usage["tokens_in"] = int(u["prompt_tokens"])
        if u.get("completion_tokens") is not None:
            usage["tokens_out"] = int(u["completion_tokens"])
    t = obj.get("timings")
    if isinstance(t, dict):
        if t.get("prompt_n") is not None and "tokens_in" not in usage:
            usage["tokens_in"] = int(t["prompt_n"])
        if t.get("predicted_n") is not None and "tokens_out" not in usage:
            usage["tokens_out"] = int(t["predicted_n"])


class LLMClient:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx

    @property
    def server(self):
        return self.ctx.llm

    def _payload(self, messages: list[dict[str, str]], *, max_tokens: int, temperature: float, top_p: float,
                 schema: dict[str, Any] | None = None) -> dict[str, Any]:
        p: dict[str, Any] = {
            "model": self.server.model_id or "local",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if schema is not None:
            p["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "strict": True, "schema": schema}}
        return p

    def complete(self, messages: list[dict[str, str]], *, max_tokens: int = 256, temperature: float = 0.0,
                 top_p: float = 1.0, schema: dict[str, Any] | None = None,
                 should_abort: Callable[[], bool] | None = None, timeout: float = 600) -> tuple[str, dict[str, int]]:
        """Blocking completion for worker threads. Streams internally so it can be aborted."""
        self.server.ensure_ready()
        parts: list[str] = []
        usage: dict[str, int] = {}
        payload = self._payload(messages, max_tokens=max_tokens, temperature=temperature, top_p=top_p, schema=schema)
        with self.server.use():
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
                with client.stream("POST", f"{self.server.base_url}/v1/chat/completions", json=payload) as r:
                    if r.status_code != 200:
                        body = r.read().decode("utf-8", errors="replace")[:300]
                        raise AppError("LLM_NOT_READY", f"LLM request failed (HTTP {r.status_code})",
                                       details={"body": body})
                    for line in r.iter_lines():
                        if should_abort is not None and should_abort():
                            raise Preempted()
                        obj = _parse_sse_line(line)
                        if obj == "DONE":
                            break
                        if isinstance(obj, dict):
                            parts.append(_delta(obj))
                            _usage(obj, usage)
        return "".join(parts), usage

    def complete_json(self, messages: list[dict[str, str]], schema: dict[str, Any], **kw) -> tuple[dict[str, Any], dict[str, int]]:
        text, usage = self.complete(messages, schema=schema, **kw)
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):]
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("LLM did not return JSON")
        return json.loads(text[start : end + 1]), usage

    async def astream(self, messages: list[dict[str, str]], *, max_tokens: int = 512, temperature: float = 0.2,
                      top_p: float = 0.9, stop_event: asyncio.Event | None = None) -> AsyncIterator[dict[str, Any]]:
        """Async token stream for chat: yields {"t": text} items, then {"usage": {...}}."""
        await run_in_threadpool(self.server.ensure_ready)
        payload = self._payload(messages, max_tokens=max_tokens, temperature=temperature, top_p=top_p)
        usage: dict[str, int] = {}
        self.server.begin_use()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10.0)) as client:
                async with client.stream("POST", f"{self.server.base_url}/v1/chat/completions", json=payload) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode("utf-8", errors="replace")[:300]
                        raise AppError("LLM_NOT_READY", f"LLM request failed (HTTP {r.status_code})",
                                       details={"body": body})
                    async for line in r.aiter_lines():
                        if stop_event is not None and stop_event.is_set():
                            break
                        obj = _parse_sse_line(line)
                        if obj == "DONE":
                            break
                        if isinstance(obj, dict):
                            _usage(obj, usage)
                            t = _delta(obj)
                            if t:
                                yield {"t": t}
        finally:
            self.server.end_use()
        yield {"usage": usage}
