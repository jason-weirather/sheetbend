"""Offline fixtures. The integration server only binds loopback."""

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from typing import Any

import pytest


@pytest.fixture
def config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "default_source": "local",
        "sources": {
            "local": {
                "protocol": "openai-compatible",
                "base_url": "http://127.0.0.1:8000/v1",
                "default_model": "test-model",
                "scope": "local",
                "auth": {"type": "none"},
            }
        },
    }


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SHEETBEND_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


@pytest.fixture
def endpoint() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"requests": [], "status": 200, "content": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            self.respond(None)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            self.respond(body)

        def respond(self, body: dict[str, Any] | None) -> None:
            state["requests"].append({
                "method": self.command, "path": self.path,
                "headers": dict(self.headers), "body": body,
            })
            status = state["status"]
            if status != 200:
                payload = {"error": {"message": "DO_NOT_PRINT_PROVIDER_BODY", "type": "test"}}
            elif body is None:
                payload = {"data": [{"id": "test-model"}, {"id": "another-model"}]}
            else:
                content = state["content"]
                if content is None:
                    content = '{"ok":true}' if "response_format" in body else "OK"
                payload = {
                    "id": "chatcmpl-test", "object": "chat.completion", "created": 1,
                    "model": body["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
                }
            self.send_response(status)
            if "location" in state:
                self.send_header("Location", state["location"])
            if status == 200 and body is not None and body.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for text in [content, ""]:
                    chunk = {
                        "id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1,
                        "model": body["model"],
                        "choices": [{"index": 0, "delta": {"content": text},
                                     "finish_reason": None if text else "stop"}],
                    }
                    if not text:
                        chunk["usage"] = payload["usage"]
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                data = json.dumps(payload).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["base_url"] = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
