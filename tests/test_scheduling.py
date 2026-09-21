"""排班生成：照护等级 × 授权时段 × 社工服务范围。"""

from datetime import time, timedelta

from src.models import CareLevel
from conftest import MONDAY, NOW, time_windows


def test_high_care_visits_only_on_authorized_weekdays(service, make_elder):
    worker = service.register_worker("王社工", ["东区"])
    elder = make_elder(care_level=CareLevel.HIGH, windows=time_windows(0, 2))  # 周一、周三
    service.create_plan(elder.id, operator="管理员", start_date=MONDAY, days_ahead=7, now=NOW)

    visits = sorted(service.visits.values(), key=lambda v: v.start)
    assert [v.start.date() for v in visits] == [MONDAY, MONDAY + timedelta(days=2)]
    assert all(v.start.time() == time(9, 0) for v in visits)
    assert all(v.end - v.start == timedelta(minutes=30) for v in visits)
    assert all(v.worker_id == worker.id for v in visits)


def test_care_level_controls_frequency(service, make_elder):
    service.register_worker("王社工", ["东区"])
    medium = make_elder(care_level=CareLevel.MEDIUM, windows=time_windows(0, 2, 4))
    low = make_elder(care_level=CareLevel.LOW, windows=time_windows(0))
    service.create_plan(medium.id, operator="管理员", start_date=MONDAY, days_ahead=7, now=NOW)
    service.create_plan(low.id, operator="管理员", start_date=MONDAY, days_ahead=7, now=NOW)

    assert len([v for v in service.visits.values() if v.elder_id == medium.id]) == 3
    assert len([v for v in service.visits.values() if v.elder_id == low.id]) == 1


def test_assignment_balances_within_area_workers(service, make_elder):
    w1 = service.register_worker("王社工", ["东区"])
    w2 = service.register_worker("李社工", ["东区"])
    west = service.register_worker("赵社工", ["西区"])
    # 四位老人同一天各有一次访问，应在同片区两名社工间均衡分配
    elders = [make_elder(windows=time_windows(0)) for _ in range(4)]
    for elder in elders:
        service.create_plan(elder.id, operator="管理员", start_date=MONDAY, days_ahead=1, now=NOW)

    assigned = [v.worker_id for v in service.visits.values()]
    assert west.id not in assigned  # 不派给其他片区社工
    assert sorted(assigned.count(w.id) for w in (w1, w2)) == [2, 2]


def test_worker_on_leave_not_assigned(service, make_elder):
    w1 = service.register_worker("王社工", ["东区"])
    service.request_leave(w1.id, MONDAY, MONDAY + timedelta(days=2), operator="主管", now=NOW)
    elder = make_elder(windows=time_windows(0, 3))  # 周一（请假中）、周四
    service.create_plan(elder.id, operator="管理员", start_date=MONDAY, days_ahead=4, now=NOW)

    by_day = {v.start.date(): v for v in service.visits.values()}
    assert by_day[MONDAY].worker_id is None  # 请假中且无人可派
    assert by_day[MONDAY + timedelta(days=3)].worker_id == w1.id


def test_unassigned_when_area_not_covered(service, make_elder):
    service.register_worker("王社工", ["东区"])
    elder = make_elder(area="北区")
    service.create_plan(elder.id, operator="管理员", start_date=MONDAY, days_ahead=1, now=NOW)

    assert all(v.worker_id is None for v in service.visits.values())
