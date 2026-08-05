from __future__ import annotations

import json
import logging
import os
import gzip
import shutil
from pathlib import Path
from typing import Any

class Journal:
    def __init__(self, path: str | Path, *, max_bytes: int | None = None, backups: int = 4):
        self.path = Path(path)
        self.max_bytes = max_bytes if max_bytes is not None else int(
            os.environ.get("ARENA_HERO_JOURNAL_MAX_BYTES", 32 * 1024 * 1024)
        )
        self.backups = max(1, backups)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._compress_oversized_backups()
    def _compress_oversized_backups(self) -> None:
        for index in range(1, self.backups + 1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            target = self.path.with_name(f"{self.path.name}.legacy-{index}.gz")
            if source.exists() and source.stat().st_size > self.max_bytes and not target.exists():
                with source.open("rb") as src, gzip.open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                source.unlink()
    def _rotate(self, incoming_bytes: int) -> None:
        if (not self.path.exists()
                or self.path.stat().st_size + incoming_bytes <= self.max_bytes):
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backups}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backups - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))
    def write(self, event: str, **data: Any) -> None:
        row = {"event": event, **data}
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._rotate(len(encoded.encode("utf-8")))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(encoded)
        meta = {
            key: data[key]
            for key in ("session", "tick", "reason")
            if key in data
        }
        if event == "plan" and isinstance(data.get("result"), dict):
            meta["status"] = data["result"].get("status", data["result"].get("dry_run"))
        logging.getLogger("arena_agent").info("%s %s", event, meta)
