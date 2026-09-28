"""领域内可重复、可恢复的模拟时钟。

真实服务用墙上时间，但预警的过期、超时升级和复盘都必须按“原定时点”
推进，因此时钟与业务代码解耦：测试与演示用手动时钟，重启后先重放事件，
再把时钟拨到原定时点，定时器即按预定时间触发。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def parse_time(value: str) -> datetime:
    """解析服务内部统一使用的、带时区的 ISO-8601 时间字符串。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间必须带时区：{value}")
    return dt


def format_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Clock:
    """可暂停、可快进的时钟；定时器只在 advance 时按到达时点触发。"""

    def __init__(self, start: datetime | str) -> None:
        self._now = parse_time(start) if isinstance(start, str) else start
        self._seq = 0
        # 到期时间 -> 回调；同一时间多个回调按注册顺序触发。key 用于取消/顶替。
        self._timers: list[tuple[datetime, int, str, object]] = []

    def now(self) -> datetime:
        return self._now

    def iso(self) -> str:
        return format_time(self._now)

    def advance(self, delta: timedelta | str) -> list[object]:
        """把时钟向前推进，触发所有到期定时器，返回触发结果列表。"""
        if isinstance(delta, str):
            hours = float(delta)
            delta = timedelta(hours=hours)
        if delta < timedelta(0):
            raise ValueError("时钟只能向前推进")
        results: list[object] = []
        self._now = self._now + delta
        due = [t for t in self._timers if t[0] <= self._now]
        due.sort(key=lambda t: (t[0], t[1]))
        for entry in due:
            self._timers.remove(entry)
            results.append(entry[3]())
        return results

    def run_until(self, when: datetime | str) -> list[object]:
        target = parse_time(when) if isinstance(when, str) else when
        if target < self._now:
            raise ValueError("目标时间早于当前时间")
        delta = target - self._now
        return self.advance(delta) if delta > timedelta(0) else []

    def schedule(self, when: datetime, callback: object, key: str | None = None) -> None:
        """登记在 when 到达时触发的定时器；同 key 旧定时器先取消。

        重启重放后落后的定时器会在下次 advance 时补触发，回调内部按
        “预定时间与状态是否仍匹配”决定是否生效，保证不丢动作、不重复触发。
        """
        if key is not None:
            self.cancel(key)
        if when < self._now:
            when = self._now
        self._seq += 1
        self._timers.append((when, self._seq, key or "", callback))

    def cancel(self, key: str) -> None:
        self._timers = [t for t in self._timers if t[2] != key]

    @property
    def pending_count(self) -> int:
        return len(self._timers)
