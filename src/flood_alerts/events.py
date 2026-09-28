"""处置服务的领域事件类型。

所有状态变更都以“事件”为唯一事实来源：事件一旦写入追加日志便不可修改，
预警升级、解除等只能产生新事件。迟到观测形成的是 revision_suggested，
绝不回写 alert_issued / alert_upgraded。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# 预警生命周期
ALERT_ISSUED = "alert_issued"
ALERT_UPGRADED = "alert_upgraded"
ALERT_CONFIRMED = "alert_confirmed"
CONFIRMATION_ESCALATED = "confirmation_escalated"
ALERT_EXPIRED = "alert_expired"
ALERT_RELEASE_REQUESTED = "alert_release_requested"
ALERT_RELEASE_REJECTED = "alert_release_rejected"
ALERT_RELEASED = "alert_released"

# 观测与修订
OBSERVATION_RECEIVED = "observation_received"
REVISION_SUGGESTED = "revision_suggested"
REVISION_RESOLVED = "revision_resolved"

# 人员转移
TRANSFER_STARTED = "transfer_started"
TRANSFER_PROGRESS = "transfer_progress"
TRANSFER_COMPLETED = "transfer_completed"
TRANSFER_OVERDUE = "transfer_overdue"

# 通知与待办
NOTIFICATION_FAILED = "notification_failed"
NOTIFICATION_TODO_RESOLVED = "notification_todo_resolved"

# 复盘
REVIEW_CREATED = "review_created"
REVIEW_DUE = "review_due"
REVIEW_COMPLETED = "review_completed"


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    at: datetime
    actor: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        from .clock import format_time

        return {
            "seq": self.seq,
            "type": self.type,
            "at": format_time(self.at),
            "actor": self.actor,
            "payload": self.payload,
        }
