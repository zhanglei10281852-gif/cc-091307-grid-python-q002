"""升级规则：紧急迹象立即升级；普通未回应按阶梯重试。"""

from datetime import datetime, timedelta

from src.models import EscalationLevel, EscalationStatus, ObservationType, VisitStatus
from conftest import MONDAY, NOW, time_windows


def _setup(service, make_elder):
    worker = service.register_worker("王社工", ["东区"])
    elder = make_elder(windows=time_windows(0))
    service.create_plan(elder.id, operator="管理员", start_date=MONDAY, days_ahead=1, now=NOW)
    visit = sorted(service.visits.values(), key=lambda v: v.start)[0]
    return worker, elder, visit


def _pending(service):
    return [v for v in service.visits.values() if v.status == VisitStatus.PENDING]


def test_emergency_escalates_immediately(service, make_elder):
    worker, elder, visit = _setup(service, make_elder)
    at = datetime(2026, 9, 21, 9, 5)
    service.check_in(visit.id, worker.id, at=at)
    service.finish_visit(visit.id, [(ObservationType.EMERGENCY, "老人倒地无法起身")],
                         recorded_by=worker.name, at=at)

    assert visit.status == VisitStatus.ESCALATED
    (esc,) = service.escalations.values()
    assert esc.level == EscalationLevel.EMERGENCY
    assert esc.status == EscalationStatus.OPEN
    assert esc.elder_id == elder.id

    service.acknowledge_escalation(esc.id, operator="管理员", at=at)
    assert esc.status == EscalationStatus.ACKED
    service.resolve_escalation(esc.id, operator="管理员", resolution="已送医，家属到场", at=at)
    assert esc.status == EscalationStatus.RESOLVED
    assert esc.resolved_by == "管理员"


def test_no_response_ladder_then_routine_escalation(service, make_elder):
    worker, elder, visit = _setup(service, make_elder)
    t1 = datetime(2026, 9, 21, 9, 10)
    service.check_in(visit.id, worker.id, at=t1)
    service.finish_visit(visit.id, [ObservationType.NO_RESPONSE], recorded_by=worker.name, at=t1)

    (retry1,) = _pending(service)
    assert retry1.retry_of == visit.id and retry1.retry_step == 1
    assert retry1.start == t1 + timedelta(hours=2)  # 第一级：2 小时后重试

    t2 = t1 + timedelta(hours=2)
    service.check_in(retry1.id, worker.id, at=t2)
    service.finish_visit(retry1.id, [ObservationType.NO_RESPONSE], recorded_by=worker.name, at=t2)

    (retry2,) = _pending(service)
    assert retry2.retry_of == retry1.id and retry2.retry_step == 2
    assert retry2.start == t2 + timedelta(days=1)  # 第二级：次日重试

    t3 = t2 + timedelta(days=1)
    service.check_in(retry2.id, worker.id, at=t3)
    service.finish_visit(retry2.id, [ObservationType.NO_RESPONSE], recorded_by=worker.name, at=t3)

    (esc,) = service.escalations.values()
    assert esc.level == EscalationLevel.ROUTINE
    assert "3" in esc.reason  # 连续 3 次未回应
    assert service.visit_count(elder.id) == 3
    assert _pending(service) == []  # 阶梯耗尽，不再生成重试


def test_ladder_stops_once_elder_responds(service, make_elder):
    worker, elder, visit = _setup(service, make_elder)
    t1 = datetime(2026, 9, 21, 9, 10)
    service.check_in(visit.id, worker.id, at=t1)
    service.finish_visit(visit.id, [ObservationType.NO_RESPONSE], recorded_by=worker.name, at=t1)

    (retry,) = _pending(service)
    t2 = t1 + timedelta(hours=2)
    service.check_in(retry.id, worker.id, at=t2)
    service.finish_visit(retry.id, [(ObservationType.NORMAL, "老人出门买菜，已电话联系确认")],
                         recorded_by=worker.name, at=t2)

    assert service.escalations == {}  # 老人有回应，不再升级
    assert _pending(service) == []
