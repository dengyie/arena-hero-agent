from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
    def write(self, event: str, **data: Any) -> None:
        row = {"event": event, **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        meta = {
            key: data[key]
            for key in ("session", "tick", "reason")
            if key in data
        }
        if event == "plan" and isinstance(data.get("result"), dict):
            meta["status"] = data["result"].get("status", data["result"].get("dry_run"))
        logging.getLogger("arena_agent").info("%s %s", event, meta)
