"""管理端视图：当天待访、逾期原因、升级状态、下一步安排。"""

from datetime import datetime, time

from src.models import EscalationLevel, LeavePeriod, ObservationType
from conftest import MONDAY, NOW, time_windows


def _visit_of(service, elder_id):
    return next(v for v in service.visits.values() if v.elder_id == elder_id)


def test_admin_dashboard(service, make_elder):
    worker = service.register_worker("王社工", ["东区"])
    morning = make_elder(windows=time_windows(0, start=time(8, 0), end=time(9, 0)))
    afternoon = make_elder(windows=time_windows(0, start=time(14, 0), end=time(15, 0)))
    no_checkin = make_elder(windows=time_windows(0, start=time(8, 0), end=time(9, 0)))
    north = make_elder(area="北区", windows=time_windows(0, start=time(8, 0), end=time(9, 0)))
    retry_elder = make_elder(windows=time_windows(0))
    for elder in (morning, afternoon, no_checkin, north, retry_elder):
        service.create_plan(elder.id, operator="管理员", start_date=MONDAY, days_ahead=1, now=NOW)

    # 紧急迹象 → 立即升级
    v_morning = _visit_of(service, morning.id)
    service.check_in(v_morning.id, worker.id, at=datetime(2026, 9, 21, 8, 35))
    service.finish_visit(v_morning.id, [(ObservationType.EMERGENCY, "老人倒地")],
                         recorded_by="王社工", at=datetime(2026, 9, 21, 8, 40))
    # 普通未回应 → 阶梯重试
    v_retry = _visit_of(service, retry_elder.id)
    service.check_in(v_retry.id, worker.id, at=datetime(2026, 9, 21, 9, 5))
    service.finish_visit(v_retry.id, [ObservationType.NO_RESPONSE],
                         recorded_by="王社工", at=datetime(2026, 9, 21, 9, 10))

    dash = service.admin_dashboard(day=MONDAY, now=datetime(2026, 9, 21, 10, 0))

    # 当天待访：下午的计划访问 + 阶梯重试
    pending_ids = {v.id for v in dash["pending"]}
    assert _visit_of(service, afternoon.id).id in pending_ids
    retry = next(v for v in service.visits.values() if v.retry_of == v_retry.id)
    assert retry.id in pending_ids

    # 逾期及原因
    overdue = {item["visit"].id: item["reason"] for item in dash["overdue"]}
    assert overdue[_visit_of(service, north.id).id] == "未指派社工"
    assert overdue[_visit_of(service, no_checkin.id).id] == "社工未签到"
    assert v_morning.id not in overdue  # 已升级，不再算逾期

    # 升级状态
    (esc,) = dash["escalations"]
    assert esc.level == EscalationLevel.EMERGENCY
    assert esc.status.value == "OPEN"

    # 下一步安排
    kinds = {step["kind"] for step in dash["next_steps"]}
    assert {"escalation", "retry", "overdue"} <= kinds


def test_overdue_reason_worker_on_leave(service, make_elder):
    worker = service.register_worker("王社工", ["东区"])
    elder = make_elder(windows=time_windows(0, start=time(8, 0), end=time(9, 0)))
    service.create_plan(elder.id, operator="管理员", start_date=MONDAY, days_ahead=1, now=NOW)
    # 直接补登请假（模拟历史遗留：访问已派给请假社工但未改派）
    worker.leave_periods.append(
        LeavePeriod(start=MONDAY, end=MONDAY, approved_by="主管", created_at=NOW)
    )

    dash = service.admin_dashboard(day=MONDAY, now=datetime(2026, 9, 21, 10, 0))
    assert dash["overdue"][0]["reason"] == "社工请假且未安排替班"
