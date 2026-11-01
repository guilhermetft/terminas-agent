"""Servidor HTTP local que imita a listagem de modelos de Claude, OpenAI e Gemini."""

from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager

VALID_KEYS = {
    "claude": "sk-ant-api03-" + "A" * 30,
    "codex": "sk-proj-" + "B" * 40,
    "gemini": "AIza" + "C" * 35,
}
RATE_LIMITED_KEY = "RATELIMITED-" + "d" * 20
SERVER_ERROR_KEY = "SERVERERROR-" + "e" * 20
BAD_JSON_KEY = "BADJSONBODY-" + "f" * 20

CLAUDE_MODELS = ["claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5-5"]
OPENAI_MODELS = [
    "gpt-5", "gpt-5-codex", "gpt-4o", "gpt-4.1", "text-embedding-3-small", "whisper-1",
    "dall-e-3", "gpt-4o-realtime-preview", "tts-1",
]  # fmt: skip
GEMINI_MODELS = [
    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
]


class _Handler(http.server.BaseHTTPRequestHandler):
    server: "_Server"

    def log_message(self, *args) -> None:
        pass

    def _reply(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self.server.requests.append({"path": self.path, "headers": dict(self.headers.items())})
        if self.path.startswith("/v1/models") and "x-api-key" in self.headers:
            provider, key = "claude", self.headers["x-api-key"]
        elif self.path.startswith("/v1/models"):
            provider, key = "codex", self.headers.get("Authorization", "").removeprefix("Bearer ")
        else:
            provider, key = "gemini", self.headers.get("x-goog-api-key", "")

        if key == RATE_LIMITED_KEY:
            return self._reply(429, "{}")
        if key == SERVER_ERROR_KEY:
            return self._reply(500, "boom")
        if key == BAD_JSON_KEY:
            return self._reply(200, "<html>nao e json</html>")
        if key != VALID_KEYS[provider]:
            return self._reply(400 if provider == "gemini" else 401, json.dumps({"error": "invalid key"}))
        if provider == "claude":
            return self._reply(200, json.dumps({"data": [{"id": m} for m in CLAUDE_MODELS]}))
        if provider == "codex":
            return self._reply(200, json.dumps({"data": [{"id": m} for m in OPENAI_MODELS]}))
        return self._reply(200, json.dumps({"models": GEMINI_MODELS}))


class _Server(http.server.HTTPServer):
    requests: list[dict]


@contextmanager
def fake_provider_server():
    server = _Server(("127.0.0.1", 0), _Handler)
    server.requests = []
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def base_urls(server: _Server) -> dict[str, str]:
    url = f"http://127.0.0.1:{server.server_port}"
    return {"claude": url, "codex": url, "gemini": url}
