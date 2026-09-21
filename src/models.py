"""独居老人关怀排班领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class CareLevel(str, Enum):
    """照护等级：等级越高，上门频次越密。"""

    LEVEL_1 = "level_1"  # 一般关怀：每 7 天一次
    LEVEL_2 = "level_2"  # 重点关怀：每 3 天一次
    LEVEL_3 = "level_3"  # 高风险：每日一次


# 各照护等级相邻两次访问的最小间隔（天）
CADENCE_DAYS: dict[CareLevel, int] = {
    CareLevel.LEVEL_1: 7,
    CareLevel.LEVEL_2: 3,
    CareLevel.LEVEL_3: 1,
}


class VisitStatus(str, Enum):
    PENDING = "pending"                # 待访
    IN_PROGRESS = "in_progress"        # 已签到，访问进行中
    COMPLETED = "completed"            # 已完成（见到本人，含本人拒绝服务）
    RESCHEDULED = "rescheduled"        # 本尝试结束，已生成后续尝试
    PAUSED = "paused"                  # 因老人暂停服务而中止
    CANCELLED = "cancelled"            # 已取消（替班调整等）
    ESCALATED = "escalated"            # 已升级处置


class ObservationKind(str, Enum):
    RESPONDED = "responded"                  # 老人应答 / 见到本人
    NO_RESPONSE = "no_response"              # 普通未回应
    TEMPORARY_ABSENCE = "temporary_absence"  # 临时外出（邻居/留言/电话确认）
    REFUSED_SERVICE = "refused_service"      # 老人明确拒绝服务
    URGENT_SIGN = "urgent_sign"              # 紧急迹象（异常气味、呼救、药物等）


# 尚未终结的状态
OPEN_STATUSES = {VisitStatus.PENDING, VisitStatus.IN_PROGRESS}
# 代表一次访问已实际落地的终结状态
TERMINAL_STATUSES = {VisitStatus.COMPLETED, VisitStatus.ESCALATED}


@dataclass
class AuthorizedWindow:
    """老人授权的可上门时段。

    weekdays: 0=周一 ... 6=周日；start/end 为 HH:MM。
    """

    weekdays: list[int]
    start: str
    end: str

    def to_dict(self) -> dict:
        return {"weekdays": list(self.weekdays), "start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, data: dict) -> "AuthorizedWindow":
        return cls(weekdays=list(data["weekdays"]), start=data["start"], end=data["end"])


@dataclass
class Worker:
    id: str
    name: str
    areas: list[str]
    active: bool = True

    def covers(self, area_id: str) -> bool:
        return area_id in self.areas

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "areas": list(self.areas), "active": self.active}

    @classmethod
    def from_dict(cls, data: dict) -> "Worker":
        return cls(id=data["id"], name=data["name"], areas=list(data["areas"]), active=data.get("active", True))


@dataclass
class Elder:
    id: str
    name: str
    id_card: str
    phone: str
    address: str
    area_id: str
    care_level: CareLevel
    windows: list[AuthorizedWindow]
    privacy_consent: bool = False
    consent_changed_at: datetime | None = None
    paused: bool = False
    paused_at: datetime | None = None
    pause_reason: str | None = None
    pause_until: date | None = None
    resumed_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "id_card": self.id_card,
            "phone": self.phone,
            "address": self.address,
            "area_id": self.area_id,
            "care_level": self.care_level.value,
            "windows": [w.to_dict() for w in self.windows],
            "privacy_consent": self.privacy_consent,
            "consent_changed_at": self.consent_changed_at.isoformat() if self.consent_changed_at else None,
            "paused": self.paused,
            "paused_at": self.paused_at.isoformat() if self.paused_at else None,
            "pause_reason": self.pause_reason,
            "pause_until": self.pause_until.isoformat() if self.pause_until else None,
            "resumed_at": self.resumed_at.isoformat() if self.resumed_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Elder":
        return cls(
            id=data["id"],
            name=data["name"],
            id_card=data["id_card"],
            phone=data["phone"],
            address=data["address"],
            area_id=data["area_id"],
            care_level=CareLevel(data["care_level"]),
            windows=[AuthorizedWindow.from_dict(w) for w in data["windows"]],
            privacy_consent=data.get("privacy_consent", False),
            consent_changed_at=_parse_dt(data.get("consent_changed_at")),
            paused=data.get("paused", False),
            paused_at=_parse_dt(data.get("paused_at")),
            pause_reason=data.get("pause_reason"),
            pause_until=_parse_date(data.get("pause_until")),
            resumed_at=_parse_dt(data.get("resumed_at")),
        )


@dataclass
class Observation:
    """一次访问中的单条观察结果；一次访问可记录多条。"""

    kind: ObservationKind
    note: str = ""
    recorded_at: datetime | None = None
    recorded_by: str | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "note": self.note,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
            "recorded_by": self.recorded_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Observation":
        return cls(
            kind=ObservationKind(data["kind"]),
            note=data.get("note", ""),
            recorded_at=_parse_dt(data.get("recorded_at")),
            recorded_by=data.get("recorded_by"),
        )


@dataclass
class CheckIn:
    """签到记录，以幂等键去重。"""

    key: str
    worker_id: str
    at: datetime

    def to_dict(self) -> dict:
        return {"key": self.key, "worker_id": self.worker_id, "at": self.at.isoformat()}

    @classmethod
    def from_dict(cls, data: dict) -> "CheckIn":
        return cls(key=data["key"], worker_id=data["worker_id"], at=datetime.fromisoformat(data["at"]))


@dataclass
class Visit:
    id: str
    group_id: str          # 同一次访问的多轮尝试（未回应重试/改约）共享 group_id
    attempt_no: int        # 1 = 首轮，之后按未回应阶梯递增
    elder_id: str
    area_id: str
    scheduled_start: datetime
    scheduled_end: datetime
    status: VisitStatus = VisitStatus.PENDING
    worker_id: str | None = None
    observations: list[Observation] = field(default_factory=list)
    checkins: list[CheckIn] = field(default_factory=list)
    source_version: int = 0
    change_note: str = ""
    created_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "group_id": self.group_id,
            "attempt_no": self.attempt_no,
            "elder_id": self.elder_id,
            "area_id": self.area_id,
            "scheduled_start": self.scheduled_start.isoformat(),
            "scheduled_end": self.scheduled_end.isoformat(),
            "status": self.status.value,
            "worker_id": self.worker_id,
            "observations": [o.to_dict() for o in self.observations],
            "checkins": [c.to_dict() for c in self.checkins],
            "source_version": self.source_version,
            "change_note": self.change_note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Visit":
        return cls(
            id=data["id"],
            group_id=data["group_id"],
            attempt_no=data["attempt_no"],
            elder_id=data["elder_id"],
            area_id=data["area_id"],
            scheduled_start=datetime.fromisoformat(data["scheduled_start"]),
            scheduled_end=datetime.fromisoformat(data["scheduled_end"]),
            status=VisitStatus(data["status"]),
            worker_id=data.get("worker_id"),
            observations=[Observation.from_dict(o) for o in data.get("observations", [])],
            checkins=[CheckIn.from_dict(c) for c in data.get("checkins", [])],
            source_version=data.get("source_version", 0),
            change_note=data.get("change_note", ""),
            created_at=_parse_dt(data.get("created_at")),
        )


@dataclass
class EscalationAction:
    name: str
    due_at: datetime
    done_at: datetime | None = None
    done_by: str | None = None

    @property
    def pending(self) -> bool:
        return self.done_at is None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "due_at": self.due_at.isoformat(),
            "done_at": self.done_at.isoformat() if self.done_at else None,
            "done_by": self.done_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EscalationAction":
        return cls(
            name=data["name"],
            due_at=datetime.fromisoformat(data["due_at"]),
            done_at=_parse_dt(data.get("done_at")),
            done_by=data.get("done_by"),
        )


@dataclass
class Escalation:
    """紧急/失联升级单。"""

    id: str
    visit_id: str
    elder_id: str
    reason: str  # urgent_sign | no_response_missing
    created_at: datetime
    created_by: str
    actions: list[EscalationAction] = field(default_factory=list)
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution_note: str = ""

    @property
    def active(self) -> bool:
        return self.resolved_at is None

    @property
    def next_action(self) -> EscalationAction | None:
        return next((a for a in self.actions if a.pending), None)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "visit_id": self.visit_id,
            "elder_id": self.elder_id,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "actions": [a.to_dict() for a in self.actions],
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by,
            "resolution_note": self.resolution_note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Escalation":
        return cls(
            id=data["id"],
            visit_id=data["visit_id"],
            elder_id=data["elder_id"],
            reason=data["reason"],
            created_at=datetime.fromisoformat(data["created_at"]),
            created_by=data["created_by"],
            actions=[EscalationAction.from_dict(a) for a in data.get("actions", [])],
            resolved_at=_parse_dt(data.get("resolved_at")),
            resolved_by=data.get("resolved_by"),
            resolution_note=data.get("resolution_note", ""),
        )


@dataclass
class Leave:
    """社工请假记录及其替班信息。"""

    id: str
    worker_id: str
    start: date
    end: date
    reason: str
    substitute_id: str | None
    created_at: datetime
    created_by: str
    reassigned_visit_ids: list[str] = field(default_factory=list)

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "worker_id": self.worker_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "reason": self.reason,
            "substitute_id": self.substitute_id,
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "reassigned_visit_ids": list(self.reassigned_visit_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Leave":
        return cls(
            id=data["id"],
            worker_id=data["worker_id"],
            start=date.fromisoformat(data["start"]),
            end=date.fromisoformat(data["end"]),
            reason=data["reason"],
            substitute_id=data.get("substitute_id"),
            created_at=datetime.fromisoformat(data["created_at"]),
            created_by=data["created_by"],
            reassigned_visit_ids=list(data.get("reassigned_visit_ids", [])),
        )


@dataclass
class PlanVersion:
    """计划变更的不可变版本记录。"""

    version: int
    at: datetime
    operator: str
    action: str
    detail: str
    date_range: tuple[str, str] | None = None
    visit_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "at": self.at.isoformat(),
            "operator": self.operator,
            "action": self.action,
            "detail": self.detail,
            "date_range": list(self.date_range) if self.date_range else None,
            "visit_ids": list(self.visit_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlanVersion":
        dr = data.get("date_range")
        return cls(
            version=data["version"],
            at=datetime.fromisoformat(data["at"]),
            operator=data["operator"],
            action=data["action"],
            detail=data["detail"],
            date_range=tuple(dr) if dr else None,
            visit_ids=list(data.get("visit_ids", [])),
        )


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
