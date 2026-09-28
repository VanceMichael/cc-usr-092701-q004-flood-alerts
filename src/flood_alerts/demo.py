"""端到端演示：黄色山洪预警的闭环处置与重启恢复。

场景（全部为虚构演示数据）：
08:00 关防乡接到黄色山洪气象预警，水文站、气象部门、网格员陆续补充
雨量与转移情况；同区域先后出现蓝色、黄色风险；相邻的湖北口乡与丰溪镇
开展省级联动转移（去重计数）；短信发送失败保留待办；值守未确认按时点
上交；过期、复盘按原定时点推进；服务重启后仍可追溯“谁在何时依据哪份
观测作出决定”，并列出尚未闭环的人员和区域。

运行：
    python3 -m src.flood_alerts.demo [存储目录]
默认使用临时目录；指定目录时可二次运行观察重启恢复。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from .catalog import demo_catalog
from .notifications import NotificationGateway
from .service import AlertService, ConflictError, PermissionError_
from .store import EventStore

T0 = "2026-09-27T08:00:00Z"
GF = "T42032201"
HBK = "T42032202"
FX = "T42032401"


def _print(title: str, rows: list[tuple[str, str]]) -> None:
    print(f"\n=== {title} ===")
    for key, value in rows:
        print(f"  {key:<28} {value}")


def make_gateway() -> NotificationGateway:
    # 演示：发往湖北口乡的短信一直失败，其余渠道正常。
    return NotificationGateway(lambda channel, target, _c: channel == "sms" and target == HBK)


def run(directory: Path) -> None:
    # ---------- 第一段：08:00 起的处置 ----------
    gateway = make_gateway()
    if directory.exists():
        shutil.rmtree(directory)
    svc = AlertService(EventStore(directory), demo_catalog(), gateway, start_time=T0)

    rain1 = svc.receive_observation("rain", "郧西水文站", GF, 45.0, "mm", "水文站", "2026-09-27T07:55:00Z")
    alert = svc.issue_alert("u-gf", GF, "yellow", 6, "1小时雨量45mm，超黄色阈值", [rain1["obs_id"]])
    alert_id = alert["alert_id"]
    _print("08:00 发布黄色预警", [
        ("预警编号", alert_id),
        ("行政区", f"{GF} 关防乡"),
        ("等级", "yellow"),
        ("锁定依据", rain1["obs_id"]),
        ("有效至", alert["valid_until"]),
    ])

    # 09:00 气象部门补充数据，网格员报告人员开始转移。
    svc.advance(1)
    rain2 = svc.receive_observation("rain", "郧西气象分局", GF, 52.0, "mm", "气象部门", "2026-09-27T08:50:00Z")
    tr1 = svc.start_transfer("u-gf", alert_id, "P-1001", "陈守财", GF, HBK)
    _print("08:50-09:00 滚动补充", [
        ("新增观测", f"{rain2['obs_id']}（气象部门，52mm）"),
        ("转移启动", f"{tr1['transfer_id']} 陈守财 关防乡→湖北口乡"),
    ])

    # 09:30 县级确认；10:00 省级发起跨县联动，重复人员不计数。
    svc.advance(0.5)
    svc.confirm_alert("u-yx", alert_id)
    try:
        svc.start_transfer("u-prov", alert_id, "P-1001", "陈守财", HBK, FX, linked=True)
        dedup_note = "异常：重复转移被建出"
    except ConflictError as exc:
        dedup_note = f"已拒绝：{exc}"
    svc.advance(0.5)
    tr2 = svc.start_transfer("u-prov", alert_id, "P-1002", "李桂兰", HBK, FX, linked=True)
    _print("09:30-10:00 确认与省级联动", [
        ("预警确认人", "u-yx 郧西县指挥员"),
        ("联动转移", f"{tr2['transfer_id']} 李桂兰 湖北口乡→丰溪镇"),
        ("重复人员 P-1001", dedup_note),
    ])

    # 10:30 升级为橙色；湖北口乡短信失败，留下待办但风险状态不变。
    svc.advance(0.5)
    rain3 = svc.receive_observation("rain", "郧西水文站", GF, 68.0, "mm", "水文站", "2026-09-27T10:20:00Z")
    svc.upgrade_alert("u-gf", alert_id, "orange", "1小时雨量68mm，持续增强", [rain3["obs_id"]], extend_hours=3)
    svc.confirm_alert("u-yx", alert_id)  # 升级后需重新确认
    todo = svc.open_loops()["open_notification_todos"]
    _print("10:30 升级橙色 + 短信失败待办", [
        ("预警等级", "orange（已重新锁定依据）"),
        ("失败待办数", str(len(todo))),
        ("待办", f"{todo[0]['todo_id']} 渠道={todo[0]['channel']} 目标={todo[0]['target']}（风险状态未变）"),
    ])

    # 11:00 关防乡值守员试图跨区域发布——被权限拒绝。
    svc.advance(0.5)
    try:
        svc.issue_alert("u-gf", FX, "blue", 6, "跨辖区尝试", [rain3["obs_id"]])
    except PermissionError_ as exc:
        _print("11:00 权限拦截", [("跨区域发布", f"已拒绝：{exc}")])

    # 12:00 迟到的强降雨观测（08:00 测得）入库：只产生修订建议，不改写历史。
    svc.advance(1)
    late = svc.receive_observation("rain", "郧西水文站", GF, 85.0, "mm", "水文站", "2026-09-27T08:00:00Z")
    rev = late["revision"]["suggestions"][0]
    _print("12:00 迟到观测（08:00 测得，12:00 入库）", [
        ("观测编号", late["obs_id"]),
        ("是否迟到", str(late["late"])),
        ("修订建议", f"{rev['revision_id']} 建议等级 {rev['suggestion']}"),
        ("历史通知", "保持不变，等待人工复核"),
    ])
    svc.resolve_revision("u-yx", rev["revision_id"], accept=True, note="核实雨势属实，已在橙色预警中覆盖")

    # 13:00 转移全部完成；申请解除。
    svc.advance(1)
    svc.complete_transfer("u-hbk", tr1["transfer_id"])
    svc.complete_transfer("u-fx", tr2["transfer_id"])
    svc.request_release("u-gf", alert_id, "降雨减弱，转移人员全部安置")
    _print("13:00 人员闭环，申请解除", [
        ("未完成转移", "0"),
        ("解除申请", "u-gf 关防乡值守员"),
    ])

    # 13:30 发布人（关防乡）不能批准自己的解除；省级批准，转入复盘。
    svc.advance(0.5)
    try:
        svc.approve_release("u-gf", alert_id, agree=True)
    except PermissionError_ as exc:
        publisher_check = f"已回避：{exc}"
    else:
        publisher_check = "异常：发布人批准成功"
    svc.approve_release("u-prov", alert_id, agree=True)
    _print("13:30 解除审批", [
        ("发布人自批", publisher_check),
        ("批准人", "u-prov 省防汛值班员"),
        ("预警状态", svc.alerts[alert_id].status),
        ("复盘任务", f"RV-{alert_id}（解除后 24 小时到点）"),
    ])

    # ---------- 模拟服务重启 ----------
    print("\n" + "#" * 60)
    print("# 模拟服务重启：重放事件日志，恢复时钟与定时器")
    print("#" * 60)
    svc2 = AlertService(EventStore(directory), demo_catalog(), make_gateway(), start_time=T0)
    _print("重启后恢复", [
        ("恢复时钟", svc2.clock.iso()),
        ("预警状态", svc2.alerts[alert_id].status),
        ("定时器数量", str(svc2.clock.pending_count)),
    ])

    # 决策轨迹：谁在何时依据哪份观测作出决定。
    rows = []
    for trace in svc2.decision_trail(alert_id):
        ev = trace["payload"].get("evidence")
        basis = ",".join(ev["observation_ids"]) if ev else "-"
        rows.append((f'{trace["at"][11:16]} {trace["type"]}', f'{trace["actor"]}  依据={basis}'))
    _print("决策轨迹（谁/何时/依据哪份观测）", rows)

    # 复盘到点。
    svc2.advance(24)
    review_id = f"RV-{alert_id}"
    _print("次日 13:30 复盘到点", [
        ("复盘任务", review_id),
        ("已提醒", str(svc2.reviews[review_id].notified)),
    ])
    svc2.complete_review("u-yx", review_id, "预警发布、升级、解除流程完整，转移无遗漏")

    # 通信网络恢复：用健康网关重建服务并重试待办（风险状态始终未被通知改变）。
    svc3 = AlertService(EventStore(directory), demo_catalog(), NotificationGateway(), start_time=T0)
    for todo in svc3.open_loops()["open_notification_todos"]:
        svc3.retry_notification("u-gf", todo["todo_id"])

    # 最终未闭环清单。
    loops = svc3.open_loops()
    _print("最终未闭环清单", [
        ("未闭环区域", str(len(loops["open_areas"]))),
        ("未闭环人员", str(len(loops["open_transfers"]))),
        ("未完成复盘", str(len(loops["open_reviews"]))),
        ("待处理修订", str(len(loops["open_revisions"]))),
        ("通知失败待办", str(len(loops["open_notification_todos"]))),
    ])
    print("\n演示结束。事件日志位于：", directory / "events.log")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="flood-alert-demo-"))
    run(target)
