"""计划生命周期：暂停/恢复/请假替班/计划调整，全程留痕。"""

from datetime import timedelta

from src.models import PlanStatus, VisitStatus
from conftest import MONDAY, NOW, time_windows


def _create(service, elder):
    return service.create_plan(elder.id, operator="管理员", start_date=MONDAY, days_ahead=7, now=NOW)


def test_pause_cancels_pending_and_keeps_version(service, make_elder):
    service.register_worker("王社工", ["东区"])
    elder = make_elder(windows=time_windows(0, 1, 2))
    plan = _create(service, elder)
    assert plan.version == 1

    service.pause_plan(elder.id, reason="老人住院", operator="管理员", now=NOW)
    assert plan.status == PlanStatus.PAUSED
    assert plan.version == 2
    last = plan.revisions[-1]
    assert (last.action, last.operator) == ("PAUSE", "管理员")
    assert "老人住院" in last.detail

    assert not [v for v in service.visits.values() if v.status == VisitStatus.PENDING]
    cancelled = [v for v in service.visits.values() if v.status == VisitStatus.CANCELLED]
    assert len(cancelled) == 3
    assert all("老人住院" in v.cancel_reason for v in cancelled)


def test_resume_regenerates_from_resume_day(service, make_elder):
    service.register_worker("王社工", ["东区"])
    elder = make_elder(windows=time_windows(2, 3))  # 周三、周四
    _create(service, elder)
    service.pause_plan(elder.id, reason="外出探亲", operator="管理员", now=NOW)

    resume_at = NOW + timedelta(days=2)  # 周三 08:00
    service.resume_plan(elder.id, operator="管理员", now=resume_at, days_ahead=2)
    plan = service._plan_of(elder.id)
    assert plan.status == PlanStatus.ACTIVE
    assert plan.version == 3
    assert plan.revisions[-1].action == "RESUME"

    pending_days = sorted(v.start.date() for v in service.visits.values() if v.status == VisitStatus.PENDING)
    assert pending_days == [MONDAY + timedelta(days=2), MONDAY + timedelta(days=3)]


def test_leave_with_substitute_reassigns_visits(service, make_elder):
    w1 = service.register_worker("王社工", ["东区"])
    elder = make_elder(windows=time_windows(0, 1))
    plan = _create(service, elder)
    assert all(v.worker_id == w1.id for v in service.visits.values())

    w2 = service.register_worker("李社工", ["东区"])
    service.request_leave(w1.id, MONDAY, MONDAY + timedelta(days=1), operator="主管",
                          substitute_id=w2.id, now=NOW)

    assert all(v.worker_id == w2.id for v in service.visits.values())
    reassigns = [r for r in plan.revisions if r.action == "REASSIGN"]
    assert len(reassigns) == 2
    assert all(r.operator == "主管" for r in reassigns)


def test_leave_without_substitute_marks_unassigned(service, make_elder):
    w1 = service.register_worker("王社工", ["东区"])
    elder = make_elder(windows=time_windows(0))
    _create(service, elder)

    service.request_leave(w1.id, MONDAY, MONDAY, operator="主管", now=NOW)
    visit = next(iter(service.visits.values()))
    assert visit.worker_id is None
    assert service.plans[visit.plan_id].revisions[-1].action == "REASSIGN"


def test_update_windows_reversions_and_regenerates(service, make_elder):
    service.register_worker("王社工", ["东区"])
    elder = make_elder(windows=time_windows(0))
    plan = _create(service, elder)

    service.update_windows(elder.id, time_windows(1, 2), operator="管理员", now=NOW)
    assert plan.version == 2
    assert plan.revisions[-1].action == "WINDOW_CHANGE"

    days = sorted(v.start.date() for v in service.visits.values() if v.status == VisitStatus.PENDING)
    assert days == [MONDAY + timedelta(days=1), MONDAY + timedelta(days=2)]
