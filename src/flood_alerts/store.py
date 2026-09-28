"""JSON 持久化：每次状态变更后整体落盘，服务重启后从同一文件恢复。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class JsonStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict[str, Any]:
        with self.path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
