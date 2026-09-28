"""闭环处置服务的约束测试。

覆盖：证据锁定、迟到观测只修订不改史、蓝/黄先后升级、确认超时上交、
辖区权限、省级跨区联动、联动去重、发布人回避、通知失败待办、过期与
转移逾期、复盘到点、解除前人员闭环、重启重放可追溯。
"""

import tempfile
import unittest
from pathlib import Path

from src.flood_alerts.catalog import demo_catalog
from src.flood_alerts.notifications import NotificationGateway
from src.flood_alerts.service import (
    AlertService,
    ConflictError,
    NotFoundError,
    PermissionError_,
)
from src.flood_alerts.store import EventStore

T0 = "2026-09-27T08:00:00Z"
GF = "T42032201"   # 关防乡
HBK = "T42032202"  # 湖北口乡
FX = "T42032401"   # 丰溪镇（竹溪县）
YX = "C420322"     # 郧西县
ZX = "C420324"     # 竹溪县


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.gateway = NotificationGateway()
        self.svc = AlertService(EventStore(self.dir), demo_catalog(), self.gateway, start_time=T0)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def restart(self, gateway: NotificationGateway | None = None) -> AlertService:
        """模拟服务重启：新实例重放事件日志并恢复时钟。"""
        return AlertService(EventStore(self.dir), demo_catalog(), gateway or self.gateway, start_time=T0)

    def rain(self, area: str = GF, value: float = 45.0, when: str = "2026-09-27T07:55:00Z", reporter: str = "水文站") -> str:
        return self.svc.receive_observation("rain", "S1", area, value, "mm", reporter, when)["obs_id"]

    def issue(self, area: str = GF, level: str = "yellow", hours: float = 6.0, obs: list[str] | None = None, user: str = "u-gf") -> str:
        return self.svc.issue_alert(user, area, level, hours, f"1h雨量触发{level}", obs or [self.rain(area=area)])["alert_id"]


class IssueAndEvidenceTest(ServiceTestBase):
    def test_issue_locks_period_area_and_evidence(self) -> None:
        obs_id = self.rain(value=45.0, when="2026-09-27T07:55:00Z")
        out = self.svc.issue_alert("u-gf", GF, "yellow", 6, "1h雨量45mm", [obs_id])
        alert = self.svc.alerts[out["alert_id"]]
        self.assertEqual(alert.area_code, GF)
        self.assertEqual(out["valid_until"], "2026-09-27T14:00:00Z")
        ev = alert.evidence
        self.assertEqual(ev["area_code"], GF)
        self.assertEqual(ev["observation_ids"], [obs_id])
        self.assertEqual(ev["observations"][0]["obs_id"], obs_id)
        self.assertEqual(ev["observations"][0]["value"], 45.0)

    def test_issue_requires_observation(self) -> None:
        with self.assertRaises(ConflictError):
            self.svc.issue_alert("u-gf", GF, "yellow", 6, "无依据", [])
        with self.assertRaises(NotFoundError):
            self.svc.issue_alert("u-gf", GF, "yellow", 6, "假依据", ["OB-9999"])

    def test_same_area_second_issue_blocked_blue_then_yellow_is_upgrade(self) -> None:
        alert_id = self.issue(level="blue")
        with self.assertRaises(ConflictError):
            self.issue(level="yellow")
        self.svc.advance(1)
        ob2 = self.rain(value=52.0, when="2026-09-27T09:00:00Z")
        self.svc.upgrade_alert("u-gf", alert_id, "yellow", "雨势增强", [ob2])
        self.assertEqual(self.svc.alerts[alert_id].level, "yellow")
        # 历史通知仍可追溯：先蓝后黄两条记录都在。
        types = [h["type"] for h in self.svc.decision_trail(alert_id)]
        self.assertEqual(types, ["alert_issued", "alert_upgraded"])

    def test_late_observation_only_suggests_revision_never_rewrites_notice(self) -> None:
        alert_id = self.issue(level="yellow")
        original_evidence = self.svc.alerts[alert_id].evidence
        self.svc.advance(0.5)
        # 40 分钟前的强雨量迟到入库（超过 10 分钟宽限）。
        result = self.svc.receive_observation(
            "rain", "S2", GF, 85.0, "mm", "水文站", "2026-09-27T08:00:00Z"
        )
        self.assertTrue(result["late"])
        self.assertIsNotNone(result["revision"])
        rev_id = result["revision"]["suggestions"][0]["revision_id"]
        self.assertEqual(result["revision"]["suggestions"][0]["suggestion"], "red")
        # 已发通知与锁定依据不变。
        self.assertEqual(self.svc.alerts[alert_id].level, "yellow")
        self.assertEqual(self.svc.alerts[alert_id].evidence, original_evidence)
        self.assertEqual(self.svc.revisions[rev_id].status, "open")
        # 人工复核：接受建议后显式升级，历史仍然完整。
        self.svc.resolve_revision("u-yx", rev_id, accept=True, note="核实属实")
        self.assertEqual(self.svc.revisions[rev_id].status, "resolved")


