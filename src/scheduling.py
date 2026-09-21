"""排班生成与未回应阶梯重试策略。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .models import CareLevel, Elder


@dataclass(frozen=True)
class CarePolicy:
    """照护等级对应的探访星期与单次时长。"""

    level_weekdays: dict[CareLevel, tuple[int, ...]]
    visit_duration: timedelta


DEFAULT_POLICY = CarePolicy(
    level_weekdays={
        CareLevel.HIGH: (0, 1, 2, 3, 4, 5, 6),  # 每日
        CareLevel.MEDIUM: (0, 2, 4),            # 周一/三/五
        CareLevel.LOW: (0,),                    # 周一
    },
    visit_duration=timedelta(minutes=30),
)


@dataclass(frozen=True)
class RetryLadder:
    """普通未回应的阶梯重试规则。

    delays 依次为每一级重试相对上次未回应的等待时长；
    所有阶梯耗尽后升级为常规事件（ROUTINE）。
    """

    delays: tuple[timedelta, ...] = (timedelta(hours=2), timedelta(days=1))


DEFAULT_LADDER = RetryLadder()


def generate_slots(
    elder: Elder,
    policy: CarePolicy,
    start_date: date,
    end_date: date,
) -> list[tuple[datetime, datetime]]:
    """按照护等级要求的星期与老人授权时段的交集生成访问时间段。

    仅在老人授权的时段内排访；某天没有授权时段则当天不排。
    """
    required = policy.level_weekdays[elder.care_level]
    slots: list[tuple[datetime, datetime]] = []
    day = start_date
    while day <= end_date:
        if day.weekday() in required:
            window = next(
                (w for w in elder.windows if w.weekday == day.weekday() and w.start < w.end),
                None,
            )
            if window is not None:
                start = datetime.combine(day, window.start)
                end = min(start + policy.visit_duration, datetime.combine(day, window.end))
                slots.append((start, end))
        day += timedelta(days=1)
    return slots
