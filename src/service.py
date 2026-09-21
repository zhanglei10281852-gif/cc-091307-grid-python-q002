"""独居老人关怀排班核心服务。

职责：
- 按授权时段、照护等级、社工服务范围生成访问计划；
- 签到幂等、一次访问多条观察、紧急立即升级、未回应阶梯重试；
- 请假替班、暂停 / 恢复；
- 所有计划变更保留版本号与经办人；
- 社工按片区看到脱敏后的身份信息，管理端看到当天全貌。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Callable, Iterable

from .errors import (
    AuthorizationError,
    ConsentRequired,
    DuplicateCheckIn,
    InvalidState,
    NotFound,
    OutOfServiceArea,
    SchedulingConflict,
)
from .models import (
    CADENCE_DAYS,
    OPEN_STATUSES,
    TERMINAL_STATUSES,
    AuthorizedWindow,
    CareLevel,
    CheckIn,
    Elder,
    Escalation,
    EscalationAction,
    Leave,
    Observation,
    ObservationKind,
    PlanVersion,
    Visit,
    VisitStatus,
    Worker,
)
from .storage import Store

VISIT_MINUTES = 20
RETRY_GAP_MINUTES = 30
PLAN_HORIZON_DAYS = 30

# 紧急迹象升级动作：(名称, 距升级时刻的截止偏移)
URGENT_ACTIONS: list[tuple[str, timedelta]] = [
    ("电话联系老人本人及紧急联系人", timedelta(0)),
    ("上报社区值班负责人并到场", timedelta(0)),
    ("报警(110)并协调入户救援", timedelta(minutes=30)),
]

# 连续未回应、疑似失联的升级动作
MISSING_ACTIONS: list[tuple[str, timedelta]] = [
    ("电话联系老人本人及紧急联系人", timedelta(0)),
    ("走访邻居、物业确认行踪", timedelta(minutes=30)),
    ("再次上门并联系社区民警", timedelta(minutes=60)),
]


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _parse_hhmm(value: str) -> time:
    try:
        h, m = value.split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        raise ValueError(f"时间格式应为 HH:MM：{value!r}") from None


class CarePlanService:
    def __init__(self, store: Store, clock: Callable[[], datetime] = datetime.now):
        self.store = store
        self.clock = clock

    def now(self) -> datetime:
        return self.clock()

    def today(self) -> date:
        return self.now().date()

    # ============================================================ 基础档案

    def add_worker(self, worker: Worker) -> Worker:
        self.store.workers[worker.id] = worker
        self.store.save()
        return worker

    def add_elder(self, elder: Elder) -> Elder:
        for window in elder.windows:
            self._validate_window(window)
        self.store.elders[elder.id] = elder
        self.store.save()
        return elder

    @staticmethod
    def _validate_window(window: AuthorizedWindow) -> None:
        if not window.weekdays or any(d < 0 or d > 6 for d in window.weekdays):
            raise ValueError("weekdays 需为 0-6（周一至周日）且非空")
        start, end = _parse_hhmm(window.start), _parse_hhmm(window.end)
        if start >= end:
            raise ValueError("授权时段开始时间必须早于结束时间")

    def set_privacy_consent(
        self, elder_id: str, granted: bool, operator: str, at: datetime | None = None
    ) -> Elder:
        """授予或撤回隐私授权。撤回时取消尚未上门的访问安排（历史留痕保留）。"""
        elder = self._elder(elder_id)
        at = at or self.now()
        if not granted and any(
            v.status is VisitStatus.IN_PROGRESS for v in self.store.visits_for(elder_id)
        ):
            raise InvalidState("存在进行中的访问，暂不能撤回授权")
        elder.privacy_consent = granted
        elder.consent_changed_at = at
        if not granted:
            cancelled = []
            for visit in self.store.visits_for(elder_id):
                if visit.status is VisitStatus.PENDING:
                    visit.status = VisitStatus.CANCELLED
                    visit.change_note = "隐私授权撤回，取消待访安排"
                    cancelled.append(visit.id)
            if cancelled:
                self._record_version("consent_withdraw", operator, "撤回隐私授权，取消待访", cancelled)
        self.store.save()
        return elder

    # ============================================================ 计划生成

    def generate_plan(
        self,
        operator: str,
        start: date | None = None,
        end: date | None = None,
    ) -> list[Visit]:
        """按照护等级节奏与授权时段，为所有已授权且未暂停的老人生成访问。

        已存在安排的日期自动跳过，可安全重复执行（重跑不会产生重复访问）。
        """
        start = start or self.today()
        end = end or (start + timedelta(days=PLAN_HORIZON_DAYS))
        created: list[Visit] = []

        for elder in sorted(self.store.elders.values(), key=lambda e: e.id):
            if not elder.privacy_consent or elder.paused:
                continue
            cadence = CADENCE_DAYS[elder.care_level]
            last_terminal = self._last_terminal_date(elder.id)
            due = max(start, last_terminal + timedelta(days=cadence)) if last_terminal else start
            while due <= end:
                slot = self._window_slot(elder, due)
                if slot is None:
                    due += timedelta(days=1)
                    continue
                if self._has_visit_on(elder.id, due):
                    due += timedelta(days=cadence)
                    continue
                start_dt, end_dt = slot
                worker = self._pick_worker(elder.area_id, due, start_dt, end_dt)
                visit = self._new_visit(elder, start_dt, end_dt, worker)
                visit.change_note = "按排班计划生成"
                self.store.visits[visit.id] = visit
                created.append(visit)
                due += timedelta(days=cadence)

        if created:
            self._record_version(
                "generate",
                operator,
                f"生成 {start} 至 {end} 访问计划",
                [v.id for v in created],
            )
            self.store.save()
        return created

    def _new_visit(
        self,
        elder: Elder,
        start_dt: datetime,
        end_dt: datetime,
        worker: Worker | None,
        *,
        group_id: str | None = None,
        attempt_no: int = 1,
    ) -> Visit:
        return Visit(
            id=_uid("v"),
            group_id=group_id or _uid("g"),
            attempt_no=attempt_no,
            elder_id=elder.id,
            area_id=elder.area_id,
            scheduled_start=start_dt,
            scheduled_end=end_dt,
            worker_id=worker.id if worker else None,
            created_at=self.now(),
        )

    def _window_slot(
        self, elder: Elder, day: date
    ) -> tuple[datetime, datetime] | None:
        windows = sorted(
            (w for w in elder.windows if day.weekday() in w.weekdays),
            key=lambda w: w.start,
        )
        if not windows:
            return None
        window = windows[0]
        start_dt = datetime.combine(day, _parse_hhmm(window.start))
        end_dt = datetime.combine(day, _parse_hhmm(window.end))
        return start_dt, end_dt

    def _next_authorized_day(
        self, elder: Elder, after: date, max_days: int = 14
    ) -> tuple[datetime, datetime] | None:
        for delta in range(1, max_days + 1):
            slot = self._window_slot(elder, after + timedelta(days=delta))
            if slot is not None:
                return slot
        return None

    def _last_terminal_date(self, elder_id: str) -> date | None:
        dates = [
            v.scheduled_start.date()
            for v in self.store.visits_for(elder_id)
            if v.status in TERMINAL_STATUSES
        ]
        return max(dates, default=None)

    def _has_visit_on(self, elder_id: str, day: date) -> bool:
        return any(
            v.scheduled_start.date() == day
            and v.status not in (VisitStatus.CANCELLED, VisitStatus.PAUSED)
            for v in self.store.visits_for(elder_id)
        )

    # ------------------------------------------------ worker 选择 / 冲突

    def _on_leave(self, worker_id: str, day: date) -> bool:
        return any(
            leave.worker_id == worker_id and leave.covers(day)
            for leave in self.store.leaves.values()
        )

    def _busy(self, worker_id: str, start_dt: datetime, end_dt: datetime) -> bool:
        for visit in self.store.visits.values():
            if visit.worker_id != worker_id or visit.status not in OPEN_STATUSES:
                continue
            if visit.scheduled_start < end_dt and start_dt < visit.scheduled_end:
                return True
        return False

    def _pick_worker(
        self, area_id: str, day: date, start_dt: datetime, end_dt: datetime
    ) -> Worker | None:
        candidates = []
        for worker in self.store.workers.values():
            if not worker.active or not worker.covers(area_id):
                continue
            if self._on_leave(worker.id, day) or self._busy(worker.id, start_dt, end_dt):
                continue
            load = sum(
                1
                for v in self.store.visits.values()
                if v.worker_id == worker.id
                and v.scheduled_start.date() == day
                and v.status not in (VisitStatus.CANCELLED, VisitStatus.PAUSED)
            )
            candidates.append((load, worker.id, worker))
        return min(candidates, key=lambda c: (c[0], c[1]))[2] if candidates else None

    # ============================================================ 访问执行

    def check_in(
        self,
        visit_id: str,
        worker_id: str,
        key: str,
        at: datetime | None = None,
    ) -> Visit:
        """社工到场签到。

        key 为业务幂等键（如设备/扫码事件 ID）：同一 key 重试直接返回原访问，
        签到永远不会创建新的访问记录，访问次数只按访问计划（group）统计。
        """
        visit = self._visit(visit_id)
        worker = self._worker(worker_id)
        self._assert_area(worker, visit.area_id)

        if any(c.key == key for c in visit.checkins):
            raise DuplicateCheckIn(visit_id, key)
        if visit.status is VisitStatus.PENDING:
            visit.status = VisitStatus.IN_PROGRESS
        elif visit.status is VisitStatus.IN_PROGRESS:
            # 同一访问已签到，换一个 key 再来仍属重复签到
            raise DuplicateCheckIn(visit_id, key)
        else:
            raise InvalidState(f"访问当前状态 {visit.status.value}，不能签到")

        visit.checkins.append(CheckIn(key=key, worker_id=worker_id, at=at or self.now()))
        self.store.save()
        return visit

    def record_observations(
        self,
        visit_id: str,
        worker_id: str,
        observations: Iterable[Observation | ObservationKind | tuple[ObservationKind, str]],
        at: datetime | None = None,
    ) -> Visit:
        """记录一条或多条观察结果，并按结果推进访问状态。"""
        visit = self._visit(visit_id)
        worker = self._worker(worker_id)
        self._assert_area(worker, visit.area_id)
        if visit.status is not VisitStatus.IN_PROGRESS:
            raise InvalidState("请先签到再记录观察结果")

        now = at or self.now()
        normalized: list[Observation] = []
        for item in observations:
            if isinstance(item, Observation):
                obs = item
                if obs.recorded_at is None:
                    obs.recorded_at = now
                if obs.recorded_by is None:
                    obs.recorded_by = worker_id
            elif isinstance(item, ObservationKind):
                obs = Observation(kind=item, recorded_at=now, recorded_by=worker_id)
            else:
                kind, note = item
                obs = Observation(kind=kind, note=note, recorded_at=now, recorded_by=worker_id)
            normalized.append(obs)
        if not normalized:
            raise ValueError("至少记录一条观察结果")
        visit.observations.extend(normalized)
        kinds = {o.kind for o in normalized}

        elder = self._elder(visit.elder_id)
        if ObservationKind.URGENT_SIGN in kinds:
            self._escalate(visit, elder, "urgent_sign", "现场发现紧急迹象", worker_id, now)
        elif ObservationKind.RESPONDED in kinds or ObservationKind.REFUSED_SERVICE in kinds:
            visit.status = VisitStatus.COMPLETED
            if ObservationKind.REFUSED_SERVICE in kinds:
                visit.change_note = "见到老人本人，老人拒绝本次服务"
            self._record_version(
                "complete", worker_id, "访问完成", [visit.id], at=now
            )
        elif ObservationKind.TEMPORARY_ABSENCE in kinds:
            self._reschedule_after_absence(visit, elder, worker_id, now)
        elif ObservationKind.NO_RESPONSE in kinds:
            self._ladder_retry(visit, elder, worker_id, now)
        # 只有补充说明类观察时保持进行中，等待后续记录

        self.store.save()
        return visit

    # ------------------------------------------------ 未回应阶梯 / 紧急升级

    def _ladder_retry(
        self, visit: Visit, elder: Elder, worker_id: str, now: datetime
    ) -> None:
        """普通未回应的阶梯重试：当日二次上门 → 次日再次上门 → 升级疑似失联。"""
        attempt = visit.attempt_no
        visit.status = VisitStatus.RESCHEDULED
        visit.change_note = f"第 {attempt} 轮上门未回应，按阶梯规则安排重试"

        if attempt >= 3:
            self._escalate(
                visit, elder, "no_response_missing",
                f"连续 {attempt} 轮上门未回应，疑似失联", worker_id, now,
            )
            return

        if attempt == 1:
            retry_start = visit.scheduled_start + timedelta(minutes=RETRY_GAP_MINUTES)
            retry_end = retry_start + timedelta(minutes=VISIT_MINUTES)
            window = self._window_slot(elder, retry_start.date())
            if window is None or retry_end > window[1]:
                slot = self._next_authorized_day(elder, retry_start.date())
                if slot is None:
                    raise SchedulingConflict("授权时段内无法安排重试，请管理员人工介入")
                retry_start, retry_end = slot[0], slot[0] + timedelta(minutes=VISIT_MINUTES)
        else:
            slot = self._next_authorized_day(elder, visit.scheduled_start.date())
            if slot is None:
                raise SchedulingConflict("授权时段内无法安排第三轮上门，请管理员人工介入")
            retry_start = slot[0]
            retry_end = slot[0] + timedelta(minutes=VISIT_MINUTES)

        retry = self._new_visit(
            elder, retry_start, retry_end,
            self._retry_worker(elder, retry_start, retry_end, visit.worker_id),
            group_id=visit.group_id, attempt_no=attempt + 1,
        )
        retry.change_note = f"未回应阶梯第 {attempt + 1} 轮重试"
        retry.source_version = self.store.next_version()
        if elder.paused:
            retry.status = VisitStatus.PAUSED
            retry.change_note += "（关怀服务已暂停）"
        self.store.visits[retry.id] = retry
        self._record_version(
            "retry_ladder", worker_id,
            f"第 {attempt} 轮未回应，安排第 {attempt + 1} 轮重试",
            [visit.id, retry.id], at=now,
        )

    def _retry_worker(
        self,
        elder: Elder,
        start_dt: datetime,
        end_dt: datetime,
        preferred_worker_id: str | None,
    ) -> Worker | None:
        preferred = self.store.workers.get(preferred_worker_id or "")
        day = start_dt.date()
        if (
            preferred is not None
            and preferred.active
            and preferred.covers(elder.area_id)
            and not self._on_leave(preferred.id, day)
            and not self._busy(preferred.id, start_dt, end_dt)
        ):
            return preferred
        return self._pick_worker(elder.area_id, day, start_dt, end_dt)

    def _reschedule_after_absence(
        self, visit: Visit, elder: Elder, worker_id: str, now: datetime
    ) -> None:
        """已确认临时外出：本轮结束，按正常节奏另排一次访问，不走失联升级。"""
        visit.status = VisitStatus.RESCHEDULED
        visit.change_note = "确认老人临时外出，按正常节奏另排访问"
        cadence = CADENCE_DAYS[elder.care_level]
        slot = None
        for delta in range(cadence, cadence + 14):
            slot = self._window_slot(elder, visit.scheduled_start.date() + timedelta(days=delta))
            if slot is not None:
                break
        if slot is None:
            raise SchedulingConflict("授权时段内无法安排回访，请管理员人工介入")
        start_dt, _ = slot
        end_dt = start_dt + timedelta(minutes=VISIT_MINUTES)
        worker = self._retry_worker(elder, start_dt, end_dt, visit.worker_id)
        makeup = self._new_visit(elder, start_dt, end_dt, worker)
        makeup.change_note = "临时外出后的回访安排"
        makeup.source_version = self.store.next_version()
        if elder.paused:
            makeup.status = VisitStatus.PAUSED
            makeup.change_note += "（关怀服务已暂停）"
        self.store.visits[makeup.id] = makeup
        self._record_version(
            "reschedule_absence", worker_id,
            "老人临时外出，另排回访", [visit.id, makeup.id], at=now,
        )

    def _escalate(
        self,
        visit: Visit,
        elder: Elder,
        reason: str,
        summary: str,
        operator: str,
        now: datetime,
    ) -> Escalation:
        visit.status = VisitStatus.ESCALATED
        template = URGENT_ACTIONS if reason == "urgent_sign" else MISSING_ACTIONS
        escalation = Escalation(
            id=_uid("e"),
            visit_id=visit.id,
            elder_id=elder.id,
            reason=reason,
            created_at=now,
            created_by=operator,
            actions=[
                EscalationAction(name=name, due_at=now + offset)
                for name, offset in template
            ],
        )
        self.store.escalations[escalation.id] = escalation
        self._record_version(
            "escalate", operator, f"升级：{summary}", [visit.id], at=now
        )
        return escalation

    def complete_escalation_action(
        self, escalation_id: str, action_name: str, operator: str, at: datetime | None = None
    ) -> Escalation:
        escalation = self.store.escalations.get(escalation_id)
        if escalation is None:
            raise NotFound(f"升级单不存在：{escalation_id}")
        for action in escalation.actions:
            if action.name == action_name and action.pending:
                action.done_at = at or self.now()
                action.done_by = operator
                self.store.save()
                return escalation
        raise NotFound(f"升级单中没有待办动作：{action_name}")

    def resolve_escalation(
        self, escalation_id: str, operator: str, note: str, at: datetime | None = None
    ) -> Escalation:
        escalation = self.store.escalations.get(escalation_id)
        if escalation is None:
            raise NotFound(f"升级单不存在：{escalation_id}")
        if not escalation.active:
            raise InvalidState("升级单已结案")
        at = at or self.now()
        for action in escalation.actions:
            if action.pending:
                action.done_at = at
                action.done_by = operator
        escalation.resolved_at = at
        escalation.resolved_by = operator
        escalation.resolution_note = note
        self._record_version(
            "escalation_resolve", operator, f"升级单结案：{note}",
            [escalation.visit_id], at=at,
        )
        self.store.save()
        return escalation

    # ============================================================ 请假替班

    def request_leave(
        self,
        worker_id: str,
        start: date,
        end: date,
        reason: str,
        substitute_id: str,
        operator: str,
        at: datetime | None = None,
    ) -> Leave:
        if end < start:
            raise ValueError("请假结束日期不能早于开始日期")
        worker = self._worker(worker_id)
        substitute = self._worker(substitute_id)
        if not substitute.active:
            raise InvalidState("替班社工不在在岗状态")

        affected = [
            v
            for v in self.store.visits.values()
            if v.worker_id == worker_id
            and v.status in OPEN_STATUSES
            and start <= v.scheduled_start.date() <= end
        ]
        uncovered = sorted({v.area_id for v in affected if not substitute.covers(v.area_id)})
        if uncovered:
            raise OutOfServiceArea(
                f"替班社工不覆盖片区：{', '.join(uncovered)}，无法替班"
            )

        leave = Leave(
            id=_uid("l"),
            worker_id=worker_id,
            start=start,
            end=end,
            reason=reason,
            substitute_id=substitute_id,
            created_at=at or self.now(),
            created_by=operator,
        )
        for visit in affected:
            visit.worker_id = substitute_id
            visit.change_note = f"{worker.name} 请假（{start}~{end}），由 {substitute.name} 替班"
            leave.reassigned_visit_ids.append(visit.id)
        self.store.leaves[leave.id] = leave
        self._record_version(
            "leave_reassign", operator,
            f"{worker.name} 请假，{substitute.name} 替班，改派 {len(affected)} 个访问",
            leave.reassigned_visit_ids,
        )
        self.store.save()
        return leave

    # ============================================================ 暂停 / 恢复

    def pause_service(
        self,
        elder_id: str,
        operator: str,
        reason: str,
        until: date | None = None,
        at: datetime | None = None,
    ) -> Elder:
        elder = self._elder(elder_id)
        at = at or self.now()
        elder.paused = True
        elder.paused_at = at
        elder.pause_reason = reason
        elder.pause_until = until
        affected = []
        for visit in self.store.visits_for(elder_id):
            # 进行中的访问社工已到场，让其正常收尾，仅挂起尚未上门的安排
            if visit.status is VisitStatus.PENDING:
                visit.status = VisitStatus.PAUSED
                visit.change_note = f"关怀服务暂停：{reason}"
                affected.append(visit.id)
        self._record_version("pause", operator, f"暂停服务：{reason}", affected, at=at)
        self.store.save()
        return elder

    def resume_service(
        self, elder_id: str, operator: str, at: datetime | None = None
    ) -> tuple[Elder, Visit | None]:
        """恢复服务并立即安排下一次到期访问。"""
        elder = self._elder(elder_id)
        if not elder.paused:
            raise InvalidState("该老人未处于暂停状态")
        at = at or self.now()
        elder.paused = False
        elder.resumed_at = at
        elder.pause_reason = None
        elder.pause_until = None

        cadence = CADENCE_DAYS[elder.care_level]
        next_visit = None
        for delta in range(0, cadence + 14):
            day = at.date() + timedelta(days=delta)
            slot = self._window_slot(elder, day)
            if slot is None or self._has_visit_on(elder.id, day):
                continue
            start_dt, _ = slot
            end_dt = start_dt + timedelta(minutes=VISIT_MINUTES)
            worker = self._pick_worker(elder.area_id, day, start_dt, end_dt)
            next_visit = self._new_visit(elder, start_dt, end_dt, worker)
            next_visit.change_note = "恢复服务后的首次访问"
            next_visit.source_version = self.store.next_version()
            self.store.visits[next_visit.id] = next_visit
            break

        self._record_version(
            "resume", operator, "恢复服务",
            [next_visit.id] if next_visit else [], at=at,
        )
        self.store.save()
        return elder, next_visit

    # ============================================================ 视图

    def get_elder_identity(self, worker_id: str, elder_id: str) -> dict:
        """社工只能读取自己片区、且已授权老人的身份信息。"""
        worker = self._worker(worker_id)
        elder = self._elder(elder_id)
        if not worker.covers(elder.area_id):
            raise AuthorizationError("无权查看非本人负责片区的老人信息")
        if not elder.privacy_consent:
            raise ConsentRequired("老人未授予隐私授权，身份信息暂不可见")
        return self._identity_dict(elder, mask=False)

    def worker_day_view(self, worker_id: str, day: date | None = None) -> list[dict]:
        """社工端：仅本人当日任务，跨片区数据不返回。"""
        worker = self._worker(worker_id)
        day = day or self.today()
        now = self.now()
        result = []
        for visit in sorted(self.store.pending_for_worker(worker_id, day),
                            key=lambda v: v.scheduled_start):
            elder = self._elder(visit.elder_id)
            item = {
                "visit_id": visit.id,
                "scheduled_start": visit.scheduled_start.isoformat(),
                "scheduled_end": visit.scheduled_end.isoformat(),
                "status": visit.status.value,
                "attempt_no": visit.attempt_no,
                "group_id": visit.group_id,
                "checkin_count": len(visit.checkins),
                "observations": [o.to_dict() for o in visit.observations],
                "overdue_reason": self._overdue_reason(visit, now),
                "next_step": self._next_step(visit, now),
            }
            item["elder"] = (
                self._identity_dict(elder, mask=True)
                if elder.privacy_consent and worker.covers(elder.area_id)
                else {"id": elder.id, "visible": False, "reason": "未授权或非本片区"}
            )
            result.append(item)
        return result

    def admin_day_view(self, day: date | None = None) -> dict:
        """管理端：当天待访、逾期原因、升级状态与下一步安排。"""
        day = day or self.today()
        now = self.now()
        items = []
        for visit in sorted(
            (v for v in self.store.visits.values() if v.scheduled_start.date() == day),
            key=lambda v: (v.scheduled_start, v.id),
        ):
            elder = self._elder(visit.elder_id)
            worker = self.store.workers.get(visit.worker_id or "")
            escalation = self._escalation_for(visit.id)
            items.append({
                "visit_id": visit.id,
                "group_id": visit.group_id,
                "attempt_no": visit.attempt_no,
                "elder": self._identity_dict(elder, mask=False),
                "worker_name": worker.name if worker else "未分配",
                "scheduled_start": visit.scheduled_start.isoformat(),
                "scheduled_end": visit.scheduled_end.isoformat(),
                "status": visit.status.value,
                "overdue_reason": self._overdue_reason(visit, now),
                "change_note": visit.change_note,
                "observations": [o.to_dict() for o in visit.observations],
                "escalation": self._escalation_brief(escalation) if escalation else None,
                "next_step": self._next_step(visit, now),
            })

        active_escalations = [
            self._escalation_brief(e)
            for e in self.store.escalations.values()
            if e.active and e.created_at.date() <= day
        ]
        return {
            "date": day.isoformat(),
            "total_visits": len({v.group_id for v in self.store.visits.values()
                                 if v.scheduled_start.date() == day}),
            "pending_count": sum(1 for i in items if i["status"] in ("pending", "in_progress")),
            "overdue_count": sum(1 for i in items if i["overdue_reason"]),
            "items": items,
            "active_escalations": active_escalations,
        }

    # ------------------------------------------------ 视图辅助

    def _overdue_reason(self, visit: Visit, now: datetime) -> str | None:
        if now <= visit.scheduled_end:
            return None
        if visit.status is VisitStatus.PENDING:
            return "超过授权时段未签到"
        if visit.status is VisitStatus.IN_PROGRESS:
            return "已签到但未提交观察结果"
        return None

    def _next_step(self, visit: Visit, now: datetime) -> str:
        escalation = self._escalation_for(visit.id)
        if escalation is not None and escalation.active:
            action = escalation.next_action
            who = "紧急" if escalation.reason == "urgent_sign" else "失联"
            if action:
                return f"【{who}升级】{action.name}（截止 {action.due_at:%H:%M}）"
            return f"【{who}升级】等待结案"
        if visit.status is VisitStatus.PENDING:
            if visit.worker_id is None:
                return "无可用社工，等待管理员调度"
            return "按授权时段上门签到"
        if visit.status is VisitStatus.IN_PROGRESS:
            return "提交观察结果"
        if visit.status is VisitStatus.RESCHEDULED:
            follow_up = self._follow_up(visit)
            if follow_up is not None:
                return (
                    f"第 {follow_up.attempt_no} 轮重试："
                    f"{follow_up.scheduled_start:%m-%d %H:%M} 上门"
                )
            return "等待安排重试"
        if visit.status is VisitStatus.PAUSED:
            return "服务暂停中，恢复后重新排班"
        if visit.status is VisitStatus.CANCELLED:
            return "已取消"
        return "本轮已闭环"

    def _follow_up(self, visit: Visit) -> Visit | None:
        return next(
            (
                v
                for v in self.store.visits.values()
                if v.group_id == visit.group_id
                and v.attempt_no > visit.attempt_no
            ),
            None,
        )

    def _escalation_for(self, visit_id: str) -> Escalation | None:
        return next(
            (e for e in self.store.escalations.values() if e.visit_id == visit_id),
            None,
        )

    @staticmethod
    def _escalation_brief(escalation: Escalation) -> dict:
        action = escalation.next_action
        return {
            "id": escalation.id,
            "reason": escalation.reason,
            "active": escalation.active,
            "created_at": escalation.created_at.isoformat(),
            "next_action": action.name if action else None,
            "next_action_due": action.due_at.isoformat() if action else None,
            "resolved_at": escalation.resolved_at.isoformat() if escalation.resolved_at else None,
        }

    @staticmethod
    def _identity_dict(elder: Elder, *, mask: bool) -> dict:
        id_card = elder.id_card
        if mask and len(id_card) >= 8:
            id_card = id_card[:4] + "*" * (len(id_card) - 8) + id_card[-4:]
        return {
            "id": elder.id,
            "name": elder.name,
            "id_card": id_card,
            "phone": elder.phone,
            "address": elder.address,
            "area_id": elder.area_id,
            "care_level": elder.care_level.value,
        }

    # ============================================================ 统计 / 留痕

    def visit_count(self, elder_id: str) -> int:
        """访问次数按访问 group 统计：重试、替班、重复签到都不增加次数。"""
        return len({v.group_id for v in self.store.visits_for(elder_id)})

    def history(self, elder_id: str | None = None) -> list[dict]:
        versions = self.store.versions
        if elder_id is not None:
            visit_ids = {v.id for v in self.store.visits_for(elder_id)}
            versions = [
                ver for ver in versions
                if any(vid in visit_ids for vid in ver.visit_ids)
                or ver.action in ("pause", "resume")
            ]
        return [ver.to_dict() for ver in versions]

    # ============================================================ 内部工具

    def _record_version(
        self,
        action: str,
        operator: str,
        detail: str,
        visit_ids: list[str],
        at: datetime | None = None,
    ) -> PlanVersion:
        version = PlanVersion(
            version=self.store.next_version(),
            at=at or self.now(),
            operator=operator,
            action=action,
            detail=detail,
            visit_ids=list(visit_ids),
        )
        self.store.versions.append(version)
        return version

    def _elder(self, elder_id: str) -> Elder:
        elder = self.store.elders.get(elder_id)
        if elder is None:
            raise NotFound(f"老人档案不存在：{elder_id}")
        return elder

    def _worker(self, worker_id: str) -> Worker:
        worker = self.store.workers.get(worker_id)
        if worker is None:
            raise NotFound(f"社工不存在：{worker_id}")
        return worker

    def _visit(self, visit_id: str) -> Visit:
        visit = self.store.visits.get(visit_id)
        if visit is None:
            raise NotFound(f"访问不存在：{visit_id}")
        return visit

    @staticmethod
    def _assert_area(worker: Worker, area_id: str) -> None:
        if not worker.covers(area_id):
            raise OutOfServiceArea(f"社工 {worker.name} 不负责片区 {area_id}")
