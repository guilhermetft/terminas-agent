"""Log de auditoria estruturado (JSONL): uma linha JSON por evento (RNF-03).

ATENÇÃO: eventos de guardrail incluem o comando solicitado. Se um comando contiver segredos
(tokens, senhas), eles serão gravados; proteja o arquivo como dado sensível."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class JsonlAuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()

    def __call__(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
