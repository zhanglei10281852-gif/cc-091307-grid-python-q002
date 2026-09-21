"""领域模型：老人、社工、访问计划、访问记录与升级事件。

所有模型均提供 to_dict / from_dict，用于 JSON 持久化，
保证应用重启后排班进度与隐私授权保持一致。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _parse_time(value: str | None) -> time | None:
    return time.fromisoformat(value) if value else None


class CareLevel(str, Enum):
    """照护等级，决定探访频次。"""

    HIGH = "HIGH"      # 每日探访
    MEDIUM = "MEDIUM"  # 每周三次
    LOW = "LOW"        # 每周一次


class ObservationType(str, Enum):
    """一次访问可记录的观察结果类型（可同时记录多种）。"""

    NORMAL = "NORMAL"            # 一切正常
    NO_RESPONSE = "NO_RESPONSE"  # 敲门/呼叫未回应
    REFUSED = "REFUSED"          # 拒绝服务
    AWAY_TEMP = "AWAY_TEMP"      # 临时外出
    EMERGENCY = "EMERGENCY"      # 紧急迹象（呼救、倒地、异味等）


class VisitStatus(str, Enum):
    PENDING = "PENDING"        # 待访
    CHECKED_IN = "CHECKED_IN"  # 已签到
    DONE = "DONE"              # 已完成
    CANCELLED = "CANCELLED"    # 已取消（暂停/计划调整）
    ESCALATED = "ESCALATED"    # 已触发升级


class PlanStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"


class EscalationLevel(str, Enum):
    EMERGENCY = "EMERGENCY"  # 紧急迹象，立即升级
    ROUTINE = "ROUTINE"      # 阶梯重试耗尽后的常规升级


class EscalationStatus(str, Enum):
    OPEN = "OPEN"
    ACKED = "ACKED"      # 管理端已知晓
    RESOLVED = "RESOLVED"  # 已闭环


@dataclass
class TimeWindow:
    """老人授权的可探访时段（按星期几）。"""

    weekday: int  # 0=周一 ... 6=周日
    start: time
    end: time

    def __post_init__(self) -> None:
        if not 0 <= self.weekday <= 6:
            raise ValueError("weekday 必须在 0-6 之间")
        if self.start >= self.end:
            raise ValueError("授权时段开始必须早于结束")

    def to_dict(self) -> dict:
        return {
            "weekday": self.weekday,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TimeWindow":
        return cls(
            weekday=d["weekday"],
            start=_parse_time(d["start"]),
            end=_parse_time(d["end"]),
        )


@dataclass
class PrivacyConsent:
    """老人隐私授权。"""

    share_identity_with_worker: bool = True  # 允许负责片区社工查看身份信息
    allow_family_contact: bool = True        # 紧急时允许联系家属

    def to_dict(self) -> dict:
        return {
            "share_identity_with_worker": self.share_identity_with_worker,
            "allow_family_contact": self.allow_family_contact,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PrivacyConsent":
        return cls(
            share_identity_with_worker=d["share_identity_with_worker"],
            allow_family_contact=d["allow_family_contact"],
        )


@dataclass
class EmergencyContact:
    name: str
    phone: str
    relation: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "phone": self.phone, "relation": self.relation}

    @classmethod
    def from_dict(cls, d: dict) -> "EmergencyContact":
        return cls(name=d["name"], phone=d["phone"], relation=d.get("relation", ""))


@dataclass
class Elder:
    """独居老人档案。身份信息属于隐私数据，访问受片区与授权控制。"""

    name: str
    id_number: str
    phone: str
    address: str
    area: str  # 所属片区
    care_level: CareLevel
    windows: list[TimeWindow] = field(default_factory=list)
    consent: PrivacyConsent = field(default_factory=PrivacyConsent)
    emergency_contact: EmergencyContact | None = None
    id: str = field(default_factory=lambda: _new_id("elder"))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "id_number": self.id_number,
            "phone": self.phone,
            "address": self.address,
            "area": self.area,
            "care_level": self.care_level.value,
            "windows": [w.to_dict() for w in self.windows],
            "consent": self.consent.to_dict(),
            "emergency_contact": self.emergency_contact.to_dict() if self.emergency_contact else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Elder":
        contact = d.get("emergency_contact")
        return cls(
            id=d["id"],
            name=d["name"],
            id_number=d["id_number"],
            phone=d["phone"],
            address=d["address"],
            area=d["area"],
            care_level=CareLevel(d["care_level"]),
            windows=[TimeWindow.from_dict(w) for w in d["windows"]],
            consent=PrivacyConsent.from_dict(d["consent"]),
            emergency_contact=EmergencyContact.from_dict(contact) if contact else None,
        )


@dataclass
class LeavePeriod:
    """社工请假记录，可指定替班人。"""

    start: date
    end: date
    approved_by: str
    created_at: datetime
    substitute_id: str | None = None

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "approved_by": self.approved_by,
            "created_at": self.created_at.isoformat(),
            "substitute_id": self.substitute_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LeavePeriod":
        return cls(
            start=_parse_date(d["start"]),
            end=_parse_date(d["end"]),
            approved_by=d["approved_by"],
            created_at=_parse_dt(d["created_at"]),
            substitute_id=d.get("substitute_id"),
        )


@dataclass
class SocialWorker:
    """社工，按服务范围（片区）负责探访。"""

    name: str
    areas: list[str]
    id: str = field(default_factory=lambda: _new_id("worker"))
    leave_periods: list[LeavePeriod] = field(default_factory=list)

    def covers(self, area: str) -> bool:
        return area in self.areas

    def is_on_leave(self, day: date) -> bool:
        return any(p.contains(day) for p in self.leave_periods)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "areas": list(self.areas),
            "leave_periods": [p.to_dict() for p in self.leave_periods],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SocialWorker":
        return cls(
            id=d["id"],
            name=d["name"],
            areas=list(d["areas"]),
            leave_periods=[LeavePeriod.from_dict(p) for p in d["leave_periods"]],
        )


@dataclass
class Observation:
    """一条观察结果。"""

    type: ObservationType
    note: str
    recorded_at: datetime

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "note": self.note,
            "recorded_at": self.recorded_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Observation":
        return cls(
            type=ObservationType(d["type"]),
            note=d["note"],
            recorded_at=_parse_dt(d["recorded_at"]),
        )


@dataclass
class CheckIn:
    """签到记录。重复签到返回同一条记录，不增加访问次数。"""

    worker_id: str
    at: datetime

    def to_dict(self) -> dict:
        return {"worker_id": self.worker_id, "at": self.at.isoformat()}

    @classmethod
    def from_dict(cls, d: dict) -> "CheckIn":
        return cls(worker_id=d["worker_id"], at=_parse_dt(d["at"]))


@dataclass
class Visit:
    """一次访问（计划内或阶梯重试产生）。"""

    plan_id: str
    elder_id: str
    worker_id: str | None  # None 表示待指派
    start: datetime
    end: datetime
    id: str = field(default_factory=lambda: _new_id("visit"))
    status: VisitStatus = VisitStatus.PENDING
    check_in: CheckIn | None = None
    observations: list[Observation] = field(default_factory=list)
    retry_of: str | None = None  # 阶梯重试：指向触发本次重试的访问
    retry_step: int = 0          # 0=计划内访问，1..N=第几级重试
    cancel_reason: str | None = None
    finished_by: str | None = None

    def is_overdue(self, now: datetime) -> bool:
        return self.status == VisitStatus.PENDING and now > self.end

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "plan_id": self.plan_id,
            "elder_id": self.elder_id,
            "worker_id": self.worker_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "status": self.status.value,
            "check_in": self.check_in.to_dict() if self.check_in else None,
            "observations": [o.to_dict() for o in self.observations],
            "retry_of": self.retry_of,
            "retry_step": self.retry_step,
            "cancel_reason": self.cancel_reason,
            "finished_by": self.finished_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Visit":
        check_in = d.get("check_in")
        return cls(
            id=d["id"],
            plan_id=d["plan_id"],
            elder_id=d["elder_id"],
            worker_id=d.get("worker_id"),
            start=_parse_dt(d["start"]),
            end=_parse_dt(d["end"]),
            status=VisitStatus(d["status"]),
            check_in=CheckIn.from_dict(check_in) if check_in else None,
            observations=[Observation.from_dict(o) for o in d["observations"]],
            retry_of=d.get("retry_of"),
            retry_step=d.get("retry_step", 0),
            cancel_reason=d.get("cancel_reason"),
            finished_by=d.get("finished_by"),
        )


@dataclass
class PlanRevision:
    """计划变更留痕：版本号、经办人、动作与说明。"""

    version: int
    at: datetime
    operator: str
    action: str  # CREATE / PAUSE / RESUME / REASSIGN / WINDOW_CHANGE / LEVEL_CHANGE / CLOSE
    detail: str

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "at": self.at.isoformat(),
            "operator": self.operator,
            "action": self.action,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlanRevision":
        return cls(
            version=d["version"],
            at=_parse_dt(d["at"]),
            operator=d["operator"],
            action=d["action"],
            detail=d["detail"],
        )


@dataclass
class VisitPlan:
    """一位老人的访问计划。每次变更追加一条版本记录。"""

    elder_id: str
    id: str = field(default_factory=lambda: _new_id("plan"))
    status: PlanStatus = PlanStatus.ACTIVE
    revisions: list[PlanRevision] = field(default_factory=list)

    @property
    def version(self) -> int:
        return self.revisions[-1].version if self.revisions else 0

    def add_revision(self, operator: str, action: str, detail: str, at: datetime) -> PlanRevision:
        revision = PlanRevision(
            version=len(self.revisions) + 1,
            at=at,
            operator=operator,
            action=action,
            detail=detail,
        )
        self.revisions.append(revision)
        return revision

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "elder_id": self.elder_id,
            "status": self.status.value,
            "revisions": [r.to_dict() for r in self.revisions],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VisitPlan":
        return cls(
            id=d["id"],
            elder_id=d["elder_id"],
            status=PlanStatus(d["status"]),
            revisions=[PlanRevision.from_dict(r) for r in d["revisions"]],
        )


@dataclass
class Escalation:
    """升级事件：紧急迹象立即升级；未回应阶梯重试耗尽后常规升级。"""

    elder_id: str
    visit_id: str | None
    level: EscalationLevel
    reason: str
    created_at: datetime
    id: str = field(default_factory=lambda: _new_id("esc"))
    status: EscalationStatus = EscalationStatus.OPEN
    acked_by: str | None = None
    acked_at: datetime | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "elder_id": self.elder_id,
            "visit_id": self.visit_id,
            "level": self.level.value,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
            "status": self.status.value,
            "acked_by": self.acked_by,
            "acked_at": self.acked_at.isoformat() if self.acked_at else None,
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolution": self.resolution,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Escalation":
        return cls(
            id=d["id"],
            elder_id=d["elder_id"],
            visit_id=d.get("visit_id"),
            level=EscalationLevel(d["level"]),
            reason=d["reason"],
            created_at=_parse_dt(d["created_at"]),
            status=EscalationStatus(d["status"]),
            acked_by=d.get("acked_by"),
            acked_at=_parse_dt(d.get("acked_at")),
            resolved_by=d.get("resolved_by"),
            resolved_at=_parse_dt(d.get("resolved_at")),
            resolution=d.get("resolution"),
        )