class ConfirmationEscalationTest(ServiceTestBase):
    def test_unconfirmed_alert_hands_off_to_county_then_province(self) -> None:
        alert_id = self.issue(level="yellow")
        fired = self.svc.advance(2)
        self.assertTrue(any(e["type"] == "confirmation_escalated" for e in fired))
        esc = self.svc.decision_trail(alert_id)[-1]
        self.assertEqual(esc["payload"]["to_area"], YX)
        # 县级仍未确认，再过 2 小时上交省级。
        fired2 = self.svc.advance(2)
        self.assertTrue(any(e["type"] == "confirmation_escalated" for e in fired2))
        self.assertEqual(self.svc.decision_trail(alert_id)[-1]["payload"]["to_area"], "P42")
        # 预警状态在交接期间保持 issued（未确认），等级不被降级。
        self.assertEqual(self.svc.alerts[alert_id].status, "issued")
        self.assertEqual(self.svc.alerts[alert_id].level, "yellow")

    def test_confirmation_cancels_escalation(self) -> None:
        alert_id = self.issue(level="yellow")
        self.svc.advance(1)
        self.svc.confirm_alert("u-yx", alert_id)
        self.assertEqual(self.svc.alerts[alert_id].status, "confirmed")
        self.svc.advance(3)
        self.assertFalse(any(h["type"] == "confirmation_escalated" for h in self.svc.decision_trail(alert_id)))

    def test_upgrade_requires_reconfirm(self) -> None:
        alert_id = self.issue(level="blue")
        self.svc.confirm_alert("u-gf", alert_id)
        self.svc.advance(1.5)
        ob2 = self.rain(value=55.0, when="2026-09-27T09:30:00Z")
        self.svc.upgrade_alert("u-gf", alert_id, "yellow", "雨势增强", [ob2])
        self.assertEqual(self.svc.alerts[alert_id].status, "issued")
        # 升级后必须重新确认。
        self.svc.confirm_alert("u-gf", alert_id)
        self.assertEqual(self.svc.alerts[alert_id].status, "confirmed")


class PermissionTest(ServiceTestBase):
    def test_township_only_own_area(self) -> None:
        # 关防乡值守员不能在丰溪镇发布预警。
        ob = self.rain(area=FX)
        with self.assertRaises(PermissionError_):
            self.svc.issue_alert("u-gf", FX, "yellow", 6, "跨辖区", [ob])
        # 竹溪县指挥员可以在本县乡镇发布。
        self.svc.issue_alert("u-zx", FX, "yellow", 6, "本县调度", [self.rain(area=FX)])

    def test_only_province_can_start_cross_region_linked_transfer(self) -> None:
        alert_id = self.issue(level="yellow")
        with self.assertRaises(PermissionError_):
            self.svc.start_transfer("u-yx", alert_id, "P-001", "张三", GF, FX, linked=True)
        out = self.svc.start_transfer("u-prov", alert_id, "P-001", "张三", GF, FX, linked=True)
        self.assertTrue(self.svc.transfers[out["transfer_id"]].linked)

    def test_publisher_cannot_approve_own_release(self) -> None:
        alert_id = self.issue(user="u-yx", level="yellow")
        self.svc.confirm_alert("u-yx", alert_id)
        self.svc.request_release("u-yx", alert_id, "降雨减弱")
        with self.assertRaises(PermissionError_):
            self.svc.approve_release("u-yx", alert_id, agree=True)
        # 省级（非发布人）可批准。
        out = self.svc.approve_release("u-prov", alert_id, agree=True)
        self.assertTrue(out["approved"])
        self.assertEqual(self.svc.alerts[alert_id].status, "released")

    def test_release_reject_keeps_alert_active(self) -> None:
        alert_id = self.issue(user="u-yx", level="yellow")
        self.svc.confirm_alert("u-yx", alert_id)
        self.svc.request_release("u-yx", alert_id, "试探性申请")
        self.svc.approve_release("u-prov", alert_id, agree=False, reason="上游仍有降雨")
        self.assertEqual(self.svc.alerts[alert_id].status, "confirmed")
        self.assertIsNone(self.svc.alerts[alert_id].release_requested_by)


