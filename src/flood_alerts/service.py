"""山洪预警闭环处置服务。

一条预警的生命周期：

    issued ──升级(可多次，升级后需重新确认)──▶ confirmed ──解除申请/批准──▶ released
      │  (超时未确认自动上交)                              │
      └────────────────────过期(expired)──────────────────┘
                            │
                            └──▶ 复盘任务(review)按原定时点推进

设计要点（对应领域约束）：

1. 证据锁定：issued/upgraded 时把触发依据（观测快照、行政区、有效时段）
   复制进事件，之后迟到观测只产生 revision_suggested，绝不改写历史通知。
2. 状态只由事件驱动：服务重启后重放 events.log 重建状态，并按事件中的
   原定时点重新登记定时器，时钟恢复到 meta.json 保存的时刻。
3. 权限：乡镇只能处置辖区；跨区域联动转移必须省级；发布人不能批准自己的解除。
4. 通知失败只留待办（notification_failed），不改变风险状态；待办可重试办结。
5. 联动转移按人员去重，避免相邻区域重复计数。
6. 过期、未完成转移、复盘均为定时器，按原定时点推进，重启不丢、不重复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import events as E
from .catalog import Catalog, User
from .clock import Clock, format_time, parse_time
from .notifications import NotificationGateway
from .observations import Observation, suggest_level
from .store import EventStore

LEVEL_ORDER = ["blue", "yellow", "orange", "red"]
LEVEL_RANK = {name: i for i, name in enumerate(LEVEL_ORDER)}
CONFIRM_TIMEOUT = timedelta(hours=2)
TRANSFER_TIMEOUT = timedelta(hours=6)
REVIEW_DELAY = timedelta(hours=24)
# 观测时间早于入库时间超过该差值视为迟到，只能形成修订建议。
LATE_GRACE = timedelta(minutes=10)


class PermissionError_(Exception):
    """操作用户无权执行该命令。"""


class ConflictError(Exception):
    """业务状态冲突（重复发布、重复确认、解除未批准等）。"""


class NotFoundError(Exception):
    """引用的预警/转移/观测不存在。"""


@dataclass
class AlertState:
    alert_id: str
    level: str
    area_code: str
    valid_from: datetime
    valid_until: datetime
    issued_by: str
    issued_at: datetime
    status: str = "issued"  # issued / confirmed / released / expired
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    last_level_at: datetime | None = None  # 最近一次发布/升级时间（确认超时起点）
    confirm_area: str | None = None  # 当前应确认的值守层级；超时后沿目录上移
    released_by: str | None = None
    released_at: datetime | None = None
    release_requested_by: str | None = None
    release_requested_at: datetime | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TransferState:
    transfer_id: str
    alert_id: str
    person_id: str
    person_name: str
    from_area: str
    to_area: str
    linked: bool
    started_at: datetime
    status: str = "started"  # started / completed / overdue
    completed_at: datetime | None = None


@dataclass
class ReviewState:
    review_id: str
    alert_id: str
    due_at: datetime
    status: str = "open"  # open / completed
    notified: bool = False  # 到点提醒是否已发（REVIEW_DUE 事件保证一次性）
    completed_at: datetime | None = None
    completed_by: str | None = None


@dataclass
class RevisionState:
    revision_id: str
    alert_id: str
    observation_id: str
    suggestion: str
    reason: str
    raised_at: datetime
    status: str = "open"  # open / resolved
    resolved_at: datetime | None = None


@dataclass
class NotificationTodo:
    todo_id: str
    alert_id: str
    channel: str
    target: str
    content: str
    created_at: datetime
    status: str = "open"  # open / resolved
    attempts: int = 1
    last_error: str | None = None
    resolved_at: datetime | None = None


class AlertService:
    def __init__(
        self,
        store: EventStore,
        catalog: Catalog,
        gateway: NotificationGateway | None = None,
        clock: Clock | None = None,
        start_time: str = "2026-09-27T08:00:00Z",
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.gateway = gateway or NotificationGateway()
        self.clock = clock or Clock(start_time)
        self.alerts: dict[str, AlertState] = {}
        self.transfers: dict[str, TransferState] = {}
        self.reviews: dict[str, ReviewState] = {}
        self.revisions: dict[str, RevisionState] = {}
        self.todos: dict[str, NotificationTodo] = {}
        self.observations: dict[str, Observation] = {}
        self._seq = 0
        # 先恢复时钟到停机前时刻，再重放事件，最后按原定时点重排定时器。
        self._restore_clock()
        self._replay()

    # ============ 重启恢复 ============
    def _restore_clock(self) -> None:
        meta = self.store.load_meta()
        if "now" in meta:
            saved = parse_time(meta["now"])
            if saved > self.clock.now():
                self.clock = Clock(saved)

    def _replay(self) -> None:
        for raw in self.store.read_all():
            self._apply(raw)
        for alert in self.alerts.values():
            self._arm_alert_timers(alert)
        for transfer in self.transfers.values():
            self._arm_transfer_timer(transfer)
        for review in self.reviews.values():
            self._arm_review_timer(review)

    # ============ 事件落库 ============
    def _emit(self, type_: str, actor: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        event = {"seq": self._seq, "type": type_, "at": self.clock.iso(), "actor": actor, "payload": payload}
        self.store.append(event)
        self._apply(event)
        self.store.save_meta({"now": self.clock.iso(), "last_seq": self._seq})
        return event

    def _apply(self, event: dict[str, Any]) -> None:
        self._seq = max(self._seq, event["seq"])
        p = event["payload"]
        t = event["type"]
        at = parse_time(event["at"])

        if t == E.OBSERVATION_RECEIVED:
            self.observations[p["obs_id"]] = Observation(
                obs_id=p["obs_id"],
                kind=p["kind"],
                station=p["station"],
                area_code=p["area_code"],
                observed_at=parse_time(p["observed_at"]),
                value=p["value"],
                unit=p["unit"],
                reporter=p["reporter"],
                received_at=at,
            )

        elif t in (E.ALERT_ISSUED, E.ALERT_UPGRADED):
            alert = self.alerts.get(p["alert_id"])
            if alert is None:
                alert = AlertState(
                    alert_id=p["alert_id"],
                    level=p["level"],
                    area_code=p["area_code"],
                    valid_from=parse_time(p["valid_from"]),
                    valid_until=parse_time(p["valid_until"]),
                    issued_by=p["issued_by"],
                    issued_at=at,
                )
                self.alerts[alert.alert_id] = alert
            else:
                # 升级后等级、有效期更新，需上级或本级重新确认；原通知保留在 history。
                alert.level = p["level"]
                alert.valid_until = parse_time(p["valid_until"])
                alert.status = "issued"
                alert.confirmed_by = None
                alert.confirmed_at = None
                alert.release_requested_by = None
                alert.release_requested_at = None
            alert.last_level_at = at
            alert.confirm_area = p["area_code"]
            alert.evidence = p["evidence"]
            alert.history.append(self._trace(event))

        elif t == E.ALERT_CONFIRMED:
            alert = self.alerts[p["alert_id"]]
            alert.status = "confirmed"
            alert.confirmed_by = event["actor"]
            alert.confirmed_at = at
            alert.history.append(self._trace(event))

        elif t == E.CONFIRMATION_ESCALATED:
            alert = self.alerts[p["alert_id"]]
            if p.get("to_area"):
                alert.confirm_area = p["to_area"]
            alert.history.append(self._trace(event))

        elif t == E.ALERT_EXPIRED:
            alert = self.alerts[p["alert_id"]]
            if alert.status not in ("released", "expired"):
                alert.status = "expired"
            alert.history.append(self._trace(event))

        elif t == E.ALERT_RELEASE_REQUESTED:
            alert = self.alerts[p["alert_id"]]
            alert.release_requested_by = event["actor"]
            alert.release_requested_at = at
            alert.history.append(self._trace(event))

        elif t == E.ALERT_RELEASE_REJECTED:
            alert = self.alerts[p["alert_id"]]
            alert.release_requested_by = None
            alert.release_requested_at = None
            alert.history.append(self._trace(event))

        elif t == E.ALERT_RELEASED:
            alert = self.alerts[p["alert_id"]]
            alert.status = "released"
            alert.released_by = event["actor"]
            alert.released_at = at
            alert.history.append(self._trace(event))

        elif t == E.TRANSFER_STARTED:
            self.transfers[p["transfer_id"]] = TransferState(
                transfer_id=p["transfer_id"],
                alert_id=p["alert_id"],
                person_id=p["person_id"],
                person_name=p["person_name"],
                from_area=p["from_area"],
                to_area=p["to_area"],
                linked=p.get("linked", False),
                started_at=at,
            )

        elif t == E.TRANSFER_COMPLETED:
            tr = self.transfers[p["transfer_id"]]
            tr.status = "completed"
            tr.completed_at = at

        elif t == E.TRANSFER_OVERDUE:
            tr = self.transfers[p["transfer_id"]]
            if tr.status != "completed":
                tr.status = "overdue"

        elif t == E.REVISION_SUGGESTED:
            self.revisions[p["revision_id"]] = RevisionState(
                revision_id=p["revision_id"],
                alert_id=p["alert_id"],
                observation_id=p["observation_id"],
                suggestion=p["suggestion"],
                reason=p["reason"],
                raised_at=at,
            )

        elif t == E.REVISION_RESOLVED:
            rev = self.revisions[p["revision_id"]]
            rev.status = "resolved"
            rev.resolved_at = at

        elif t == E.NOTIFICATION_FAILED:
            existing = self.todos.get(p["todo_id"])
            if existing is None:
                self.todos[p["todo_id"]] = NotificationTodo(
                    todo_id=p["todo_id"],
                    alert_id=p["alert_id"],
                    channel=p["channel"],
                    target=p["target"],
                    content=p["content"],
                    attempts=p.get("attempts", 1),
                    last_error=p.get("error"),
                    created_at=at,
                )
            else:
                # 同一待办的再次失败：累加尝试次数，不改变风险状态。
                existing.attempts = p.get("attempts", existing.attempts + 1)
                existing.last_error = p.get("error")

        elif t == E.NOTIFICATION_TODO_RESOLVED:
            todo = self.todos[p["todo_id"]]
            todo.status = "resolved"
            todo.resolved_at = at

        elif t == E.REVIEW_CREATED:
            self.reviews[p["review_id"]] = ReviewState(
                review_id=p["review_id"],
                alert_id=p["alert_id"],
                due_at=parse_time(p["due_at"]),
            )

        elif t == E.REVIEW_DUE:
            self.reviews[p["review_id"]].notified = True

        elif t == E.REVIEW_COMPLETED:
            review = self.reviews[p["review_id"]]
            review.status = "completed"
            review.completed_at = at
            review.completed_by = event["actor"]

    @staticmethod
    def _trace(event: dict[str, Any]) -> dict[str, Any]:
        return {"seq": event["seq"], "type": event["type"], "at": event["at"], "actor": event["actor"], "payload": event["payload"]}

    # ============ 定时器（按事件中的原定时点登记） ============
    def _arm_alert_timers(self, alert: AlertState) -> None:
        if alert.status in ("released", "expired"):
            return
        self.clock.schedule(
            alert.valid_until,
            lambda aid=alert.alert_id: self._expire_alert(aid),
            key=f"expire:{alert.alert_id}",
        )
        if alert.status == "issued":
            # 确认超时起点：最近一次升级之后；升级后再超时，则按最近一次
            # 自动上交事件的时点续算，保证重启后仍按原定时点推进。
            escalations = [h for h in alert.history if h["type"] == E.CONFIRMATION_ESCALATED]
            if escalations:
                last = escalations[-1]
                deadline = parse_time(last["at"]) + CONFIRM_TIMEOUT
                if last["payload"].get("to_area") is None:
                    # 已到省级并记录过终点事件，不再生成任何升级事件。
                    return
                # 时限已过却没有更上级可交：重启时不补排，避免重复升级。
                if deadline <= self.clock.now():
                    return
            else:
                deadline = (alert.last_level_at or alert.issued_at) + CONFIRM_TIMEOUT
            self.clock.schedule(
                deadline,
                lambda aid=alert.alert_id, dl=deadline: self._escalate_confirmation(aid, dl),
                key=f"confirm:{alert.alert_id}",
            )
    def _arm_transfer_timer(self, transfer: TransferState) -> None:
        # 已完成或已逾期的转移不再重排，避免重启后重复生成逾期事件。
        if transfer.status != "started":
            return
        deadline = transfer.started_at + TRANSFER_TIMEOUT
        self.clock.schedule(
            deadline,
            lambda tid=transfer.transfer_id, dl=deadline: self._transfer_overdue(tid, dl),
            key=f"transfer:{transfer.transfer_id}",
        )

    def _arm_review_timer(self, review: ReviewState) -> None:
        if review.status == "completed" or review.notified:
            return
        self.clock.schedule(
            review.due_at,
            lambda rid=review.review_id: self._review_due(rid),
            key=f"review:{review.review_id}",
        )

    def _expire_alert(self, alert_id: str) -> dict[str, Any] | None:
        alert = self.alerts.get(alert_id)
        if alert is None or alert.status in ("released", "expired"):
            return None
        event = self._emit(E.ALERT_EXPIRED, "system", {"alert_id": alert_id})
        self._create_review(alert)
        return event

    def _escalate_confirmation(self, alert_id: str, deadline: datetime) -> dict[str, Any] | None:
        alert = self.alerts.get(alert_id)
        if alert is None or alert.status != "issued" or self.clock.now() < deadline:
            return None
        target = self.catalog.handoff_target(from_area := (alert.confirm_area or alert.area_code))
        event = self._emit(
            E.CONFIRMATION_ESCALATED,
            "system",
            {"alert_id": alert_id, "from_area": from_area, "to_area": target, "reason": "值守人员未按时确认"},
        )
        if target is not None:
            alert.confirm_area = target
            next_deadline = self.clock.now() + CONFIRM_TIMEOUT
            self.clock.schedule(
                next_deadline,
                lambda aid=alert_id, dl=next_deadline: self._escalate_confirmation(aid, dl),
                key=f"confirm:{alert_id}",
            )
        self._notify(alert, "sms", target or from_area, f"【升级】{alert_id} 超时未确认，已交上级值守")
        return event

    def _transfer_overdue(self, transfer_id: str, deadline: datetime) -> dict[str, Any] | None:
        tr = self.transfers.get(transfer_id)
        if tr is None or tr.status == "completed" or self.clock.now() < deadline:
            return None
        return self._emit(E.TRANSFER_OVERDUE, "system", {"transfer_id": transfer_id})

    def _review_due(self, review_id: str) -> dict[str, Any] | None:
        review = self.reviews.get(review_id)
        if review is None or review.status == "completed" or review.notified:
            return None
        event = self._emit(E.REVIEW_DUE, "system", {"review_id": review_id})
        alert = self.alerts.get(review.alert_id)
        self._notify(alert, "sms", "duty-room", f"【复盘】{review.alert_id} 复盘任务已到点，请指挥员复盘闭环情况")
        return event

    def _create_review(self, alert: AlertState) -> None:
        review_id = f"RV-{alert.alert_id}"
        if review_id in self.reviews:
            return
        due = self.clock.now() + REVIEW_DELAY
        self._emit(E.REVIEW_CREATED, "system", {"review_id": review_id, "alert_id": alert.alert_id, "due_at": format_time(due)})
        self._arm_review_timer(self.reviews[review_id])

    # ============ 通知与待办 ============
    def _notify(self, alert: AlertState | None, channel: str, target: str, content: str) -> bool:
        ok = self.gateway.send(channel, target, content)
        if ok:
            return True
        # 失败只产生待办，绝不改动预警等级或风险状态。
        todo_id = f"NT-{self._seq + 1:04d}"
        self._emit(
            E.NOTIFICATION_FAILED,
            "system",
            {
                "todo_id": todo_id,
                "alert_id": alert.alert_id if alert else "",
                "channel": channel,
                "target": target,
                "content": content,
                "attempts": 1,
                "error": "网关返回失败",
            },
        )
        return False

    def retry_notification(self, user_id: str, todo_id: str) -> dict[str, Any]:
        self._user(user_id)
        todo = self.todos.get(todo_id)
        if todo is None:
            raise NotFoundError(f"待办不存在：{todo_id}")
        if todo.status == "resolved":
            raise ConflictError(f"待办已办结：{todo_id}")
        if self.gateway.send(todo.channel, todo.target, todo.content):
            event = self._emit(E.NOTIFICATION_TODO_RESOLVED, user_id, {"todo_id": todo_id})
            return {"todo_id": todo_id, "resolved": True, "event_seq": event["seq"]}
        # 再次失败仍只留待办（尝试次数落事件，重启可追溯），风险状态不变。
        self._emit(
            E.NOTIFICATION_FAILED,
            user_id,
            {
                "todo_id": todo_id,
                "alert_id": todo.alert_id,
                "channel": todo.channel,
                "target": todo.target,
                "content": todo.content,
                "attempts": todo.attempts + 1,
                "error": "网关仍返回失败",
            },
        )
        return {"todo_id": todo_id, "resolved": False, "attempts": todo.attempts}

    # ============ 权限 ============
    def _user(self, user_id: str) -> User:
        user = self.catalog.users.get(user_id)
        if user is None:
            raise NotFoundError(f"用户不存在：{user_id}")
        return user

    def _require_command(self, user: User, area_code: str) -> None:
        if not self.catalog.can_command(user, area_code):
            raise PermissionError_(f"{user.name} 无权处置 {area_code}（乡镇只能处理辖区）")

    # ============ 证据锁定 ============
    def _evidence(self, area_code: str, level: str, reason: str, refs: list[str]) -> dict[str, Any]:
        return {
            "area_code": area_code,
            "level": level,
            "reason": reason,
            "observation_ids": list(refs),
            "observations": [self.observations[oid].to_dict() for oid in refs],
            "evidence_locked_at": self.clock.iso(),
        }

    def _check_observations(self, refs: list[str]) -> None:
        if not refs:
            raise ConflictError("发布或升级预警必须引用至少一份观测作为触发依据")
        missing = [oid for oid in refs if oid not in self.observations]
        if missing:
            raise NotFoundError(f"观测不存在：{', '.join(missing)}")

    # ============ 命令：观测入库与迟到修订 ============
    def receive_observation(
        self,
        kind: str,
        station: str,
        area_code: str,
        value: float,
        unit: str,
        reporter: str,
        observed_at: str,
    ) -> dict[str, Any]:
        if kind not in ("rain", "water"):
            raise ValueError(f"未知观测类型：{kind}")
        if area_code not in self.catalog.areas:
            raise NotFoundError(f"行政区不存在：{area_code}")
        obs_dt = parse_time(observed_at)
        if obs_dt > self.clock.now():
            raise ConflictError("观测时间晚于当前时钟，不能提前录入")
        obs_id = f"OB-{len(self.observations) + 1:04d}"
        self._emit(
            E.OBSERVATION_RECEIVED,
            reporter,
            {
                "obs_id": obs_id,
                "kind": kind,
                "station": station,
                "area_code": area_code,
                "observed_at": observed_at,
                "value": value,
                "unit": unit,
                "reporter": reporter,
            },
        )
        late = self.clock.now() - obs_dt > LATE_GRACE
        revision = self._revise_on_late_data(self.observations[obs_id]) if late else None
        return {"obs_id": obs_id, "late": late, "revision": revision}

    def _revise_on_late_data(self, obs: Observation) -> dict[str, Any] | None:
        """迟到观测只可能形成修订建议，绝不改动已发布预警及其通知。"""
        candidates = [
            a for a in self.alerts.values()
            if a.status in ("issued", "confirmed")
            and self.catalog.contains(a.area_code, obs.area_code)
            and a.valid_from <= obs.observed_at <= a.valid_until
        ]
        if not candidates:
            return None
        if obs.kind == "rain":
            suggested = suggest_level(obs.value)
        else:
            # 水位观测值在演示中表示“距保证水位差值（米）”。
            suggested = "orange" if obs.value <= 0.5 else ("yellow" if obs.value <= 1.5 else None)
        if suggested is None:
            return None
        created: list[dict[str, Any]] = []
        for alert in candidates:
            if LEVEL_RANK[suggested] <= LEVEL_RANK[alert.level]:
                continue  # 迟到数据不支持更高等级时无需打扰
            rev_id = f"RV-{alert.alert_id}-{obs.obs_id}"
            if rev_id in self.revisions:
                continue
            self._emit(
                E.REVISION_SUGGESTED,
                "system",
                {
                    "revision_id": rev_id,
                    "alert_id": alert.alert_id,
                    "observation_id": obs.obs_id,
                    "suggestion": suggested,
                    "reason": f"迟到观测 {obs.obs_id}（观测于 {format_time(obs.observed_at)}）"
                              f"建议等级 {suggested}；预警 {alert.alert_id} 依据已锁定，需人工复核后决定是否升级",
                },
            )
            created.append({"revision_id": rev_id, "alert_id": alert.alert_id, "suggestion": suggested})
        return {"suggestions": created}

    def resolve_revision(self, user_id: str, revision_id: str, accept: bool, note: str = "") -> dict[str, Any]:
        user = self._user(user_id)
        rev = self.revisions.get(revision_id)
        if rev is None:
            raise NotFoundError(f"修订建议不存在：{revision_id}")
        if rev.status != "open":
            raise ConflictError(f"修订建议已处理：{revision_id}")
        self._require_command(user, self.alerts[rev.alert_id].area_code)
        event = self._emit(E.REVISION_RESOLVED, user_id, {"revision_id": revision_id, "accept": accept, "note": note})
        return {"revision_id": revision_id, "accept": accept, "event_seq": event["seq"]}

    # ============ 命令：发布 / 升级 / 确认 ============
    def issue_alert(
        self,
        user_id: str,
        area_code: str,
        level: str,
        valid_hours: float,
        reason: str,
        observation_ids: list[str],
    ) -> dict[str, Any]:
        user = self._user(user_id)
        self._require_command(user, area_code)
        if level not in LEVEL_RANK:
            raise ValueError(f"未知预警等级：{level}")
        self._check_observations(observation_ids)
        for alert in self.alerts.values():
            if alert.area_code == area_code and alert.status in ("issued", "confirmed"):
                raise ConflictError(f"{area_code} 已有未结束预警 {alert.alert_id}，等级变化请走升级")
        alert_id = f"AL-{len(self.alerts) + 1:04d}"
        start = self.clock.now()
        until = start + timedelta(hours=valid_hours)
        event = self._emit(
            E.ALERT_ISSUED,
            user_id,
            {
                "alert_id": alert_id,
                "level": level,
                "area_code": area_code,
                "valid_from": format_time(start),
                "valid_until": format_time(until),
                "issued_by": user_id,
                "evidence": self._evidence(area_code, level, reason, observation_ids),
            },
        )
        self._arm_alert_timers(self.alerts[alert_id])
        self._notify(self.alerts[alert_id], "sms", area_code, f"【预警发布】{area_code} {level} 级山洪预警，依据：{reason}")
        return {"alert_id": alert_id, "event_seq": event["seq"], "valid_until": format_time(until)}

    def upgrade_alert(self, user_id: str, alert_id: str, level: str, reason: str, observation_ids: list[str], extend_hours: float = 0.0) -> dict[str, Any]:
        user = self._user(user_id)
        alert = self.alerts.get(alert_id)
        if alert is None:
            raise NotFoundError(f"预警不存在：{alert_id}")
        if alert.status in ("released", "expired"):
            raise ConflictError(f"预警已{alert.status}，不能升级")
        self._require_command(user, alert.area_code)
        if LEVEL_RANK[level] <= LEVEL_RANK[alert.level]:
            raise ConflictError(f"升级后等级必须严格高于当前 {alert.level}")
        self._check_observations(observation_ids)
        until = max(alert.valid_until, self.clock.now() + timedelta(hours=extend_hours)) if extend_hours else alert.valid_until
        event = self._emit(
            E.ALERT_UPGRADED,
            user_id,
            {
                "alert_id": alert_id,
                "level": level,
                "area_code": alert.area_code,
                "valid_from": format_time(alert.valid_from),
                "valid_until": format_time(until),
                "issued_by": alert.issued_by,
                "evidence": self._evidence(alert.area_code, level, reason, observation_ids),
            },
        )
        self._arm_alert_timers(alert)
        self._notify(alert, "sms", alert.area_code, f"【预警升级】{alert_id} 升至 {level} 级，依据：{reason}")
        return {"alert_id": alert_id, "level": level, "event_seq": event["seq"]}

    def confirm_alert(self, user_id: str, alert_id: str) -> dict[str, Any]:
        user = self._user(user_id)
        alert = self.alerts.get(alert_id)
        if alert is None:
            raise NotFoundError(f"预警不存在：{alert_id}")
        if alert.status != "issued":
            raise ConflictError(f"预警 {alert_id} 当前状态 {alert.status}，无需确认")
        # 超时上交后，只能由当前值守层级（或其上级）确认，原乡镇不能再确认。
        duty_area = alert.confirm_area or alert.area_code
        if not self.catalog.can_command(user, duty_area):
            raise PermissionError_(f"{user.name} 无权确认已上交至 {duty_area} 的预警")
        event = self._emit(E.ALERT_CONFIRMED, user_id, {"alert_id": alert_id, "confirmed_by": user_id})
        self.clock.cancel(f"confirm:{alert_id}")
        self._notify(alert, "sms", alert.area_code, f"【已确认】{alert_id} 由 {user.name} 确认")
        return {"alert_id": alert_id, "confirmed_by": user_id, "event_seq": event["seq"]}

    # ============ 命令：人员转移（联动去重） ============
    def start_transfer(
        self,
        user_id: str,
        alert_id: str,
        person_id: str,
        person_name: str,
        from_area: str,
        to_area: str,
        linked: bool = False,
    ) -> dict[str, Any]:
        user = self._user(user_id)
        alert = self.alerts.get(alert_id)
        if alert is None:
            raise NotFoundError(f"预警不存在：{alert_id}")
        if alert.status in ("released", "expired"):
            raise ConflictError(f"预警已{alert.status}，不能组织转移")
        if from_area == to_area:
            raise ConflictError("转移迁出区与迁入区相同")
        if linked:
            if not self.catalog.can_cross_region(user):
                raise PermissionError_("跨区域联动转移必须由省级人员发起")
        elif not self.catalog.can_command(user, from_area):
            raise PermissionError_(f"{user.name} 无权在 {from_area} 组织转移")
        for tr in self.transfers.values():
            if tr.alert_id == alert_id and tr.person_id == person_id and tr.status != "completed":
                raise ConflictError(f"人员 {person_id} 在预警 {alert_id} 下已有未完成转移 {tr.transfer_id}，联动不重复计数")
        transfer_id = f"TR-{len(self.transfers) + 1:04d}"
        event = self._emit(
            E.TRANSFER_STARTED,
            user_id,
            {
                "transfer_id": transfer_id,
                "alert_id": alert_id,
                "person_id": person_id,
                "person_name": person_name,
                "from_area": from_area,
                "to_area": to_area,
                "linked": linked,
            },
        )
        self._arm_transfer_timer(self.transfers[transfer_id])
        self._notify(alert, "sms", to_area, f"【转移】{person_name}（{person_id}）由 {from_area} 转入，请接应")
        return {"transfer_id": transfer_id, "event_seq": event["seq"]}

    def complete_transfer(self, user_id: str, transfer_id: str) -> dict[str, Any]:
        self._user(user_id)
        tr = self.transfers.get(transfer_id)
        if tr is None:
            raise NotFoundError(f"转移不存在：{transfer_id}")
        if tr.status == "completed":
            raise ConflictError(f"转移已完成：{transfer_id}")
        event = self._emit(E.TRANSFER_COMPLETED, user_id, {"transfer_id": transfer_id})
        self.clock.cancel(f"transfer:{transfer_id}")
        return {"transfer_id": transfer_id, "status": "completed", "event_seq": event["seq"]}

    # ============ 命令：解除（申请 + 批准，发布人回避） ============
    def request_release(self, user_id: str, alert_id: str, reason: str) -> dict[str, Any]:
        user = self._user(user_id)
        alert = self.alerts.get(alert_id)
        if alert is None:
            raise NotFoundError(f"预警不存在：{alert_id}")
        if alert.status in ("released", "expired"):
            raise ConflictError(f"预警已{alert.status}，不能解除")
        self._require_command(user, alert.area_code)
        if alert.release_requested_by is not None:
            raise ConflictError(f"预警 {alert_id} 已在解除审批中")
        open_people = [tr for tr in self.transfers.values() if tr.alert_id == alert_id and tr.status != "completed"]
        if open_people:
            raise ConflictError(f"仍有 {len(open_people)} 名人员未完成转移（含逾期），不能申请解除")
        event = self._emit(E.ALERT_RELEASE_REQUESTED, user_id, {"alert_id": alert_id, "reason": reason})
        return {"alert_id": alert_id, "event_seq": event["seq"]}

    def approve_release(self, user_id: str, alert_id: str, agree: bool, reason: str = "") -> dict[str, Any]:
        user = self._user(user_id)
        alert = self.alerts.get(alert_id)
        if alert is None:
            raise NotFoundError(f"预警不存在：{alert_id}")
        if alert.status in ("released", "expired"):
            raise ConflictError(f"预警已{alert.status}")
        if alert.release_requested_by is None:
            raise ConflictError(f"预警 {alert_id} 尚无解除申请")
        if not agree:
            event = self._emit(E.ALERT_RELEASE_REJECTED, user_id, {"alert_id": alert_id, "reason": reason})
            return {"alert_id": alert_id, "approved": False, "event_seq": event["seq"]}
        if alert.issued_by == user_id:
            raise PermissionError_("发布人不能批准自己发布预警的解除")
        if not self.catalog.can_command(user, alert.area_code):
            raise PermissionError_(f"{user.name} 无权批准 {alert.area_code} 的解除")
        event = self._emit(E.ALERT_RELEASED, user_id, {"alert_id": alert_id, "approved_by": user_id})
        self.clock.cancel(f"expire:{alert_id}")
        self._create_review(alert)
        self._notify(alert, "sms", alert.area_code, f"【解除】{alert_id} 已批准解除，转入复盘")
        return {"alert_id": alert_id, "approved": True, "event_seq": event["seq"]}

    # ============ 命令：复盘 ============
    def complete_review(self, user_id: str, review_id: str, note: str) -> dict[str, Any]:
        self._user(user_id)
        review = self.reviews.get(review_id)
        if review is None:
            raise NotFoundError(f"复盘任务不存在：{review_id}")
        if review.status != "open":
            raise ConflictError(f"复盘任务已完成：{review_id}")
        event = self._emit(E.REVIEW_COMPLETED, user_id, {"review_id": review_id, "note": note})
        self.clock.cancel(f"review:{review_id}")
        return {"review_id": review_id, "completed": True, "event_seq": event["seq"]}

    # ============ 模拟时钟推进 ============
    def advance(self, hours: float) -> list[dict[str, Any]]:
        results = self.clock.advance(timedelta(hours=hours))
        self.store.save_meta({"now": self.clock.iso(), "last_seq": self._seq})
        return [r for r in results if r is not None]

    # ============ 查询：谁在何时依据哪份观测作出决定 ============
    def decision_trail(self, alert_id: str) -> list[dict[str, Any]]:
        alert = self.alerts.get(alert_id)
        if alert is None:
            raise NotFoundError(f"预警不存在：{alert_id}")
        return list(alert.history)

    # ============ 查询：尚未闭环的人员和区域 ============
    def open_loops(self) -> dict[str, Any]:
        return {
            "open_transfers": [
                {
                    "transfer_id": tr.transfer_id,
                    "alert_id": tr.alert_id,
                    "person_id": tr.person_id,
                    "person_name": tr.person_name,
                    "from_area": tr.from_area,
                    "to_area": tr.to_area,
                    "status": tr.status,
                }
                for tr in self.transfers.values()
                if tr.status != "completed"
            ],
            "open_areas": [
                {
                    "alert_id": a.alert_id,
                    "area_code": a.area_code,
                    "level": a.level,
                    "status": a.status,
                    "valid_until": format_time(a.valid_until),
                }
                for a in self.alerts.values()
                if a.status in ("issued", "confirmed")
            ],
            "open_reviews": [
                {"review_id": r.review_id, "alert_id": r.alert_id, "due_at": format_time(r.due_at), "reminded": r.notified}
                for r in self.reviews.values()
                if r.status == "open"
            ],
            "open_revisions": [
                {
                    "revision_id": r.revision_id,
                    "alert_id": r.alert_id,
                    "observation_id": r.observation_id,
                    "suggestion": r.suggestion,
                }
                for r in self.revisions.values()
                if r.status == "open"
            ],
            "open_notification_todos": [
                {
                    "todo_id": t.todo_id,
                    "alert_id": t.alert_id,
                    "channel": t.channel,
                    "target": t.target,
                    "attempts": t.attempts,
                }
                for t in self.todos.values()
                if t.status == "open"
            ],
        }
