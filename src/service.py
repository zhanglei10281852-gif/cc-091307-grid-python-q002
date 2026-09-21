"""独居老人关怀排班服务门面。

核心能力：
- 按老人授权时段、照护等级与社工服务范围生成访问计划
- 请假替班、计划暂停与恢复，全部留痕（版本号 + 经办人）
- 一次访问可记录多种观察结果；紧急迹象立即升级，普通未回应按阶梯规则重试
- 重复签到幂等，不增加访问次数
- 社工仅可查看自己负责片区且经老人授权的身份信息
- 管理端视图：当天待访、逾期原因、升级状态、下一步安排
- JSON 持久化，重启后排班进度与隐私授权保持一致
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .models import (
    CareLevel,
    CheckIn,
    Elder,
    Escalation,
    EscalationLevel,
    EscalationStatus,
    LeavePeriod,
    Observation,
    ObservationType,
    PlanStatus,
    PrivacyConsent,
    SocialWorker,
    TimeWindow,
    Visit,
    VisitPlan,
    VisitStatus,
    _new_id,
)
from .scheduling import DEFAULT_LADDER, DEFAULT_POLICY, CarePolicy, RetryLadder, generate_slots
from .storage import JsonStore


def _mask_name(name: str) -> str:
    return name[:1] + "*" * (len(name) - 1) if name else "*"


def _mask_id_number(id_number: str) -> str:
    return id_number[:3] + "*" * 6 + id_number[-2:] if len(id_number) > 5 else "***"


def _mask_phone(phone: str) -> str:
    return phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else "***"


class CareService:
    """关怀排班领域服务。"""

    def __init__(self, policy: CarePolicy | None = None, ladder: RetryLadder | None = None):
        self.policy = policy or DEFAULT_POLICY
        self.ladder = ladder or DEFAULT_LADDER
        self.elders: dict[str, Elder] = {}
        self.workers: dict[str, SocialWorker] = {}
        self.plans: dict[str, VisitPlan] = {}
        self.visits: dict[str, Visit] = {}
        self.escalations: dict[str, Escalation] = {}
        self.ready = True

    # ------------------------------------------------------------------
    # 档案登记
    # ------------------------------------------------------------------

    def register_elder(
        self,
        name: str,
        id_number: str,
        phone: str,
        address: str,
        area: str,
        care_level,
        windows: list[TimeWindow] | None = None,
        consent: PrivacyConsent | None = None,
        emergency_contact=None,
    ) -> Elder:
        elder = Elder(
            name=name,
            id_number=id_number,
            phone=phone,
            address=address,
            area=area,
            care_level=care_level,
            windows=list(windows or []),
            consent=consent or PrivacyConsent(),
            emergency_contact=emergency_contact,
        )
        self.elders[elder.id] = elder
        return elder

    def register_worker(self, name: str, areas: list[str]) -> SocialWorker:
        worker = SocialWorker(name=name, areas=list(areas))
        self.workers[worker.id] = worker
        return worker

    # ------------------------------------------------------------------
    # 计划生命周期（全部留痕：版本 + 经办人）
    # ------------------------------------------------------------------

    def create_plan(
        self,
        elder_id: str,
        operator: str,
        start_date: date | None = None,
        days_ahead: int = 7,
        now: datetime | None = None,
    ) -> VisitPlan:
        now = now or datetime.now()
        self._elder(elder_id)
        if any(p.elder_id == elder_id and p.status != PlanStatus.CLOSED for p in self.plans.values()):
            raise ValueError("老人已存在进行中的访问计划")
        plan = VisitPlan(elder_id=elder_id)
        plan.add_revision(operator, "CREATE", "创建访问计划", now)
        self.plans[plan.id] = plan
        self._generate_visits(plan, start_date or now.date(), days_ahead, not_before=now)
        return plan

    def pause_plan(self, elder_id: str, reason: str, operator: str, now: datetime | None = None) -> VisitPlan:
        """暂停计划：取消所有待访，期间不再生成新访问。"""
        now = now or datetime.now()
        plan = self._active_plan(elder_id)
        plan.status = PlanStatus.PAUSED
        cancelled = 0
        for v in self._sorted_visits():
            if v.plan_id == plan.id and v.status == VisitStatus.PENDING:
                v.status = VisitStatus.CANCELLED
                v.cancel_reason = f"计划暂停：{reason}"
                cancelled += 1
        plan.add_revision(operator, "PAUSE", f"{reason}（取消待访 {cancelled} 次）", now)
        return plan

    def resume_plan(
        self,
        elder_id: str,
        operator: str,
        now: datetime | None = None,
        days_ahead: int = 7,
    ) -> VisitPlan:
        """恢复计划：从恢复当天重新生成访问。"""
        now = now or datetime.now()
        plan = self._plan_of(elder_id)
        if plan.status != PlanStatus.PAUSED:
            raise ValueError("计划未处于暂停状态")
        plan.status = PlanStatus.ACTIVE
        created = self._generate_visits(plan, now.date(), days_ahead, not_before=now)
        plan.add_revision(operator, "RESUME", f"恢复排班，生成待访 {created} 次", now)
        return plan

    def close_plan(self, elder_id: str, reason: str, operator: str, now: datetime | None = None) -> VisitPlan:
        now = now or datetime.now()
        plan = self._plan_of(elder_id)
        if plan.status == PlanStatus.CLOSED:
            raise ValueError("计划已关闭")
        plan.status = PlanStatus.CLOSED
        for v in self._sorted_visits():
            if v.plan_id == plan.id and v.status == VisitStatus.PENDING:
                v.status = VisitStatus.CANCELLED
                v.cancel_reason = f"计划关闭：{reason}"
        plan.add_revision(operator, "CLOSE", reason, now)
        return plan

    def update_windows(
        self,
        elder_id: str,
        windows: list[TimeWindow],
        operator: str,
        now: datetime | None = None,
        days_ahead: int = 7,
    ) -> VisitPlan:
        """更新老人授权时段，并据此重排未来待访。"""
        now = now or datetime.now()
        elder = self._elder(elder_id)
        elder.windows = list(windows)
        plan = self._active_plan(elder_id)
        created = self._regenerate_future(plan, now, days_ahead)
        plan.add_revision(operator, "WINDOW_CHANGE", f"更新授权时段，重排待访 {created} 次", now)
        return plan

    def update_care_level(
        self,
        elder_id: str,
        care_level,
        operator: str,
        now: datetime | None = None,
        days_ahead: int = 7,
    ) -> VisitPlan:
        """调整照护等级，并据此重排未来待访。"""
        now = now or datetime.now()
        elder = self._elder(elder_id)
        elder.care_level = care_level
        plan = self._active_plan(elder_id)
        created = self._regenerate_future(plan, now, days_ahead)
        plan.add_revision(operator, "LEVEL_CHANGE", f"照护等级调整为 {care_level.value}，重排待访 {created} 次", now)
        return plan

    # ------------------------------------------------------------------
    # 请假与替班
    # ------------------------------------------------------------------

    def request_leave(
        self,
        worker_id: str,
        start: date,
        end: date,
        operator: str,
        substitute_id: str | None = None,
        now: datetime | None = None,
    ) -> LeavePeriod:
        """登记请假。请假期间的待访优先改派指定替班人，其次改派同片区其他社工。"""
        now = now or datetime.now()
        worker = self._worker(worker_id)
        substitute = self._worker(substitute_id) if substitute_id else None
        period = LeavePeriod(
            start=start, end=end, approved_by=operator, created_at=now,
            substitute_id=substitute_id,
        )
        worker.leave_periods.append(period)
        for v in self._sorted_visits():
            if v.worker_id != worker_id or v.status != VisitStatus.PENDING:
                continue
            if not period.contains(v.start.date()):
                continue
            elder = self.elders[v.elder_id]
            day = v.start.date()
            if substitute and substitute.covers(elder.area) and not substitute.is_on_leave(day):
                new_worker_id = substitute.id
            else:
                new_worker_id = self._assign_worker(elder.area, day, exclude={worker_id})
            v.worker_id = new_worker_id
            plan = self.plans[v.plan_id]
            if new_worker_id:
                detail = f"社工 {worker.name} 请假，访问改派 {self.workers[new_worker_id].name}"
            else:
                detail = f"社工 {worker.name} 请假，无可用替班，访问待指派"
            plan.add_revision(operator, "REASSIGN", detail, now)
        return period

    def reassign_visit(self, visit_id: str, worker_id: str, operator: str, now: datetime | None = None) -> Visit:
        """管理端手工改派某次访问。"""
        now = now or datetime.now()
        visit = self._visit(visit_id)
        worker = self._worker(worker_id)
        elder = self.elders[visit.elder_id]
        if not worker.covers(elder.area):
            raise ValueError("该社工不负责老人所在片区")
        visit.worker_id = worker_id
        self.plans[visit.plan_id].add_revision(operator, "REASSIGN", f"访问改派 {worker.name}", now)
        return visit

    # ------------------------------------------------------------------
    # 访问执行：签到（幂等）、观察记录、升级与阶梯重试
    # ------------------------------------------------------------------

    def check_in(self, visit_id: str, worker_id: str, at: datetime | None = None) -> CheckIn:
        """签到。重复签到返回原记录，不增加访问次数。"""
        at = at or datetime.now()
        visit = self._visit(visit_id)
        if visit.check_in is not None:
            return visit.check_in
        if visit.status != VisitStatus.PENDING:
            raise ValueError(f"访问状态为 {visit.status.value}，不能签到")
        if visit.worker_id != worker_id:
            raise PermissionError("只能为本人负责的访问签到")
        visit.check_in = CheckIn(worker_id=worker_id, at=at)
        visit.status = VisitStatus.CHECKED_IN
        return visit.check_in

    def finish_visit(
        self,
        visit_id: str,
        observations: list,
        recorded_by: str,
        at: datetime | None = None,
    ) -> Visit:
        """结束访问并记录观察结果（一次访问可记录多种）。

        - 含紧急迹象：立即升级为紧急事件；
        - 普通未回应：按阶梯规则安排重试，耗尽后升级为常规事件。
        """
        at = at or datetime.now()
        visit = self._visit(visit_id)
        if visit.status != VisitStatus.CHECKED_IN:
            raise ValueError("访问未签到或已结束")
        if not observations:
            raise ValueError("至少记录一条观察结果")
        for obs in observations:
            obs_type, note = obs if isinstance(obs, tuple) else (obs, "")
            visit.observations.append(
                Observation(type=ObservationType(obs_type), note=note, recorded_at=at)
            )
        visit.finished_by = recorded_by
        types = {o.type for o in visit.observations}
        if ObservationType.EMERGENCY in types:
            visit.status = VisitStatus.ESCALATED
            self._escalate(visit, EscalationLevel.EMERGENCY, "访问中发现紧急迹象", at)
        elif ObservationType.NO_RESPONSE in types:
            visit.status = VisitStatus.DONE
            self._ladder_retry(visit, at)
        else:
            visit.status = VisitStatus.DONE
        return visit

    def visit_count(self, elder_id: str) -> int:
        """老人已完成的访问次数（签到幂等，重复签到不会虚增）。"""
        return sum(
            1 for v in self.visits.values()
            if v.elder_id == elder_id and v.status in (VisitStatus.DONE, VisitStatus.ESCALATED)
        )

    # ------------------------------------------------------------------
    # 隐私：身份信息按片区与授权可见
    # ------------------------------------------------------------------

    def update_consent(self, elder_id: str, consent: PrivacyConsent) -> Elder:
        """更新老人隐私授权（随持久化在重启后保持一致）。"""
        elder = self._elder(elder_id)
        elder.consent = consent
        return elder

    def view_elder_identity(self, requester_id: str, elder_id: str, *, admin: bool = False) -> dict:
        """查看老人身份信息。

        管理端可见全部；社工仅可见自己负责片区且老人授权查看的档案，
        其余情况返回脱敏结果。
        """
        elder = self._elder(elder_id)
        if admin:
            return self._identity(elder, masked=False)
        worker = self._worker(requester_id)
        allowed = worker.covers(elder.area) and elder.consent.share_identity_with_worker
        return self._identity(elder, masked=not allowed)

    @staticmethod
    def _identity(elder: Elder, masked: bool) -> dict:
        if not masked:
            return {
                "masked": False,
                "name": elder.name,
                "id_number": elder.id_number,
                "phone": elder.phone,
                "address": elder.address,
                "area": elder.area,
            }
        return {
            "masked": True,
            "name": _mask_name(elder.name),
            "id_number": _mask_id_number(elder.id_number),
            "phone": _mask_phone(elder.phone),
            "address": f"{elder.area}（详细地址已隐藏）",
            "area": elder.area,
        }

    # ------------------------------------------------------------------
    # 升级事件处理
    # ------------------------------------------------------------------

    def acknowledge_escalation(self, escalation_id: str, operator: str, at: datetime | None = None) -> Escalation:
        esc = self._escalation(escalation_id)
        if esc.status != EscalationStatus.OPEN:
            raise ValueError("仅待处理的升级事件可以签收")
        esc.status = EscalationStatus.ACKED
        esc.acked_by = operator
        esc.acked_at = at or datetime.now()
        return esc

    def resolve_escalation(
        self,
        escalation_id: str,
        operator: str,
        resolution: str,
        at: datetime | None = None,
    ) -> Escalation:
        esc = self._escalation(escalation_id)
        if esc.status == EscalationStatus.RESOLVED:
            raise ValueError("升级事件已闭环")
        if not resolution:
            raise ValueError("闭环必须填写处理结果")
        esc.status = EscalationStatus.RESOLVED
        esc.resolved_by = operator
        esc.resolved_at = at or datetime.now()
        esc.resolution = resolution
        return esc

    # ------------------------------------------------------------------
    # 管理端视图
    # ------------------------------------------------------------------

    def admin_dashboard(self, day: date | None = None, now: datetime | None = None) -> dict:
        """当天待访、逾期原因、升级状态与下一步安排。"""
        now = now or datetime.now()
        day = day or now.date()
        pending = [
            v for v in self._sorted_visits()
            if v.start.date() == day and v.status in (VisitStatus.PENDING, VisitStatus.CHECKED_IN)
        ]
        overdue = [
            {"visit": v, "reason": self._overdue_reason(v)}
            for v in self._sorted_visits()
            if v.is_overdue(now)
        ]
        escalations = sorted(
            (e for e in self.escalations.values() if e.status != EscalationStatus.RESOLVED),
            key=lambda e: e.created_at,
        )
        next_steps: list[dict] = []
        for e in escalations:
            next_steps.append({
                "kind": "escalation",
                "escalation_id": e.id,
                "action": self._escalation_action(e),
            })
        for v in pending:
            if v.retry_of:
                next_steps.append({
                    "kind": "retry",
                    "visit_id": v.id,
                    "action": f"阶梯重试第 {v.retry_step} 步，{v.start:%m-%d %H:%M} 上门",
                })
        for item in overdue:
            next_steps.append({
                "kind": "overdue",
                "visit_id": item["visit"].id,
                "action": "重新指派社工或联系当前社工确认",
            })
        return {
            "day": day,
            "pending": pending,
            "overdue": overdue,
            "escalations": escalations,
            "next_steps": next_steps,
        }

    # ------------------------------------------------------------------
    # 持久化：重启后排班进度与隐私授权保持一致
    # ------------------------------------------------------------------

    def to_state(self) -> dict:
        return {
            "config": {
                "level_weekdays": {lvl.value: list(days) for lvl, days in self.policy.level_weekdays.items()},
                "visit_duration_minutes": int(self.policy.visit_duration.total_seconds() // 60),
                "ladder_delays_minutes": [int(d.total_seconds() // 60) for d in self.ladder.delays],
            },
            "elders": [e.to_dict() for e in self.elders.values()],
            "workers": [w.to_dict() for w in self.workers.values()],
            "plans": [p.to_dict() for p in self.plans.values()],
            "visits": [v.to_dict() for v in self.visits.values()],
            "escalations": [e.to_dict() for e in self.escalations.values()],
        }

    def save(self, path) -> None:
        JsonStore(path).save(self.to_state())

    @classmethod
    def load(cls, path) -> "CareService":
        state = JsonStore(path).load()
        if state is None:
            raise FileNotFoundError(f"未找到状态文件：{path}")
        cfg = state["config"]
        policy = CarePolicy(
            level_weekdays={CareLevel(k): tuple(v) for k, v in cfg["level_weekdays"].items()},
            visit_duration=timedelta(minutes=cfg["visit_duration_minutes"]),
        )
        ladder = RetryLadder(delays=tuple(timedelta(minutes=m) for m in cfg["ladder_delays_minutes"]))
        svc = cls(policy=policy, ladder=ladder)
        svc.elders = {d["id"]: Elder.from_dict(d) for d in state["elders"]}
        svc.workers = {d["id"]: SocialWorker.from_dict(d) for d in state["workers"]}
        svc.plans = {d["id"]: VisitPlan.from_dict(d) for d in state["plans"]}
        svc.visits = {d["id"]: Visit.from_dict(d) for d in state["visits"]}
        svc.escalations = {d["id"]: Escalation.from_dict(d) for d in state["escalations"]}
        return svc

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _generate_visits(self, plan: VisitPlan, start_date: date, days: int, not_before: datetime | None = None) -> int:
        elder = self.elders[plan.elder_id]
        existing = {
            (v.start, v.end)
            for v in self.visits.values()
            if v.plan_id == plan.id and v.status != VisitStatus.CANCELLED
        }
        created = 0
        for start, end in generate_slots(elder, self.policy, start_date, start_date + timedelta(days=days - 1)):
            if not_before is not None and end <= not_before:
                continue  # 已过去的时段不再补排
            if (start, end) in existing:
                continue  # 避免与已有访问重复
            visit = Visit(
                plan_id=plan.id,
                elder_id=elder.id,
                worker_id=self._assign_worker(elder.area, start.date()),
                start=start,
                end=end,
            )
            self.visits[visit.id] = visit
            created += 1
        return created

    def _regenerate_future(self, plan: VisitPlan, now: datetime, days_ahead: int) -> int:
        for v in self._sorted_visits():
            if v.plan_id == plan.id and v.status == VisitStatus.PENDING and v.end > now:
                v.status = VisitStatus.CANCELLED
                v.cancel_reason = "计划调整，重新排班"
        return self._generate_visits(plan, now.date(), days_ahead, not_before=now)

    def _assign_worker(self, area: str, day: date, exclude: set[str] | None = None) -> str | None:
        """在覆盖该片区且当日未请假的社工中，指派当日负载最轻者。"""
        exclude = exclude or set()
        candidates = [
            w for w in self.workers.values()
            if w.id not in exclude and w.covers(area) and not w.is_on_leave(day)
        ]
        if not candidates:
            return None

        def load(w: SocialWorker) -> int:
            return sum(
                1 for v in self.visits.values()
                if v.worker_id == w.id and v.start.date() == day and v.status != VisitStatus.CANCELLED
            )

        candidates.sort(key=lambda w: (load(w), w.id))
        return candidates[0].id

    def _ladder_retry(self, visit: Visit, at: datetime) -> None:
        next_step = visit.retry_step + 1
        if next_step <= len(self.ladder.delays):
            delay = self.ladder.delays[next_step - 1]
            elder = self.elders[visit.elder_id]
            start = at + delay
            worker_id = visit.worker_id
            if worker_id is None or self.workers[worker_id].is_on_leave(start.date()):
                worker_id = self._assign_worker(elder.area, start.date())
            retry = Visit(
                plan_id=visit.plan_id,
                elder_id=visit.elder_id,
                worker_id=worker_id,
                start=start,
                end=start + self.policy.visit_duration,
                retry_of=visit.id,
                retry_step=next_step,
            )
            self.visits[retry.id] = retry
        else:
            attempts = self._chain_length(visit)
            self._escalate(visit, EscalationLevel.ROUTINE, f"连续 {attempts} 次上门未回应", at)

    def _chain_length(self, visit: Visit) -> int:
        length = 1
        while visit.retry_of:
            visit = self.visits[visit.retry_of]
            length += 1
        return length

    def _escalate(self, visit: Visit, level: EscalationLevel, reason: str, at: datetime) -> Escalation:
        esc = Escalation(
            elder_id=visit.elder_id,
            visit_id=visit.id,
            level=level,
            reason=reason,
            created_at=at,
        )
        self.escalations[esc.id] = esc
        return esc

    def _overdue_reason(self, visit: Visit) -> str:
        if visit.worker_id is None:
            return "未指派社工"
        worker = self.workers[visit.worker_id]
        if worker.is_on_leave(visit.start.date()):
            return "社工请假且未安排替班"
        return "社工未签到"

    @staticmethod
    def _escalation_action(esc: Escalation) -> str:
        if esc.level == EscalationLevel.EMERGENCY:
            return "立即联系急救与老人家属，并现场处置" if esc.status == EscalationStatus.OPEN else "紧急事件跟进处置并闭环"
        return "联系老人家属并安排上门核查" if esc.status == EscalationStatus.OPEN else "继续跟进直至闭环"

    def _sorted_visits(self) -> list[Visit]:
        return sorted(self.visits.values(), key=lambda v: (v.start, v.id))

    def _elder(self, elder_id: str) -> Elder:
        try:
            return self.elders[elder_id]
        except KeyError:
            raise KeyError(f"老人不存在：{elder_id}") from None

    def _worker(self, worker_id: str) -> SocialWorker:
        try:
            return self.workers[worker_id]
        except KeyError:
            raise KeyError(f"社工不存在：{worker_id}") from None

    def _visit(self, visit_id: str) -> Visit:
        try:
            return self.visits[visit_id]
        except KeyError:
            raise KeyError(f"访问不存在：{visit_id}") from None

    def _escalation(self, escalation_id: str) -> Escalation:
        try:
            return self.escalations[escalation_id]
        except KeyError:
            raise KeyError(f"升级事件不存在：{escalation_id}") from None

    def _plan_of(self, elder_id: str) -> VisitPlan:
        plans = [p for p in self.plans.values() if p.elder_id == elder_id and p.status != PlanStatus.CLOSED]
        if not plans:
            raise ValueError("老人没有进行中的访问计划")
        return plans[0]

    def _active_plan(self, elder_id: str) -> VisitPlan:
        plan = self._plan_of(elder_id)
        if plan.status != PlanStatus.ACTIVE:
            raise ValueError(f"计划状态为 {plan.status.value}，无法执行该操作")
        return plan


class Service(CareService):
    """兼容既有入口命名的领域服务。"""
