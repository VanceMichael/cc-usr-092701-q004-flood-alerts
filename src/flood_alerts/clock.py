"""模拟时钟：所有时间从这里读取，保证演练可重放、重启后按原定时点推进。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Clock:
    """只进不退的离散模拟时钟。

    真实部署可替换为返回墙钟时间的实现，领域服务只依赖 :meth:`now`。
    """

    _now: str
    history: list[tuple[str, str]] = field(default_factory=list)

    def now(self) -> str:
        return self._now

    def advance(self, value: str, reason: str = "") -> None:
        if value <= self._now:
            raise ValueError(f"模拟时钟只能前进：{self._now} -> {value}")
        self.history.append((value, reason))
        self._now = value

    def to_dict(self) -> dict:
        return {"now": self._now, "history": [list(item) for item in self.history]}

    @classmethod
    def from_dict(cls, value: dict) -> Clock:
        return cls(_now=value["now"], history=[tuple(item) for item in value.get("history", [])])
