"""追加式事件存储与原子状态元数据。

* events.log 为 JSONL 追加日志：只追加、不修改，重启后逐条重放重建状态；
* meta.json 保存模拟时钟当前时刻等易失信息，采用“临时文件 + 原子替换”，
  避免重启瞬间读到半写文件。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator


class EventStore:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "events.log"
        self.meta_path = self.dir / "meta.json"

    # ---- 事件日志 ----
    def append(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_all(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        with self.log_path.open("r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.read_all())

    # ---- 元数据 ----
    def save_meta(self, meta: dict[str, Any]) -> None:
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.meta_path)

    def load_meta(self) -> dict[str, Any]:
        if not self.meta_path.exists():
            return {}
        return json.loads(self.meta_path.read_text(encoding="utf-8"))
