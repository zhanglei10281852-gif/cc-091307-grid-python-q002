"""共享夹具与构造辅助。"""

from datetime import date, datetime, time

import pytest

from src.models import CareLevel, TimeWindow
from src.service import CareService

MONDAY = date(2026, 9, 21)  # 2026-09-21 为周一
NOW = datetime(2026, 9, 21, 8, 0)


def time_windows(*weekdays, start=time(9, 0), end=time(10, 0)):
    """按星期几构造授权时段，默认 09:00-10:00。"""
    return [TimeWindow(weekday=d, start=start, end=end) for d in weekdays]


@pytest.fixture
def service():
    return CareService()


@pytest.fixture
def make_elder(service):
    counter = {"n": 0}

    def _make(area="东区", care_level=CareLevel.HIGH, windows=None, **kw):
        counter["n"] += 1
        n = counter["n"]
        defaults = dict(
            name=f"老人{n}",
            id_number=f"11010119400101{n:04d}",
            phone=f"1380000{n:04d}",
            address=f"{area}幸福里{n}栋101",
            windows=windows if windows is not None else time_windows(0),
        )
        defaults.update(kw)
        return service.register_elder(area=area, care_level=care_level, **defaults)

    return _make
