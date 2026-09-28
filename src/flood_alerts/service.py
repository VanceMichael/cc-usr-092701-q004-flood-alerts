"""山洪预警闭环处置服务。

一条预警的生命周期：

    draft ──发布──▶ issued ──确认──▶ confirmed ──解除批准──▶ released
                      │                │
                      └── 过期 ────────┴──▶ expired（终态，仍需复盘）

- 每条预警锁定适用时段、行政区与触发依据（观测快照），发布后不可变；
- 迟到观测只生成修订建议，绝不改写已发出的预警；
- 相邻区域联动转移共享同一本转移台账，避免重复计数；
- 值守人员未按时确认即按升级策略交接给上级，直到有人确认或升级到顶；
- 发布人不能批准自己提出的解除；
- 短信/电话发送失败只生成待办，风险状态保持不变，可重试；
- 模拟时钟推进时，过期预警、超时未完成转移、到期复盘任务按原定时点自动推进；
- 所有动作记入审计链，服务重启后仍可回答“谁在何时依据哪份观测作决定”。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .clock import Clock
from .errors import (
    JurisdictionError,
    NotFoundError,
    SegregationError,
    StateError,
    ValidationError,
)
from .store import JsonStore

LEVELS = ("blue", "yellow", "orange", "red")
LEVEL_RANK = {level: idx for idx, level in enumerate(LEVELS)}
LEVEL_CN = {"blue": "蓝色", "yellow": "黄色", "orange": "橙色", "red": "红色"}

# 确认时限（分钟）：发布后此时长内未确认即升级给上级
CONFIRM_TIMEOUT_MINUTES = 30
# 转移任务时限（分钟）：超时未完成则标记逾期并提醒
TRANSFER_TIMEOUT_MINUTES = 120
# 复盘时限（分钟）：预警解除/过期后此时长内必须完成复盘
REVIEW_TIMEOUT_MINUTES = 1440

ROLE_CN = {
    "commander": "防汛指挥员",
    "hydrologist": "水文站人员",
    "grid_worker": "乡镇网格员",
    "superior": "上级值班人员",
}

# 演示用组织树：行政区 -> 上级行政区（None 表示顶层）
REGION_HIERARCHY = {
    "town_qingxi": "county_guangyuan",
    "town_baolun": "county_guangyuan",
    "county_guangyuan": "city_guangyuan",
    "city_guangyuan": "province_sichuan",
    "province_sichuan": None,
}
REGION_CN = {
    "town_qingxi": "青溪镇",
    "town_baolun": "宝轮镇",
    "county_guangyuan": "广元县",
    "city_guangyuan": "广元市",
    "province_sichuan": "四川省",
}

# 升级链：乡镇网格员 -> 县值班 -> 市值班 -> 省值班
ESCALATION_CHAIN = ["grid_worker", "county_duty", "city_duty", "province_duty"]


def _new_id(prefix: str, seq: int) -> str:
    return f"{prefix}-{seq:06d}"


def min_iso(left: str, right: str) -> str:
    """ISO 8601 同时区字符串可直接按字典序比较。"""
    return left if left <= right else right


class FloodAlertService:
    """闭环处置服务，状态可经 :class:`JsonStore` 持久化与恢复。"""

    def __init__(
        self,
        store: JsonStore,
        clock: Clock | None = None,
        notifier: Callable[[str, str, str], bool] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock or Clock("2026-09-28T08:00:00")
        # notifier(channel, target, content) -> 是否发送成功；None 表示总是成功
        self.notifier = notifier
        self._state: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 持久化

    @classmethod
    def open(cls, path: str | Path, notifier=None) -> "FloodAlertService":
        """从 ``path`` 恢复服务；文件不存在则初始化空状态。"""
        store = JsonStore(path)
        if store.exists():
            data = store.load()
            clock = Clock.from_dict(data["clock"])
            service = cls(store, clock=clock, notifier=notifier)
            service._state = data
        else:
            service = cls(store, notifier=notifier)
            service._state = service._empty_state()
            service._persist()
        return service

    def _empty_state(self) -> dict[str, Any]:
        return {
            "seq": 0,
            "actors": {},
            "observations": [],
            "warnings": [],
            "transfer_ledger": {"tasks": [], "groups": {}},
            "notifications": [],
            "todos": [],
            "reviews": [],
            "audit": [],
        }

    @property
    def state(self) -> dict[str, Any]:
        assert self._state is not None
        return self._state

    def _persist(self) -> None:
        payload = dict(self.state)
        payload["clock"] = self.clock.to_dict()
        self.store.save(payload)

    def _next_id(self, prefix: str) -> str:
        self.state["seq"] += 1
        return _new_id(prefix, self.state["seq"])

    # ------------------------------------------------------------------ 审计

    def _audit(
        self,
        actor_id: str,
        action: str,
        target: str,
        *,
        detail: dict | None = None,
        at: str | None = None,
    ) -> None:
        self.state["audit"].append(
            {
                "id": self._next_id("aud"),
                "at": at or self.clock.now(),
                "actor_id": actor_id,
                "action": action,
                "target": target,
                "detail": detail or {},
            }
        )

    # ------------------------------------------------------------------ 参与方

    def register_actor(self, actor_id: str, name: str, role: str, region: str) -> dict:
        if actor_id in self.state["actors"]:
            raise ValidationError(f"参与方已存在：{actor_id}")
        if role not in ROLE_CN and role not in ("county_duty", "city_duty", "province_duty"):
            raise ValidationError(f"未知角色：{role}")
        if region not in REGION_HIERARCHY:
            raise ValidationError(f"未知行政区：{region}")
        actor = {"id": actor_id, "name": name, "role": role, "region": region}
        self.state["actors"][actor_id] = actor
        self._audit(actor_id, "register_actor", actor_id, detail={"role": role, "region": region})
        self._persist()
        return actor

    def _actor(self, actor_id: str) -> dict:
        actor = self.state["actors"].get(actor_id)
        if actor is None:
            raise NotFoundError(f"参与方不存在：{actor_id}")
        return actor

    def _is_jurisdiction(self, actor: dict, region: str) -> bool:
        """乡镇只能处理本辖区；县、市、省可处理下级辖区。"""
        cursor: str | None = region
        while cursor is not None:
            if cursor == actor["region"]:
                return True
            cursor = REGION_HIERARCHY.get(cursor)
        return False

    # ------------------------------------------------------------------ 观测

    def record_observation(
        self,
        actor_id: str,
        region: str,
        kind: str,
        value: float,
        unit: str,
        observed_at: str,
        source: str,
    ) -> dict:
        """登记一份观测（雨量/水位等）。

        预警一经发出，其时段、区域与依据即被锁定；此后同区域再补充到达的
        观测（无论观测时刻早晚）只能挂为修订建议，供指挥员决定是否升级，
        绝不自动改写或抹掉已经发出的预警。
        """
        actor = self._actor(actor_id)
        if not self._is_jurisdiction(actor, region):
            raise JurisdictionError(f"{ROLE_CN.get(actor['role'], actor['role'])}无权在{REGION_CN.get(region, region)}登记观测")
        if observed_at > self.clock.now():
            raise ValidationError("观测时刻不能晚于当前模拟时间")
        obs = {
            "id": self._next_id("obs"),
            "region": region,
            "kind": kind,
            "value": value,
            "unit": unit,
            "observed_at": observed_at,
            "source": source,
            "recorded_at": self.clock.now(),
            "recorded_by": actor_id,
        }
        self.state["observations"].append(obs)
        self._audit(actor_id, "record_observation", obs["id"], detail={"kind": kind, "value": value, "region": region})

        # obs 为新登记观测，此刻不可能是任何已发预警的依据；
        # 凡同区域生效中的预警，该观测都只能形成修订建议。
        late_for = [
            w
            for w in self.state["warnings"]
            if w["region"] == region and w["status"] in ("issued", "confirmed")
        ]
        for w in late_for:
            suggestion = {
                "id": self._next_id("rev"),
                "warning_id": w["id"],
                "observation_id": obs["id"],
                "created_at": self.clock.now(),
                "status": "pending",
            }
            w["revision_suggestions"].append(suggestion)
            self._audit(
                actor_id,
                "revision_suggested",
                w["id"],
                detail={"observation_id": obs["id"], "reason": "观测在预警发出后补充到达，仅形成修订建议"},
            )
        self._persist()
        return obs

    def _observation(self, obs_id: str) -> dict:
        for obs in self.state["observations"]:
            if obs["id"] == obs_id:
                return obs
        raise NotFoundError(f"观测不存在：{obs_id}")

    # ------------------------------------------------------------------ 预警

    def _get_warning(self, warning_id: str) -> dict:
        for w in self.state["warnings"]:
            if w["id"] == warning_id:
                return w
        raise NotFoundError(f"预警不存在：{warning_id}")

    def _active_warning(self, region: str) -> dict | None:
        for w in self.state["warnings"]:
            if w["region"] == region and w["status"] in ("issued", "confirmed"):
                return w
        return None

    def issue_warning(
        self,
        actor_id: str,
        region: str,
        level: str,
        valid_from: str,
        valid_until: str,
        basis_observation_ids: list[str],
        headline: str,
    ) -> dict:
        """发布一条预警，锁定时段、行政区与触发依据（观测快照）。"""
        actor = self._actor(actor_id)
        if level not in LEVELS:
            raise ValidationError(f"未知预警级别：{level}")
        if valid_from >= valid_until:
            raise ValidationError("预警生效时间必须早于解除时间")
        if not basis_observation_ids:
            raise ValidationError("预警必须锁定触发依据观测")
        basis = [self._observation(oid) for oid in basis_observation_ids]
        for obs in basis:
            if obs["region"] != region:
                raise ValidationError("触发依据观测必须来自同一行政区")
        if self._active_warning(region) is not None:
            raise StateError(f"{REGION_CN.get(region, region)}已有生效中的预警，请先升级或解除")
        if not self._is_jurisdiction(actor, region):
            raise JurisdictionError(f"{ROLE_CN.get(actor['role'], actor['role'])}无权在{REGION_CN.get(region, region)}发布预警")

        warning = {
            "id": self._next_id("warn"),
            "region": region,
            "level": level,
            "headline": headline,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "basis": [
                {
                    "observation_id": obs["id"],
                    "kind": obs["kind"],
                    "value": obs["value"],
                    "unit": obs["unit"],
                    "observed_at": obs["observed_at"],
                    "source": obs["source"],
                    "recorded_by": obs["recorded_by"],
                }
                for obs in basis
            ],
            "status": "issued",
            "issued_by": actor_id,
            "issued_at": self.clock.now(),
            "confirmed_by": None,
            "confirmed_at": None,
            "escalation_level": 0,
            "confirm_deadline": self._add_minutes(self.clock.now(), CONFIRM_TIMEOUT_MINUTES),
            "revision_suggestions": [],
            "release_requested_by": None,
            "release_requested_at": None,
            "released_by": None,
            "released_at": None,
            "expired_at": None,
        }
        self.state["warnings"].append(warning)
        self._audit(
            actor_id,
            "issue_warning",
            warning["id"],
            detail={
                "level": level,
                "region": region,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "basis": [obs["id"] for obs in basis],
            },
        )
        self._notify_warning(warning, "issued")
        self._persist()
        return warning

    def upgrade_warning(
        self, actor_id: str, warning_id: str, new_level: str, basis_observation_ids: list[str], headline: str
    ) -> dict:
        """升级预警级别；升级后需重新确认，原通知不抹除。"""
        actor = self._actor(actor_id)
        warning = self._get_warning(warning_id)
        if warning["status"] not in ("issued", "confirmed"):
            raise StateError(f"预警已{warning['status']}，不能升级")
        if LEVEL_RANK[new_level] <= LEVEL_RANK[warning["level"]]:
            raise ValidationError("升级后的级别必须高于原级别")
        if not basis_observation_ids:
            raise ValidationError("升级必须锁定新的触发依据观测")
        basis = [self._observation(oid) for oid in basis_observation_ids]
        for obs in basis:
            if obs["region"] != warning["region"]:
                raise ValidationError("触发依据观测必须来自同一行政区")
        if not self._is_jurisdiction(actor, warning["region"]):
            raise JurisdictionError("无权升级该行政区预警")

        old_level = warning["level"]
        warning["level"] = new_level
        warning["headline"] = headline
        warning["basis"] = [
            {
                "observation_id": obs["id"],
                "kind": obs["kind"],
                "value": obs["value"],
                "unit": obs["unit"],
                "observed_at": obs["observed_at"],
                "source": obs["source"],
                "recorded_by": obs["recorded_by"],
            }
            for obs in basis
        ]
        # 升级视为新的风险，需重新确认；历史确认记录保留在审计中
        warning["status"] = "issued"
        warning["confirmed_by"] = None
        warning["confirmed_at"] = None
        warning["escalation_level"] = 0
        warning["confirm_deadline"] = self._add_minutes(self.clock.now(), CONFIRM_TIMEOUT_MINUTES)
        self._audit(
            actor_id,
            "upgrade_warning",
            warning_id,
            detail={"from": old_level, "to": new_level, "basis": [obs["id"] for obs in basis]},
        )
        self._notify_warning(warning, "upgraded")
        self._persist()
        return warning

    def confirm_warning(self, actor_id: str, warning_id: str) -> dict:
        """确认收到预警。"""
        actor = self._actor(actor_id)
        warning = self._get_warning(warning_id)
        if warning["status"] not in ("issued",):
            raise StateError(f"预警状态为{warning['status']}，无需确认")
        if not self._is_jurisdiction(actor, warning["region"]):
            raise JurisdictionError("无权确认该行政区预警")
        warning["status"] = "confirmed"
        warning["confirmed_by"] = actor_id
        warning["confirmed_at"] = self.clock.now()
        self._audit(actor_id, "confirm_warning", warning_id, detail={"level": warning["level"]})
        self._persist()
        return warning

    def request_release(self, actor_id: str, warning_id: str, reason: str) -> dict:
        """提出解除申请；发布人不能批准自己的解除。"""
        actor = self._actor(actor_id)
        warning = self._get_warning(warning_id)
        if warning["status"] not in ("issued", "confirmed"):
            raise StateError(f"预警状态为{warning['status']}，不能申请解除")
        if not self._is_jurisdiction(actor, warning["region"]):
            raise JurisdictionError("无权申请解除该行政区预警")
        warning["release_requested_by"] = actor_id
        warning["release_requested_at"] = self.clock.now()
        warning["release_reason"] = reason
        self._audit(actor_id, "request_release", warning_id, detail={"reason": reason})
        self._persist()
        return warning

    def approve_release(self, actor_id: str, warning_id: str, approved: bool, comment: str = "") -> dict:
        """批准/驳回解除申请。批准后预警解除并生成复盘任务。"""
        actor = self._actor(actor_id)
        warning = self._get_warning(warning_id)
        if warning["release_requested_by"] is None:
            raise StateError("尚无解除申请")
        if warning["status"] not in ("issued", "confirmed"):
            raise StateError(f"预警状态为{warning['status']}，不能处理解除申请")
        # 职责分离：发布人不能批准自己的解除
        if warning["issued_by"] == actor_id:
            raise SegregationError("发布人不能批准自己发布预警的解除")
        if not self._is_jurisdiction(actor, warning["region"]):
            raise JurisdictionError("无权批准该行政区预警解除")

        if approved:
            warning["status"] = "released"
            warning["released_by"] = actor_id
            warning["released_at"] = self.clock.now()
            self._audit(actor_id, "approve_release", warning_id, detail={"approved": True, "comment": comment})
            self._notify_warning(warning, "released")
            self._open_review(warning, reason="released")
        else:
            warning["release_requested_by"] = None
            warning["release_requested_at"] = None
            warning["release_reason"] = None
            self._audit(actor_id, "approve_release", warning_id, detail={"approved": False, "comment": comment})
        self._persist()
        return warning

    # ------------------------------------------------------------------ 通知

    def _notify_warning(self, warning: dict, event: str) -> None:
        """向相关方发送短信/电话通知；失败只留待办，不改风险状态。"""
        region = warning["region"]
        recipients = [
            actor
            for actor in self.state["actors"].values()
            if self._is_jurisdiction(actor, region)
        ]
        for actor in recipients:
            for channel in ("sms", "phone"):
                self._send_notification(
                    channel,
                    actor["id"],
                    actor["name"],
                    region,
                    warning["id"],
                    f"{LEVEL_CN[warning['level']]}山洪预警{event}：{warning['headline']}（{warning['valid_from']} 至 {warning['valid_until']}）",
                )

    def _send_notification(
        self, channel: str, actor_id: str, actor_name: str, region: str, warning_id: str, content: str
    ) -> dict:
        notif = {
            "id": self._next_id("ntf"),
            "channel": channel,
            "actor_id": actor_id,
            "region": region,
            "warning_id": warning_id,
            "content": content,
            "status": "pending",
            "attempts": 0,
            "created_at": self.clock.now(),
            "sent_at": None,
        }
        self.state["notifications"].append(notif)
        self._attempt_send(notif)
        return notif

    def _attempt_send(self, notif: dict) -> None:
        notif["attempts"] += 1
        try:
            ok = self.notifier(notif["channel"], notif["actor_id"], notif["content"]) if self.notifier else True
        except Exception as exc:  # 通知渠道自身异常也按失败处理
            ok = False
            notif["last_error"] = str(exc)
        if ok:
            notif["status"] = "sent"
            notif["sent_at"] = self.clock.now()
            notif.pop("last_error", None)
        else:
            notif["status"] = "failed"
            exists = any(
                todo["kind"] == "notification_failed"
                and todo["target"] == notif["id"]
                and todo["status"] == "open"
                for todo in self.state["todos"]
            )
            if not exists:
                self._add_todo(
                    "notification_failed",
                    notif["id"],
                    notif["region"],
                    f"{notif['channel']} 通知发送失败（{notif['actor_id']}），待重试",
                )

    def retry_notification(self, notification_id: str) -> dict:
        for notif in self.state["notifications"]:
            if notif["id"] == notification_id:
                if notif["status"] != "failed":
                    raise StateError(f"通知状态为{notif['status']}，无需重试")
                self._attempt_send(notif)
                if notif["status"] == "sent":
                    self._close_todos("notification_failed", notif["id"])
                self._persist()
                return notif
        raise NotFoundError(f"通知不存在：{notification_id}")

    # ------------------------------------------------------------------ 待办

    def _add_todo(self, kind: str, target: str, region: str, summary: str) -> dict:
        todo = {
            "id": self._next_id("todo"),
            "kind": kind,
            "target": target,
            "region": region,
            "summary": summary,
            "status": "open",
            "created_at": self.clock.now(),
            "closed_at": None,
        }
        self.state["todos"].append(todo)
        return todo

    def _close_todos(self, kind: str, target: str) -> None:
        for todo in self.state["todos"]:
            if todo["kind"] == kind and todo["target"] == target and todo["status"] == "open":
                todo["status"] = "closed"
                todo["closed_at"] = self.clock.now()

    def open_todos(self) -> list[dict]:
        return [todo for todo in self.state["todos"] if todo["status"] == "open"]

    # ------------------------------------------------------------------ 转移

    def create_transfer_task(
        self, actor_id: str, region: str, warning_id: str, target_count: int, destination: str
    ) -> dict:
        """创建人员转移任务。

        每个任务自带一本按人员唯一编号去重的台账；相邻区域的任务经省级
        发起联动后合并台账，跨区域上报同一批人员不会重复计数。
        任务必须挂在本区域生效中的预警下。
        """
        actor = self._actor(actor_id)
        warning = self._get_warning(warning_id)
        if warning["region"] != region:
            raise ValidationError("转移任务必须挂在本行政区的预警下")
        if warning["status"] not in ("issued", "confirmed"):
            raise StateError(f"预警已{warning['status']}，不再创建转移任务")
        if target_count <= 0:
            raise ValidationError("转移目标人数必须为正整数")
        if not self._is_jurisdiction(actor, region):
            raise JurisdictionError(f"无权在{REGION_CN.get(region, region)}创建转移任务")
        if any(t["warning_id"] == warning_id and t["region"] == region for t in self.state["transfer_ledger"]["tasks"]):
            raise StateError("该区域在此预警下已有转移任务")
        group_id = self._next_id("grp")
        self.state["transfer_ledger"]["groups"][group_id] = {"task_ids": [], "persons": {}}
        task = {
            "id": self._next_id("trf"),
            "warning_id": warning_id,
            "region": region,
            "destination": destination,
            "target_count": target_count,
            "counted_persons": [],
            "group_id": group_id,
            "status": "open",
            "created_by": actor_id,
            "created_at": self.clock.now(),
            "completed_at": None,
            "overdue": False,
        }
        self.state["transfer_ledger"]["tasks"].append(task)
        self.state["transfer_ledger"]["groups"][group_id]["task_ids"].append(task["id"])
        self._audit(actor_id, "create_transfer_task", task["id"], detail={"region": region, "target": target_count})
        self._persist()
        return task

    def _find_transfer_task(self, task_id: str) -> dict:
        task = next((t for t in self.state["transfer_ledger"]["tasks"] if t["id"] == task_id), None)
        if task is None:
            raise NotFoundError(f"转移任务不存在：{task_id}")
        return task

    def link_transfer(self, actor_id: str, task_id: str, neighbor_task_id: str) -> dict:
        """省级人员发起跨区联动：把相邻区域两本转移台账合并，人员不重复计数。"""
        actor = self._actor(actor_id)
        if actor["role"] != "province_duty":
            raise JurisdictionError("跨区联动转移只能由省级值班人员发起")
        task = self._find_transfer_task(task_id)
        neighbor = self._find_transfer_task(neighbor_task_id)
        if task["region"] == neighbor["region"]:
            raise ValidationError("联动转移必须面向相邻的不同行政区")
        if task["status"] != "open" or neighbor["status"] != "open":
            raise StateError("已闭环的转移任务不能再建立联动")

        keep_id, merge_id = task["group_id"], neighbor["group_id"]
        if keep_id != merge_id:
            groups = self.state["transfer_ledger"]["groups"]
            keep, merge = groups[keep_id], groups[merge_id]
            # 合并人员去重集：同一编号只保留首次登记记录
            for person_id, record in merge["persons"].items():
                keep["persons"].setdefault(person_id, record)
            for moved_id in merge["task_ids"]:
                moved = self._find_transfer_task(moved_id)
                moved["group_id"] = keep_id
                keep["task_ids"].append(moved_id)
            del groups[merge_id]
        self._audit(
            actor_id,
            "link_transfer",
            task_id,
            detail={"neighbor_task_id": neighbor_task_id, "group_id": task["group_id"]},
        )
        self._persist()
        return {"group_id": task["group_id"], "task_ids": list(self.state["transfer_ledger"]["groups"][task["group_id"]]["task_ids"])}

    def report_transfer(self, actor_id: str, task_id: str, person_ids: list[str]) -> dict:
        """上报已转移人员；同一联动台账内同一人员只计一次。"""
        actor = self._actor(actor_id)
        task = self._find_transfer_task(task_id)
        if task["status"] != "open":
            raise StateError(f"转移任务已{task['status']}")
        if not self._is_jurisdiction(actor, task["region"]):
            raise JurisdictionError("无权报告该行政区转移进度")
        if not person_ids:
            raise ValidationError("上报人员清单不能为空")
        group = self.state["transfer_ledger"]["groups"][task["group_id"]]
        persons = group["persons"]
        new_count = 0
        duplicate_count = 0
        for person_id in person_ids:
            if person_id in persons:
                duplicate_count += 1
                continue
            persons[person_id] = {
                "task_id": task_id,
                "region": task["region"],
                "recorded_by": actor_id,
                "recorded_at": self.clock.now(),
            }
            task["counted_persons"].append(person_id)
            new_count += 1
        if len(task["counted_persons"]) >= task["target_count"]:
            task["status"] = "completed"
            task["completed_at"] = self.clock.now()
            self._close_todos("transfer_overdue", task_id)
        self._audit(
            actor_id,
            "report_transfer",
            task_id,
            detail={
                "new": new_count,
                "duplicate": duplicate_count,
                "total_for_task": len(task["counted_persons"]),
                "target": task["target_count"],
            },
        )
        self._persist()
        return {"task": task, "new_count": new_count, "duplicate_count": duplicate_count}

    def transfer_rollup(self, warning_id: str) -> dict:
        """汇总某预警及其联动区域的转移情况（同一人员在整个联动台账内只计一次）。"""
        tasks = [t for t in self.state["transfer_ledger"]["tasks"] if t["warning_id"] == warning_id]
        if not tasks:
            raise NotFoundError(f"预警下没有转移任务：{warning_id}")
        groups = self.state["transfer_ledger"]["groups"]
        group_ids = {t["group_id"] for t in tasks}
        linked_tasks = [t for t in self.state["transfer_ledger"]["tasks"] if t["group_id"] in group_ids]
        regions = sorted({t["region"] for t in linked_tasks})
        unique_persons = set()
        for gid in group_ids:
            unique_persons.update(groups[gid]["persons"])
        target = sum(t["target_count"] for t in linked_tasks)
        open_tasks = sum(1 for t in linked_tasks if t["status"] == "open")
        return {
            "warning_id": warning_id,
            "linked_regions": regions,
            "target_count": target,
            "unique_transferred": len(unique_persons),
            "open_tasks": open_tasks,
            "completed": open_tasks == 0 and target > 0 and len(unique_persons) >= target,
        }

    # ------------------------------------------------------------------ 复盘

    def _open_review(self, warning: dict, reason: str) -> dict:
        review = {
            "id": self._next_id("revw"),
            "warning_id": warning["id"],
            "region": warning["region"],
            "reason": reason,
            "status": "open",
            "created_at": self.clock.now(),
            "due_at": self._add_minutes(self.clock.now(), REVIEW_TIMEOUT_MINUTES),
            "completed_at": None,
            "completed_by": None,
            "summary": None,
        }
        self.state["reviews"].append(review)
        self._audit("system", "open_review", review["id"], detail={"warning_id": warning["id"], "reason": reason})
        return review

    def complete_review(self, actor_id: str, review_id: str, summary: str) -> dict:
        actor = self._actor(actor_id)
        review = next((r for r in self.state["reviews"] if r["id"] == review_id), None)
        if review is None:
            raise NotFoundError(f"复盘任务不存在：{review_id}")
        if review["status"] != "open":
            raise StateError("复盘任务已完成")
        review["status"] = "completed"
        review["completed_at"] = self.clock.now()
        review["completed_by"] = actor_id
        review["summary"] = summary
        self._close_todos("review_overdue", review["id"])
        self._audit(actor_id, "complete_review", review_id, detail={"summary": summary})
        self._persist()
        return review

    # ------------------------------------------------------------------ 时钟推进

    @staticmethod
    def _add_minutes(iso: str, minutes: int) -> str:
        dt = datetime.fromisoformat(iso)
        return (dt + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()

    def advance(self, to_time: str) -> list[dict]:
        """推进模拟时钟，按原定时点处理过期、升级、逾期与复盘。返回处理事件清单。"""
        events: list[dict] = []
        self.clock.advance(to_time, reason="scheduled_tick")

        # 1) 未确认预警按升级策略逐级交接给上级（每级一个确认时限窗口）。
        #    升级只推进到预警失效时点为止，失效后的未确认不再交接。
        for warning in self.state["warnings"]:
            if warning["status"] != "issued":
                continue
            limit = min_iso(self.clock.now(), warning["valid_until"])
            while (
                limit >= warning["confirm_deadline"]
                and warning["escalation_level"] < len(ESCALATION_CHAIN) - 1
            ):
                warning["escalation_level"] += 1
                role = ESCALATION_CHAIN[warning["escalation_level"]]
                deadline = warning["confirm_deadline"]
                self._audit(
                    "system",
                    "escalate_confirmation",
                    warning["id"],
                    detail={"to_role": role, "level": warning["escalation_level"]},
                    at=deadline,
                )
                events.append({"type": "escalation", "warning_id": warning["id"], "to_role": role, "at": deadline})
                self._notify_role(warning, role, "未确认升级")
                warning["confirm_deadline"] = self._add_minutes(deadline, CONFIRM_TIMEOUT_MINUTES)

        # 2) 过期预警（仍未解除）终止风险状态并保留记录
        for warning in self.state["warnings"]:
            if warning["status"] in ("issued", "confirmed") and self.clock.now() >= warning["valid_until"]:
                warning["status"] = "expired"
                warning["expired_at"] = warning["valid_until"]
                self._audit(
                    "system",
                    "expire_warning",
                    warning["id"],
                    detail={"valid_until": warning["valid_until"]},
                    at=warning["valid_until"],
                )
                events.append({"type": "expired", "warning_id": warning["id"], "at": warning["valid_until"]})
                self._notify_warning(warning, "expired")
                self._open_review(warning, reason="expired")

        # 3) 超时未完成转移任务标记逾期
        for task in self.state["transfer_ledger"]["tasks"]:
            if task["status"] != "open" or task["overdue"]:
                continue
            deadline = self._add_minutes(task["created_at"], TRANSFER_TIMEOUT_MINUTES)
            if self.clock.now() >= deadline:
                task["overdue"] = True
                self._add_todo("transfer_overdue", task["id"], task["region"], f"转移任务逾期：{task['id']}")
                self._audit("system", "transfer_overdue", task["id"], at=deadline)
                events.append({"type": "transfer_overdue", "task_id": task["id"], "at": deadline})

        # 4) 到期未完成复盘（每条复盘任务只生成一次逾期待办）
        for review in self.state["reviews"]:
            if review["status"] != "open" or review.get("overdue"):
                continue
            if self.clock.now() >= review["due_at"]:
                review["overdue"] = True
                self._add_todo("review_overdue", review["id"], review["region"], f"复盘任务逾期：{review['id']}")
                self._audit("system", "review_overdue", review["id"], at=review["due_at"])
                events.append({"type": "review_overdue", "review_id": review["id"], "at": review["due_at"]})

        self._persist()
        return events

    def _notify_role(self, warning: dict, role: str, event: str) -> None:
        for actor in self.state["actors"].values():
            if actor["role"] != role:
                continue
            if not self._is_jurisdiction(actor, warning["region"]):
                continue
            self._send_notification(
                "sms",
                actor["id"],
                actor["name"],
                warning["region"],
                warning["id"],
                f"{LEVEL_CN[warning['level']]}山洪预警{event}：{warning['headline']}（{warning['valid_from']} 至 {warning['valid_until']}）",
            )

    # ------------------------------------------------------------------ 查询

    def warning_trace(self, warning_id: str) -> dict:
        """预警全链路：谁在何时依据哪份观测作了什么决定。"""
        warning = self._get_warning(warning_id)
        obs_ids = {b["observation_id"] for b in warning["basis"]}
        obs_ids.update(s["observation_id"] for s in warning["revision_suggestions"])
        entries = [
            e
            for e in self.state["audit"]
            if e["target"] == warning_id or e["target"] in obs_ids
        ]
        observations = [o for o in self.state["observations"] if o["id"] in obs_ids]
        return {
            "warning": warning,
            "basis_observations": observations,
            "audit": entries,
            "revision_suggestions": warning["revision_suggestions"],
        }

    def open_loops(self) -> dict:
        """尚未闭环的人员与区域。"""
        open_warnings = [
            {"id": w["id"], "region": w["region"], "level": w["level"], "status": w["status"]}
            for w in self.state["warnings"]
            if w["status"] in ("issued", "confirmed")
        ]
        open_transfers = [
            {
                "id": t["id"],
                "warning_id": t["warning_id"],
                "region": t["region"],
                "target": t["target_count"],
                "transferred": len(t["counted_persons"]),
                "overdue": t["overdue"],
            }
            for t in self.state["transfer_ledger"]["tasks"]
            if t["status"] == "open"
        ]
        open_reviews = [
            {"id": r["id"], "warning_id": r["warning_id"], "region": r["region"], "due_at": r["due_at"]}
            for r in self.state["reviews"]
            if r["status"] == "open"
        ]
        return {
            "open_warnings": open_warnings,
            "open_transfers": open_transfers,
            "open_reviews": open_reviews,
            "open_todos": self.open_todos(),
        }
