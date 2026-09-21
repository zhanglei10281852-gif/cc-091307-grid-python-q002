"""JSON 文件持久化：原子写入，重启后排班进度与隐私授权保持一致。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import (
    Elder,
    Escalation,
    Leave,
    PlanVersion,
    Visit,
    Worker,
)


class Store:
    """单文件 JSON 存储。写操作先落临时文件再原子替换。"""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.workers: dict[str, Worker] = {}
        self.elders: dict[str, Elder] = {}
        self.visits: dict[str, Visit] = {}
        self.escalations: dict[str, Escalation] = {}
        self.leaves: dict[str, Leave] = {}
        self.versions: list[PlanVersion] = []
        self.load()

    # ------------------------------------------------------------------ load/save

    def load(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.workers = {w["id"]: Worker.from_dict(w) for w in raw.get("workers", [])}
        self.elders = {e["id"]: Elder.from_dict(e) for e in raw.get("elders", [])}
        self.visits = {v["id"]: Visit.from_dict(v) for v in raw.get("visits", [])}
        self.escalations = {
            x["id"]: Escalation.from_dict(x) for x in raw.get("escalations", [])
        }
        self.leaves = {x["id"]: Leave.from_dict(x) for x in raw.get("leaves", [])}
        self.versions = [PlanVersion.from_dict(v) for v in raw.get("versions", [])]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "workers": [w.to_dict() for w in self.workers.values()],
            "elders": [e.to_dict() for e in self.elders.values()],
            "visits": [v.to_dict() for v in self.visits.values()],
            "escalations": [x.to_dict() for x in self.escalations.values()],
            "leaves": [x.to_dict() for x in self.leaves.values()],
            "versions": [v.to_dict() for v in self.versions],
        }
        # 同目录临时文件 + os.replace，保证应用重启时读到的永远是完整快照
        fd, tmp = tempfile.mkstemp(prefix=".careplan-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ queries

    def next_version(self) -> int:
        return (self.versions[-1].version + 1) if self.versions else 1

    def visits_for(self, elder_id: str) -> list[Visit]:
        return [v for v in self.visits.values() if v.elder_id == elder_id]

    def pending_for_worker(self, worker_id: str, day) -> list[Visit]:
        from .models import OPEN_STATUSES

        return sorted(
            (
                v
                for v in self.visits.values()
                if v.worker_id == worker_id
                and v.status in OPEN_STATUSES
                and v.scheduled_start.date() == day
            ),
            key=lambda v: v.scheduled_start,
        )
