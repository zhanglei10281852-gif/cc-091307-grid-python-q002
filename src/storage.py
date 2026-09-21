"""JSON 持久化：原子写入，保证重启后状态可恢复。"""

from __future__ import annotations

import json
import os
from pathlib import Path


class JsonStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # 原子替换，避免写一半损坏状态

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))
