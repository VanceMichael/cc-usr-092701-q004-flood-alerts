"""山洪预警闭环处置服务测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.flood_alerts import (
    CONFIRM_TIMEOUT_MINUTES,
    FloodAlertService,
    JurisdictionError,
    SegregationError,
    StateError,
    ValidationError,
)

T0 = "2026-09-28T08:00:00"


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.svc = FloodAlertService.open(self.dir / "state.json", notifier=getattr(self, "notifier", None))
        self._register_actors()

    def _register_actors(self) -> None:
        s = self.svc
        s.register_actor("cmd_qingxi", "青溪指挥员", "commander", "town_qingxi")
        s.register_actor("cmd_baolun", "宝轮指挥员", "commander", "town_baolun")
        s.register_actor("hydro_qingxi", "青溪水文员", "hydrologist", "town_qingxi")
        s.register_actor("hydro_baolun", "宝轮水文员", "hydrologist", "town_baolun")
        s.register_actor("grid_qingxi", "青溪网格员", "grid_worker", "town_qingxi")
        s.register_actor("grid_baolun", "宝轮网格员", "grid_worker", "town_baolun")
        s.register_actor("county_duty", "县级值班", "county_duty", "county_guangyuan")
        s.register_actor("city_duty", "市级值班", "city_duty", "city_guangyuan")
        s.register_actor("province_duty", "省级值班", "province_duty", "province_sichuan")

    def rain(self, actor="hydro_qingxi", region="town_qingxi", at="2026-09-28T07:50:00", value=48.0):
        return self.svc.record_observation(actor, region, "rainfall_3h", value, "mm", at, "自动雨量站")

    def issue_yellow(self, actor="cmd_qingxi", region="town_qingxi", until="2026-09-28T12:00:00", **kw):
        obs = self.rain(region=region, **({"actor": f"hydro_{region.split('_')[1]}"} if region.startswith("town_") else {}))
        return self.svc.issue_warning(
            actor,
            region,
            "yellow",
            T0,
            until,
            [obs["id"]],
            "山区短时强降雨，注意山洪",
        )


class WarningLockingTest(ServiceCase):
    def test_issue_locks_window_region_and_basis(self):
        obs = self.rain(value=52.5)
        w = self.svc.issue_warning(
            "cmd_qingxi", "town_qingxi", "yellow", T0, "2026-09-28T12:00:00", [obs["id"]], "黄色预警"
        )
        self.assertEqual(w["region"], "town_qingxi")
        self.assertEqual(w["valid_from"], T0)
        self.assertEqual(w["valid_until"], "2026-09-28T12:00:00")
        self.assertEqual(w["basis"][0]["observation_id"], obs["id"])
        self.assertEqual(w["basis"][0]["value"], 52.5)
        self.assertEqual(w["status"], "issued")

    def test_requires_basis_and_valid_window(self):
        with self.assertRaises(ValidationError):
            self.svc.issue_warning("cmd_qingxi", "town_qingxi", "yellow", T0, T0, [], "无依据")
        obs = self.rain()
        with self.assertRaises(ValidationError):
            self.svc.issue_warning("cmd_qingxi", "town_qingxi", "yellow", T0, T0, [obs["id"]], "时段颠倒")

    def test_only_one_active_warning_per_region(self):
        self.issue_yellow()
        with self.assertRaises(StateError):
            self.issue_yellow()

    def test_basis_observation_must_match_region(self):
        obs_baolun = self.svc.record_observation(
            "hydro_baolun", "town_baolun", "rainfall_3h", 40, "mm",
            "2026-09-28T07:55:00", "宝轮雨量站",
        )
        with self.assertRaises(ValidationError):
            self.svc.issue_warning(
                "cmd_qingxi", "town_qingxi", "yellow", T0, "2026-09-28T12:00:00",
                [obs_baolun["id"]], "跨区依据",
            )


class UpgradeAndLateObservationTest(ServiceCase):
    def test_late_observation_only_suggests_revision(self):
        w = self.issue_yellow()
        before = dict(level=w["level"], headline=w["headline"], basis_ids=[b["observation_id"] for b in w["basis"]])

        late = self.svc.record_observation(
            "hydro_qingxi", "town_qingxi", "river_level", 6.2, "m",
            "2026-09-28T07:40:00", "补传：水位站延迟上报",
        )
        w2 = self.svc.state["warnings"][0]
        self.assertEqual(w2["level"], before["level"])
        self.assertEqual(w2["headline"], before["headline"])
        self.assertEqual([b["observation_id"] for b in w2["basis"]], before["basis_ids"])
        self.assertEqual(len(w2["revision_suggestions"]), 1)
        self.assertEqual(w2["revision_suggestions"][0]["observation_id"], late["id"])
        actions = [a["action"] for a in self.svc.state["audit"]]
        self.assertIn("revision_suggested", actions)

    def test_upgrade_preserves_history_and_requires_reconfirm(self):
        w = self.issue_yellow()
        self.svc.confirm_warning("grid_qingxi", w["id"])
        self.svc.advance("2026-09-28T08:20:00")
        new_obs = self.rain(at="2026-09-28T08:20:00", value=76.0)

        upgraded = self.svc.upgrade_warning(
            "cmd_qingxi", w["id"], "orange", [new_obs["id"]], "雨强加大，升级橙色"
        )
        self.assertEqual(upgraded["level"], "orange")
        self.assertEqual(upgraded["status"], "issued")
        self.assertIsNone(upgraded["confirmed_by"])
        self.assertEqual(upgraded["basis"][0]["value"], 76.0)
        # 原发布与原确认记录仍在审计链上，没有被抹掉
        actions = [(a["action"], a["at"]) for a in self.svc.state["audit"] if a["target"] == w["id"]]
        self.assertIn(("issue_warning", T0), actions)
        self.assertIn("confirm_warning", [a for a, _ in actions])
        self.assertIn("upgrade_warning", [a for a, _ in actions])

    def test_cannot_downgrade(self):
        w = self.issue_yellow()
        self.svc.advance("2026-09-28T08:10:00")
        new_obs = self.rain(at="2026-09-28T08:10:00", value=30.0)
        with self.assertRaises(ValidationError):
            self.svc.upgrade_warning("cmd_qingxi", w["id"], "blue", [new_obs["id"]], "降级")

    def test_blue_then_yellow_kept_as_separate_warnings(self):
        obs1 = self.rain(value=22.0)
        blue = self.svc.issue_warning(
            "cmd_qingxi", "town_qingxi", "blue", T0, "2026-09-28T08:20:00", [obs1["id"]], "蓝色预警"
        )
        self.svc.advance("2026-09-28T08:30:00")
        self.assertEqual(self.svc.state["warnings"][0]["status"], "expired")
        obs2 = self.rain(at="2026-09-28T08:25:00", value=61.0)
        yellow = self.svc.issue_warning(
            "cmd_qingxi", "town_qingxi", "yellow", "2026-09-28T08:30:00", "2026-09-28T12:00:00",
            [obs2["id"]], "雨强发展，转黄色",
        )
        self.assertNotEqual(blue["id"], yellow["id"])
        self.assertEqual(len(self.svc.state["warnings"]), 2)
        self.assertEqual(self.svc.warning_trace(blue["id"])["warning"]["level"], "blue")


class JurisdictionTest(ServiceCase):
    def test_town_cannot_touch_other_town(self):
        obs = self.svc.record_observation(
            "hydro_baolun", "town_baolun", "rainfall_3h", 40, "mm",
            "2026-09-28T07:55:00", "宝轮雨量站",
        )
        with self.assertRaises(JurisdictionError):
            self.svc.issue_warning(
                "cmd_qingxi", "town_baolun", "yellow", T0, "2026-09-28T12:00:00", [obs["id"]], "越权"
            )

    def test_county_may_handle_subordinate_town(self):
        obs = self.rain()
        w = self.svc.issue_warning(
            "county_duty", "town_qingxi", "yellow", T0, "2026-09-28T12:00:00", [obs["id"]], "县级代发"
        )
        self.svc.confirm_warning("county_duty", w["id"])
        self.assertEqual(self.svc.state["warnings"][0]["confirmed_by"], "county_duty")

    def test_town_cannot_link_transfer(self):
        w = self.issue_yellow()
        task = self.svc.create_transfer_task("grid_qingxi", "town_qingxi", w["id"], 50, "镇中心小学")
        with self.assertRaises(JurisdictionError):
            self.svc.link_transfer("grid_qingxi", task["id"], "trf-x")


class TransferDedupTest(ServiceCase):
    def _two_town_warnings_and_tasks(self):
        obs_q = self.rain(actor="hydro_qingxi", region="town_qingxi", value=50.0)
        wq = self.svc.issue_warning(
            "cmd_qingxi", "town_qingxi", "yellow", T0, "2026-09-28T12:00:00", [obs_q["id"]], "青溪黄色"
        )
        obs_b = self.svc.record_observation(
            "hydro_baolun", "town_baolun", "rainfall_3h", 47, "mm",
            "2026-09-28T07:55:00", "宝轮雨量站",
        )
        wb = self.svc.issue_warning(
            "cmd_baolun", "town_baolun", "yellow", T0, "2026-09-28T12:00:00", [obs_b["id"]], "宝轮黄色"
        )
        tq = self.svc.create_transfer_task("grid_qingxi", "town_qingxi", wq["id"], 50, "安置点A")
        tb = self.svc.create_transfer_task("grid_baolun", "town_baolun", wb["id"], 50, "安置点B")
        return wq, wb, tq, tb

    def test_linked_transfers_count_each_person_once(self):
        wq, wb, tq, tb = self._two_town_warnings_and_tasks()
        self.svc.link_transfer("province_duty", tq["id"], tb["id"])

        r1 = self.svc.report_transfer("grid_qingxi", tq["id"], [f"p{i}" for i in range(1, 51)])
        self.assertEqual(r1["new_count"], 50)
        # 宝轮上报中有 10 人已在青溪台账登记（边界村重复上报）
        r2 = self.svc.report_transfer("grid_baolun", tb["id"], [f"p{i}" for i in range(41, 91)])
        self.assertEqual(r2["new_count"], 40)
        self.assertEqual(r2["duplicate_count"], 10)

        rollup = self.svc.transfer_rollup(wq["id"])
        self.assertEqual(rollup["unique_transferred"], 90)
        self.assertEqual(rollup["target_count"], 100)
        self.assertEqual(sorted(rollup["linked_regions"]), ["town_baolun", "town_qingxi"])
        self.assertFalse(rollup["completed"])

    def test_transfer_task_must_belong_to_region_warning(self):
        w = self.issue_yellow()
        with self.assertRaises(ValidationError):
            self.svc.create_transfer_task("grid_baolun", "town_baolun", w["id"], 50, "安置点")


class EscalationTest(ServiceCase):
    def test_unconfirmed_warning_escalates_level_by_level(self):
        w = self.issue_yellow(until="2026-09-28T14:00:00")
        self.svc.advance("2026-09-28T08:30:00")
        self.svc.advance("2026-09-28T09:00:00")
        self.svc.advance("2026-09-28T09:30:00")
        roles = [
            a["detail"]["to_role"]
            for a in self.svc.state["audit"]
            if a["action"] == "escalate_confirmation"
        ]
        self.assertEqual(roles, ["county_duty", "city_duty", "province_duty"])
        # 已到升级链顶端，继续推进不再产生升级
        self.svc.advance("2026-09-28T10:00:00")
        roles = [
            a["detail"]["to_role"]
            for a in self.svc.state["audit"]
            if a["action"] == "escalate_confirmation"
        ]
        self.assertEqual(len(roles), 3)
        # 上级确认后状态闭环
        self.svc.confirm_warning("city_duty", w["id"])
        self.assertEqual(self.svc.state["warnings"][0]["status"], "confirmed")

    def test_escalation_stops_at_valid_until(self):
        # 生效窗口只有 20 分钟，短于 30 分钟确认时限，过期后不再升级
        self.issue_yellow(until="2026-09-28T08:20:00")
        events = self.svc.advance("2026-09-28T09:30:00")
        kinds = [e["type"] for e in events]
        self.assertNotIn("escalation", kinds)
        self.assertIn("expired", kinds)
        expiry = next(a for a in self.svc.state["audit"] if a["action"] == "expire_warning")
        self.assertEqual(expiry["at"], "2026-09-28T08:20:00")  # 按原定时点记录

    def test_confirmed_warning_does_not_escalate(self):
        w = self.issue_yellow(until="2026-09-28T14:00:00")
        self.svc.confirm_warning("grid_qingxi", w["id"])
        events = self.svc.advance("2026-09-28T10:00:00")
        self.assertFalse(any(e["type"] == "escalation" for e in events))


class ReleaseSegregationTest(ServiceCase):
    def test_issuer_cannot_approve_own_release(self):
        w = self.issue_yellow(actor="county_duty")
        self.svc.request_release("county_duty", w["id"], "降雨结束，水位回落")
        with self.assertRaises(SegregationError):
            self.svc.approve_release("county_duty", w["id"], True)
        # 发布人之外、且有管辖权的上级可以批准
        self.svc.approve_release("city_duty", w["id"], True, "同意解除")
        self.assertEqual(self.svc.state["warnings"][0]["status"], "released")
        self.assertEqual(self.svc.state["warnings"][0]["released_by"], "city_duty")
        self.assertEqual(len(self.svc.state["reviews"]), 1)

    def test_rejected_release_keeps_warning_active(self):
        w = self.issue_yellow()
        self.svc.request_release("grid_qingxi", w["id"], "申请解除")
        self.svc.approve_release("county_duty", w["id"], False, "仍有风险")
        warning = self.svc.state["warnings"][0]
        self.assertEqual(warning["status"], "confirmed" if warning["confirmed_by"] else "issued")
        self.assertIsNone(warning["release_requested_by"])


class NotificationFailureTest(ServiceCase):
    def notifier(self, channel: str, actor_id: str, content: str) -> bool:  # type: ignore[override]
        return not (channel == "phone" and actor_id == "grid_qingxi")

    def test_failed_notification_keeps_risk_and_creates_todo(self):
        w = self.issue_yellow()
        failed = [n for n in self.svc.state["notifications"] if n["status"] == "failed"]
        self.assertTrue(failed)
        # 发送失败不改变风险状态
        self.assertEqual(self.svc.state["warnings"][0]["status"], "issued")
        todos = self.svc.open_todos()
        self.assertTrue(any(t["kind"] == "notification_failed" for t in todos))

        # 渠道恢复后重试成功，待办关闭
        self.svc.notifier = lambda channel, actor_id, content: True
        notif = self.svc.retry_notification(failed[0]["id"])
        self.assertEqual(notif["status"], "sent")
        self.assertFalse(any(t["target"] == notif["id"] and t["status"] == "open" for t in self.svc.open_todos()))
        self.assertEqual(self.svc.state["warnings"][0]["status"], "issued")

    def test_repeated_failure_does_not_duplicate_todo(self):
        w = self.issue_yellow()
        failed = next(n for n in self.svc.state["notifications"] if n["status"] == "failed")
        self.svc.retry_notification(failed["id"])  # 仍然失败
        matching = [t for t in self.svc.state["todos"] if t["kind"] == "notification_failed" and t["target"] == failed["id"]]
        self.assertEqual(len(matching), 1)


class ClockScheduleTest(ServiceCase):
    def test_overdue_transfer_and_review_follow_original_schedule(self):
        w = self.issue_yellow(until="2026-09-28T12:00:00")
        task = self.svc.create_transfer_task("grid_qingxi", "town_qingxi", w["id"], 50, "安置点A")
        self.svc.report_transfer("grid_qingxi", task["id"], ["p1", "p2"])

        # 转移时限 120 分钟
        events = self.svc.advance("2026-09-28T11:00:00")
        self.assertIn("transfer_overdue", [e["type"] for e in events])
        overdue = next(a for a in self.svc.state["audit"] if a["action"] == "transfer_overdue")
        self.assertEqual(overdue["at"], "2026-09-28T10:00:00")

        # 县级批准解除 → 复盘任务生成，时限 24 小时
        self.svc.request_release("cmd_qingxi", w["id"], "雨停")
        self.svc.approve_release("county_duty", w["id"], True)
        review = self.svc.state["reviews"][0]
        self.assertEqual(review["due_at"], "2026-09-29T11:00:00")
        events = self.svc.advance("2026-09-29T11:30:00")
        self.assertIn("review_overdue", [e["type"] for e in events])
        self.assertTrue(any(t["kind"] == "review_overdue" for t in self.svc.open_todos()))

        # 完成复盘后逾期复盘待办仍提示一次，但复盘本身已闭环
        self.svc.complete_review("cmd_qingxi", review["id"], "转移32人，预警提前30分钟发布")
        self.assertEqual(self.svc.state["reviews"][0]["status"], "completed")


class RecoveryAndTraceTest(ServiceCase):
    def test_restart_preserves_trace_and_open_loops(self):
        w = self.issue_yellow(until="2026-09-28T12:00:00")
        self.svc.confirm_warning("grid_qingxi", w["id"])
        self.svc.advance("2026-09-28T09:00:00")
        late_obs = self.svc.record_observation(
            "hydro_qingxi", "town_qingxi", "rainfall_3h", 64, "mm",
            "2026-09-28T08:30:00", "二次降雨",
        )
        task = self.svc.create_transfer_task("grid_qingxi", "town_qingxi", w["id"], 50, "安置点A")
        self.svc.report_transfer("grid_qingxi", task["id"], [f"p{i}" for i in range(1, 21)])

        # 模拟服务重启：重新从同一文件打开
        restarted = FloodAlertService.open(self.dir / "state.json")
        self.assertEqual(restarted.clock.now(), "2026-09-28T09:00:00")
        trace = restarted.warning_trace(w["id"])
        self.assertEqual(trace["warning"]["id"], w["id"])
        actions = [a["action"] for a in trace["audit"]]
        relevant = [
            a
            for a in actions
            if a in {"issue_warning", "confirm_warning", "revision_suggested", "record_observation"}
        ]
        # 依据观测登记 -> 发布 -> 确认 -> 迟到观测的修订建议，顺序可追溯
        self.assertEqual(relevant[0], "record_observation")
        self.assertIn("issue_warning", relevant)
        self.assertIn("confirm_warning", relevant)
        self.assertIn("revision_suggested", relevant)
        self.assertLess(relevant.index("revision_suggested"), len(relevant))
        basis_ids = {b["id"] for b in trace["basis_observations"]}
        self.assertIn(late_obs["id"], basis_ids)
        first_issue = next(a for a in trace["audit"] if a["action"] == "issue_warning")
        self.assertEqual(first_issue["actor_id"], "cmd_qingxi")
        self.assertEqual(first_issue["at"], T0)

        loops = restarted.open_loops()
        open_regions = {t["region"] for t in loops["open_transfers"]}
        self.assertIn("town_qingxi", open_regions)
        self.assertTrue(any(t["transferred"] == 20 and t["target"] == 50 for t in loops["open_transfers"]))

        # 继续完成转移并解除、复盘
        restarted.report_transfer("grid_qingxi", task["id"], [f"p{i}" for i in range(21, 51)])
        restarted.request_release("cmd_qingxi", w["id"], "结束")
        restarted.approve_release("county_duty", w["id"], True)
        review_id = restarted.state["reviews"][0]["id"]
        loops = restarted.open_loops()
        self.assertFalse(loops["open_transfers"])
        self.assertTrue(any(r["id"] == review_id for r in loops["open_reviews"]))
        restarted.complete_review("cmd_qingxi", review_id, "全部闭环")
        loops = restarted.open_loops()
        self.assertFalse(loops["open_warnings"])
        self.assertFalse(loops["open_transfers"])
        self.assertFalse(loops["open_reviews"])


if __name__ == "__main__":
    unittest.main()
