"""端到端演练：把发布、升级、确认、转移、解除、复盘串成可追溯闭环。

用法：

    python3 -m src.flood_alerts.demo [状态文件路径]

不传路径时使用临时文件。脚本全程在模拟时钟下运行，结尾会“重启服务”
并从磁盘恢复，打印决策追溯链与尚未闭环的人员和区域。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .errors import DomainError
from .service import LEVEL_CN, REGION_CN, FloodAlertService


def line(title: str) -> None:
    print(f"\n{'─' * 8} {title} {'─' * 8}")


def main(path: str | None = None) -> int:
    state_path = Path(path) if path else Path(tempfile.mkdtemp()) / "demo_state.json"
    print(f"状态文件：{state_path}")

    # 电话渠道在演练开始时对青溪网格员故障，稍后恢复
    phone_back = {"ok": False}

    def notifier(channel: str, actor_id: str, content: str) -> bool:
        if channel == "phone" and actor_id == "grid_qingxi" and not phone_back["ok"]:
            return False
        return True

    svc = FloodAlertService.open(state_path, notifier=notifier)

    # 1) 参与方到位
    line("08:00 值班力量报到")
    svc.register_actor("cmd_qingxi", "青溪镇指挥员", "commander", "town_qingxi")
    svc.register_actor("cmd_baolun", "宝轮镇指挥员", "commander", "town_baolun")
    svc.register_actor("hydro_qingxi", "青溪水文员", "hydrologist", "town_qingxi")
    svc.register_actor("hydro_baolun", "宝轮水文员", "hydrologist", "town_baolun")
    svc.register_actor("grid_qingxi", "青溪网格员", "grid_worker", "town_qingxi")
    svc.register_actor("grid_baolun", "宝轮网格员", "grid_worker", "town_baolun")
    svc.register_actor("county_duty", "广元县值班", "county_duty", "county_guangyuan")
    svc.register_actor("city_duty", "广元市值班", "city_duty", "city_guangyuan")
    svc.register_actor("province_duty", "四川省值班", "province_duty", "province_sichuan")

    # 2) 依据观测发布黄色预警，锁定时段、区域、依据
    line("08:00 黄色山洪预警发布")
    rain = svc.record_observation(
        "hydro_qingxi", "town_qingxi", "rainfall_3h", 48.0, "mm",
        "2026-09-28T07:50:00", "青溪自动雨量站",
    )
    warn = svc.issue_warning(
        "cmd_qingxi", "town_qingxi", "yellow",
        "2026-09-28T08:00:00", "2026-09-28T12:00:00",
        [rain["id"]], "3小时雨量48mm，山区沟谷注意山洪",
    )
    print(f"预警 {warn['id']}：{REGION_CN[warn['region']]} {LEVEL_CN[warn['level']]} "
          f"{warn['valid_from']}~{warn['valid_until']}，依据 {rain['id']}")

    failed = [n for n in svc.state["notifications"] if n["status"] == "failed"]
    print(f"短信/电话通知：{len(svc.state['notifications'])} 条，失败 {len(failed)} 条（风险状态不变，生成待办）")

    # 3) 电话渠道恢复，重试失败通知
    line("08:02 渠道恢复，重试失败通知")
    phone_back["ok"] = True
    for notif in failed:
        svc.retry_notification(notif["id"])
    print(f"剩余发送失败待办：{sum(1 for t in svc.open_todos() if t['kind'] == 'notification_failed')} 条")

    # 4) 迟到观测：只能形成修订建议
    line("08:10 水位站延迟补传观测")
    svc.advance("2026-09-28T08:10:00")
    late = svc.record_observation(
        "hydro_qingxi", "town_qingxi", "river_level", 6.2, "m",
        "2026-09-28T07:40:00", "青溪水位站（延迟补传）",
    )
    fresh = svc.warning_trace(warn["id"])["warning"]
    print(f"迟到观测 {late['id']} -> 修订建议 {len(fresh['revision_suggestions'])} 条；"
          f"已发预警级别仍为 {LEVEL_CN[fresh['level']]}，通知未抹除")

    # 5) 无人确认，08:30 自动升级给县级
    line("08:30 未确认，按升级策略交接")
    events = svc.advance("2026-09-28T08:30:00")
    for event in events:
        print(f"  定时事件：{event['type']} -> {event.get('to_role', '')} @{event['at']}")
    svc.confirm_warning("county_duty", warn["id"])
    print("县值班已确认")

    # 6) 转移任务：青溪与宝轮各自预警
    line("08:36-08:40 相邻两镇部署转移")
    svc.advance("2026-09-28T08:36:00")
    task_q = svc.create_transfer_task("grid_qingxi", "town_qingxi", warn["id"], 100, "青溪中心小学安置点")

    rain_b = svc.record_observation(
        "hydro_baolun", "town_baolun", "rainfall_3h", 45.5, "mm",
        "2026-09-28T08:35:00", "宝轮自动雨量站",
    )
    svc.advance("2026-09-28T08:40:00")
    warn_b = svc.issue_warning(
        "cmd_baolun", "town_baolun", "yellow",
        "2026-09-28T08:40:00", "2026-09-28T12:00:00",
        [rain_b["id"]], "宝轮山区同步防范",
    )
    svc.confirm_warning("grid_baolun", warn_b["id"])
    task_b = svc.create_transfer_task("grid_baolun", "town_baolun", warn_b["id"], 60, "宝轮中学安置点")

    # 7) 省级发起跨区联动，台账合并
    line("08:45 省级发起跨区联动")
    svc.advance("2026-09-28T08:45:00")
    link = svc.link_transfer("province_duty", task_q["id"], task_b["id"])
    print(f"两镇转移并入联动台账 {link['group_id']}")

    svc.report_transfer("grid_qingxi", task_q["id"], [f"qx-{i}" for i in range(1, 101)])
    # 宝轮上报 75 人次，其中 15 名边界村人员已由青溪登记
    overlap = [f"qx-{i}" for i in range(86, 101)]
    r = svc.report_transfer("grid_baolun", task_b["id"], overlap + [f"bl-{i}" for i in range(1, 61)])
    print(f"宝轮上报 75 人次：新增 {r['new_count']}，跨区重复 {r['duplicate_count']}，未重复计数")
    rollup = svc.transfer_rollup(warn["id"])
    print(f"联动汇总：区域 {rollup['linked_regions']}，目标 {rollup['target_count']}，"
          f"实际去重转移 {rollup['unique_transferred']}，全部闭环={rollup['completed']}")

    # 8) 解除：发布人不能批准自己的解除
    line("09:00 申请解除青溪预警")
    svc.advance("2026-09-28T09:00:00")
    svc.request_release("cmd_qingxi", warn["id"], "降雨结束，水位回落至警戒线下")
    try:
        svc.approve_release("cmd_qingxi", warn["id"], True)
    except DomainError as exc:
        print(f"发布人自批被拒绝：{exc}")
    svc.approve_release("county_duty", warn["id"], True, "现场核查无误，同意解除")
    print("县值班批准解除，复盘任务已生成（时限24小时）")

    # 9) 模拟时钟越过宝轮预警失效时点
    line("12:05 宝轮预警到期未解除，自动过期")
    events = svc.advance("2026-09-28T12:05:00")
    for event in events:
        print(f"  定时事件：{event['type']} @{event['at']}")

    # 10) 服务重启：从磁盘恢复
    line("服务重启，从状态文件恢复")
    restarted = FloodAlertService.open(state_path)
    print(f"恢复后模拟时钟：{restarted.clock.now()}")

    trace = restarted.warning_trace(warn["id"])
    print(f"\n预警 {warn['id']} 决策追溯链：")
    for entry in trace["audit"]:
        detail = ",".join(f"{k}={v}" for k, v in entry["detail"].items())
        print(f"  {entry['at']}  {entry['actor_id']:<14}{entry['action']:<22}{detail}")
    print("锁定依据：")
    for basis in trace["warning"]["basis"]:
        print(f"  {basis['observation_id']} {basis['kind']}={basis['value']}{basis['unit']} "
              f"观测于 {basis['observed_at']}（{basis['source']}）")

    loops = restarted.open_loops()
    print("\n尚未闭环：")
    print(f"  生效中预警：{[(w['id'], w['region'], w['status']) for w in loops['open_warnings']]}")
    print(f"  未完成转移：{[(t['id'], t['region'], f'{t['transferred']}/{t['target']}') for t in loops['open_transfers']]}")
    print(f"  待复盘：{[(r['id'], r['region']) for r in loops['open_reviews']]}")
    print(f"  待办：{[(t['kind'], t['summary']) for t in loops['open_todos']]}")

    # 11) 完成全部复盘，闭环
    line("次日 复盘完成")
    restarted.advance("2026-09-29T12:30:00")
    for review in restarted.state["reviews"]:
        if review["status"] == "open":
            restarted.complete_review(
                "cmd_qingxi" if review["region"] == "town_qingxi" else "cmd_baolun",
                review["id"],
                f"{REGION_CN[review['region']]}{'解除' if review['reason'] == 'released' else '过期'}复盘：转移到位、无人员伤亡",
            )
    loops = restarted.open_loops()
    print(f"生效中预警 {len(loops['open_warnings'])}，未完成转移 {len(loops['open_transfers'])}，"
          f"待复盘 {len(loops['open_reviews'])}，待办 {len(loops['open_todos'])}")
    print("演练结束。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