class TransferTest(ServiceTestBase):
    def test_linked_transfer_dedup(self) -> None:
        alert_id = self.issue(level="yellow")
        self.svc.start_transfer("u-gf", alert_id, "P-001", "张三", GF, HBK)
        # 省级发起相邻区域联动，同一人不能重复计数。
        with self.assertRaises(ConflictError):
            self.svc.start_transfer("u-prov", alert_id, "P-001", "张三", HBK, FX, linked=True)
        # 另一名村民可以联动。
        self.svc.start_transfer("u-prov", alert_id, "P-002", "李四", HBK, FX, linked=True)
        active = [t for t in self.svc.transfers.values() if t.status != "completed"]
        self.assertEqual(len(active), 2)

    def test_transfer_overdue_at_original_time(self) -> None:
        alert_id = self.issue(level="yellow")
        tr = self.svc.start_transfer("u-gf", alert_id, "P-001", "张三", GF, HBK)["transfer_id"]
        self.svc.advance(5)
        self.assertEqual(self.svc.transfers[tr].status, "started")
        self.svc.advance(1)
        self.assertEqual(self.svc.transfers[tr].status, "overdue")
        # 逾期人员仍计入未闭环。
        people = {t["person_id"] for t in self.svc.open_loops()["open_transfers"]}
        self.assertIn("P-001", people)

    def test_cannot_release_with_unfinished_transfer(self) -> None:
        alert_id = self.issue(level="yellow")
        self.svc.confirm_alert("u-gf", alert_id)
        tr = self.svc.start_transfer("u-gf", alert_id, "P-001", "张三", GF, HBK)["transfer_id"]
        with self.assertRaises(ConflictError):
            self.svc.request_release("u-gf", alert_id, "雨停了")
        self.svc.complete_transfer("u-hbk", tr)
        self.svc.request_release("u-gf", alert_id, "人员已全部安置")
        self.svc.approve_release("u-yx", alert_id, agree=True)
        self.assertEqual(self.svc.alerts[alert_id].status, "released")


