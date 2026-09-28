"""雨量与河道水位观测。

每份观测带观测时间（observed_at，由站网产生）和入库时间（received_at，
由模拟时钟决定）。预警触发依据锁定的是“引用时刻可见的观测”，之后迟到的
观测不会改写历史，只能形成修订建议（见 service.revise_on_late_data）。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime

RAIN = "rain"
WATER = "water"

# 山洪气象风险阈值（演示用简化阈值，单位：毫米 / 米）。
BLUE_RAIN_1H = 20.0
YELLOW_RAIN_1H = 40.0
ORANGE_RAIN_1H = 60.0
RED_RAIN_1H = 80.0
WATER_WARN_DELTA = 1.5  # 距保证水位差值（米）


@dataclass(frozen=True)
class Observation:
    obs_id: str
    kind: str  # RAIN / WATER
    station: str
    area_code: str
    observed_at: datetime
    value: float
    unit: str
    reporter: str  # 水文站人员 / 气象部门 / 网格员
    received_at: datetime

    def to_dict(self) -> dict:
        from .clock import format_time

        data = asdict(self)
        data["observed_at"] = format_time(self.observed_at)
        data["received_at"] = format_time(self.received_at)
        return data


def rain_threshold(level: str) -> float:
    return {
        "blue": BLUE_RAIN_1H,
        "yellow": YELLOW_RAIN_1H,
        "orange": ORANGE_RAIN_1H,
        "red": RED_RAIN_1H,
    }[level]


def suggest_level(rain_1h: float, water_gap: float | None = None) -> str | None:
    """按最新 1 小时雨量给出建议风险等级；水位逼近保证水位时抬升一级。"""
    level: str | None = None
    if rain_1h >= RED_RAIN_1H:
        level = "red"
    elif rain_1h >= ORANGE_RAIN_1H:
        level = "orange"
    elif rain_1h >= YELLOW_RAIN_1H:
        level = "yellow"
    elif rain_1h >= BLUE_RAIN_1H:
        level = "blue"
    if level is not None and water_gap is not None and water_gap <= WATER_WARN_DELTA:
        order = ["blue", "yellow", "orange", "red"]
        level = order[min(order.index(level) + 1, len(order) - 1)]
    return level
