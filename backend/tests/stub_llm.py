"""A tiny OpenAI-compatible stand-in for llama-server (SDD 15.1 "stub LLM server")."""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

RAG_ANSWER = "The monthly rent is 950 euros [1]."


def _doc_type(text: str) -> str:
    low = text.lower()
    for key, dt in (("mietvertrag", "contract"), ("invoice", "invoice"), ("lebenslauf", "cv"), ("lecture", "lecture"),
                    ("report", "report"), ("steuer", "form")):
        if key in low:
            return dt
    return "other"


def create_stub_app(delay_s: float = 0.0) -> FastAPI:
    app = FastAPI()
    app.state.requests = []

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        app.state.requests.append(body)
        msgs = body.get("messages", [])
        text = "\n".join(m.get("content", "") for m in msgs)
        if body.get("response_format"):
            m = re.search(r"File name: (.+)", text)
            name = m.group(1).strip() if m else "file"
            content = json.dumps({"summary": f"Stub summary of {name}.", "doc_type": _doc_type(name),
                                  "tags": ["stub", "fixture", name.split(".")[0].lower()[:20]]})
        elif msgs and msgs[0].get("role") == "system":
            content = RAG_ANSWER
        else:
            m = re.search(r"Last question: (.+)", text)
            content = m.group(1).strip() if m else "Stub block summary."

        async def gen():
            words = content.split(" ")
            for i, w in enumerate(words):
                piece = w if i == 0 else " " + w
                yield f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]})}\n\n"
                if delay_s:
                    await asyncio.sleep(delay_s)
            yield f"data: {json.dumps({'choices': [], 'usage': {'prompt_tokens': 123, 'completion_tokens': len(words)}})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


class StubLLMServer:
    def __init__(self, delay_s: float = 0.0):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.app = create_stub_app(delay_s)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "StubLLMServer":
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
