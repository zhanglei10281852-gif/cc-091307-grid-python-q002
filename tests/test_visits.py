"""访问执行：签到幂等、权限、多种观察结果。"""

from datetime import datetime, timedelta

import pytest

from src.models import ObservationType, VisitStatus
from conftest import MONDAY, NOW, time_windows


@pytest.fixture
def visit_setup(service, make_elder):
    worker = service.register_worker("王社工", ["东区"])
    elder = make_elder(windows=time_windows(0))
    service.create_plan(elder.id, operator="管理员", start_date=MONDAY, days_ahead=1, now=NOW)
    visit = sorted(service.visits.values(), key=lambda v: v.start)[0]
    return worker, elder, visit


def test_check_in_is_idempotent(service, visit_setup):
    worker, elder, visit = visit_setup
    at = datetime(2026, 9, 21, 9, 5)
    first = service.check_in(visit.id, worker.id, at=at)
    again = service.check_in(visit.id, worker.id, at=at + timedelta(minutes=30))

    assert again is first  # 重复签到返回原记录
    assert again.at == at

    service.finish_visit(visit.id, [ObservationType.NORMAL], recorded_by=worker.name,
                         at=at + timedelta(minutes=20))
    assert service.visit_count(elder.id) == 1  # 重复签到未虚增访问次数


def test_check_in_rejects_other_worker(service, visit_setup):
    _, _, visit = visit_setup
    other = service.register_worker("李社工", ["东区"])
    with pytest.raises(PermissionError):
        service.check_in(visit.id, other.id, at=datetime(2026, 9, 21, 9, 5))


def test_finish_requires_check_in(service, visit_setup):
    worker, _, visit = visit_setup
    with pytest.raises(ValueError):
        service.finish_visit(visit.id, [ObservationType.NORMAL], recorded_by=worker.name)


def test_visit_records_multiple_observations(service, visit_setup):
    worker, elder, visit = visit_setup
    at = datetime(2026, 9, 21, 9, 5)
    service.check_in(visit.id, worker.id, at=at)
    service.finish_visit(
        visit.id,
        [(ObservationType.NORMAL, "精神状态良好"), (ObservationType.REFUSED, "拒绝本周理发服务")],
        recorded_by=worker.name,
        at=at + timedelta(minutes=20),
    )

    assert visit.status == VisitStatus.DONE
    assert [o.type for o in visit.observations] == [ObservationType.NORMAL, ObservationType.REFUSED]
    assert service.visit_count(elder.id) == 1