class NotificationTest(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        # 关防乡目标的短信一律失败。
        self.gateway = NotificationGateway(lambda c, t, x: c == "sms" and t == GF)
        self.svc = AlertService(EventStore(self.dir), demo_catalog(), self.gateway, start_time=T0)

    def test_failed_send_becomes_todo_without_state_change(self) -> None:
        alert_id = self.issue(level="yellow")
        loops = self.svc.open_loops()
        self.assertEqual(len(loops["open_notification_todos"]), 1)
        todo_id = loops["open_notification_todos"][0]["todo_id"]
        self.assertEqual(self.svc.alerts[alert_id].level, "yellow")
        self.assertEqual(self.svc.alerts[alert_id].status, "issued")
        # 仍失败：待办保留，状态不变。
        again = self.svc.retry_notification("u-gf", todo_id)
        self.assertFalse(again["resolved"])
        self.assertEqual(self.svc.todos[todo_id].attempts, 2)
        # 网关恢复后重试成功，待办办结。
        self.gateway._fail = None  # type: ignore[attr-defined]
        ok = self.svc.retry_notification("u-gf", todo_id)
        self.assertTrue(ok["resolved"])
        self.assertEqual(self.svc.todos[todo_id].status, "resolved")
        self.assertEqual(self.svc.alerts[alert_id].status, "issued")


class TimerAndReviewTest(ServiceTestBase):
    def test_expiry_at_valid_until_then_review_due_24h(self) -> None:
        alert_id = self.issue(level="yellow", hours=3)
        self.svc.advance(2.9)
        self.assertEqual(self.svc.alerts[alert_id].status, "issued")
        self.svc.advance(0.2)
        self.assertEqual(self.svc.alerts[alert_id].status, "expired")
        review_id = f"RV-{alert_id}"
        self.assertIn(review_id, self.svc.reviews)
        # 复盘在过期后 24 小时到点，不提前。
        self.svc.advance(23)
        self.assertFalse(self.svc.reviews[review_id].notified)
        self.svc.advance(1.1)
        self.assertTrue(self.svc.reviews[review_id].notified)
        self.svc.complete_review("u-yx", review_id, "预警按时解除，转移闭环")
        self.assertEqual(self.svc.reviews[review_id].status, "completed")

    def test_released_alert_does_not_also_expire(self) -> None:
        alert_id = self.issue(level="yellow", hours=6)
        self.svc.confirm_alert("u-gf", alert_id)
        self.svc.request_release("u-gf", alert_id, "雨停")
        self.svc.approve_release("u-yx", alert_id, agree=True)
        self.svc.advance(8)
        self.assertEqual(self.svc.alerts[alert_id].status, "released")
        self.assertFalse(any(h["type"] == "alert_expired" for h in self.svc.decision_trail(alert_id)))


class RestartRecoveryTest(ServiceTestBase):
    def test_replay_shows_trail_and_open_loops_after_restart(self) -> None:
        obs_id = self.rain(value=45.0)
        alert_id = self.svc.issue_alert("u-gf", GF, "yellow", 6, "1h雨量45mm", [obs_id])["alert_id"]
        self.svc.confirm_alert("u-yx", alert_id)
        tr = self.svc.start_transfer("u-gf", alert_id, "P-001", "张三", GF, HBK)["transfer_id"]
        self.svc.advance(1)
        trail_before = self.svc.decision_trail(alert_id)
        loops_before = self.svc.open_loops()

        svc2 = self.restart()
        # 时钟恢复到停机时刻。
        self.assertEqual(svc2.clock.iso(), "2026-09-27T09:00:00Z")
        # 谁、何时、依据哪份观测：轨迹完整可追溯。
        self.assertEqual(svc2.decision_trail(alert_id), trail_before)
        issued = svc2.decision_trail(alert_id)[0]
        self.assertEqual(issued["actor"], "u-gf")
        self.assertEqual(issued["payload"]["evidence"]["observation_ids"], [obs_id])
        # 未闭环人员与区域一致。
        self.assertEqual(svc2.open_loops()["open_transfers"], loops_before["open_transfers"])
        self.assertEqual({a["alert_id"] for a in svc2.open_loops()["open_areas"]}, {alert_id})
        # 继续推进：转移在原定 6 小时时点逾期（09:00 起算，再走 6 小时余）。
        svc2.advance(6.1)
        self.assertEqual(svc2.transfers[tr].status, "overdue")

    def test_restart_does_not_duplicate_or_lose_timers(self) -> None:
        alert_id = self.issue(level="yellow", hours=4)
        self.svc.advance(1)  # 09:00
        svc = self.restart()
        svc.advance(3)  # 12:00：预警在原定 12:00 过期
        # 过期事件只出现一次。
        expiries = [h for h in svc.alerts[alert_id].history if h["type"] == "alert_expired"]
        self.assertEqual(len(expiries), 1)
        # 在重启后的实例上继续发布第二条预警，停机后再重启，升级不重不丢。
        ob2 = svc.receive_observation("rain", "S9", HBK, 25.0, "mm", "气象部门", "2026-09-27T11:50:00Z")["obs_id"]
        alert2 = svc.issue_alert("u-hbk", HBK, "blue", 8, "1h雨量25mm", [ob2])["alert_id"]
        svc.advance(1.5)  # 13:30，确认截止 14:00，尚未升级
        self.assertFalse(any(h["type"] == "confirmation_escalated" for h in svc.decision_trail(alert2)))
        svc = self.restart()  # 时钟恢复至 13:30
        before = len(svc.decision_trail(alert2))
        svc.advance(1)  # 14:30：越过 14:00，补一次且仅一次升级
        self.assertEqual([h["type"] for h in svc.decision_trail(alert2)].count("confirmation_escalated"), 1)
        self.assertEqual(len(svc.decision_trail(alert2)), before + 1)

    def test_overdue_not_duplicated_after_restart(self) -> None:
        alert_id = self.issue(level="yellow", hours=12)
        tr = self.svc.start_transfer("u-gf", alert_id, "P-001", "张三", GF, HBK)["transfer_id"]
        self.svc.advance(7)  # 已逾期
        self.assertEqual(self.svc.transfers[tr].status, "overdue")
        store = EventStore(self.dir)
        overdue_events_before = sum(
            1 for e in store if e["type"] == "transfer_overdue" and e["payload"]["transfer_id"] == tr
        )
        svc = self.restart()
        svc.advance(2)
        overdue_events_after = sum(
            1 for e in EventStore(self.dir) if e["type"] == "transfer_overdue" and e["payload"]["transfer_id"] == tr
        )
        self.assertEqual(overdue_events_before, 1)
        self.assertEqual(overdue_events_after, 1)

    def test_escalation_to_top_and_handoff_permission_survive_restart(self) -> None:
        alert_id = self.issue(level="yellow", hours=24)
        self.svc.advance(2)  # 10:00 上交县级
        self.svc.advance(2)  # 12:00 上交省级，仍未确认
        self.assertEqual(self.svc.alerts[alert_id].confirm_area, "P42")
        self.svc.advance(2)  # 省级时限也过，记录终点，不再有上级
        count_top = [h["type"] for h in self.svc.decision_trail(alert_id)].count("confirmation_escalated")
        svc = self.restart()
        # 再推进也不会对已到顶的预警重复生成升级事件。
        svc.advance(5)
        self.assertEqual(
            [h["type"] for h in svc.decision_trail(alert_id)].count("confirmation_escalated"), count_top
        )
        # 已上交省级后，原乡镇无权确认，省级可以。
        with self.assertRaises(PermissionError_):
            svc.confirm_alert("u-gf", alert_id)
        svc.confirm_alert("u-prov", alert_id)
        self.assertEqual(svc.alerts[alert_id].status, "confirmed")


if __name__ == "__main__":
    unittest.main()
