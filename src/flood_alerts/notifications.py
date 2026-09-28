"""短信 / 电话通知网关。

网关是可脚本化的：演示与测试可令指定目标发送失败。业务约定：

* 发送失败只登记一条待办（notification_failed 事件），风险状态、预警等级
  一律不变；
* 待办由值守人员稍后重试，重试成功记 notification_todo_resolved；
* 待办不解除、不降级任何风险，闭环查询时仍计入未办结事项。
"""

from __future__ import annotations

from typing import Callable


class NotificationGateway:
    def __init__(self, fail_predicate: Callable[[str, str, str], bool] | None = None) -> None:
        # 参数：渠道(sms/voice)、目标、内容；返回 True 表示本次发送失败。
        self._fail = fail_predicate
        self.sent: list[tuple[str, str, str]] = []

    def send(self, channel: str, target: str, content: str) -> bool:
        if channel not in ("sms", "voice"):
            raise ValueError(f"未知通知渠道：{channel}")
        if self._fail is not None and self._fail(channel, target, content):
            return False
        self.sent.append((channel, target, content))
        return True
