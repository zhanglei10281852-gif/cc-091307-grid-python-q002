"""关怀排班服务的集成式测试（标准库 unittest，无需第三方依赖）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import (  # noqa: E402
    AuthorizationError,
    AuthorizedWindow,
    CareLevel,
    CarePlanService,
    ConsentRequired,
    DuplicateCheckIn,
    Elder,
    InvalidState,
    ObservationKind,
    OutOfServiceArea,
    Store,
    VisitStatus,
    Worker,
)

DAY_WINDOW = AuthorizedWindow(weekdays=[0, 1, 2, 3, 4, 5, 6], start="09:00", end="11:00")
MON = date(2026, 9, 21)  # 周一


class Clock:
    """可控时钟。"""

    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def set(self, t: datetime):
        self.t = t

    def advance(self, **kwargs):
        self.t += timedelta(**kwargs)


def make_service(path: str | None = None, now: datetime | None = None):
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    store = Store(tmp.name if path is None else path)
    clock = Clock(now or datetime(2026, 9, 21, 8, 0))
    return CarePlanService(store, clock), store, clock, tmp.name


def mk_worker(wid="w1", areas=("A",)):
    return Worker(id=wid, name=f"社工{wid}", areas=list(areas))


def mk_elder(eid="e1", area="A", level=CareLevel.LEVEL_3, windows=None, consent=True):
    return Elder(
        id=eid,
        name=f"老人{eid}",
        id_card="110101199001011234",
        phone="13800000000",
        address=f"{area}区幸福里1号",
        area_id=area,
        care_level=level,
        windows=windows or [DAY_WINDOW],
        privacy_consent=consent,
    )


class PlanGenerationTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock, self.path = make_service()

    def test_cadence_follows_care_level(self):
        self.svc.add_worker(mk_worker())
        self.svc.add_elder(mk_elder(level=CareLevel.LEVEL_1))
        created = self.svc.generate_plan("admin", start=MON, end=MON + timedelta(days=14))
        days = sorted(v.scheduled_start.date() for v in created)
        self.assertEqual(days, [MON, MON + timedelta(days=7), MON + timedelta(days=14)])
        for v in created:
            self.assertEqual(v.worker_id, "w1")

    def test_level_2_every_three_days(self):
        self.svc.add_worker(mk_worker())
        self.svc.add_elder(mk_elder(level=CareLevel.LEVEL_2))
        created = self.svc.generate_plan("admin", start=MON, end=MON + timedelta(days=6))
        self.assertEqual(
            sorted(v.scheduled_start.date() for v in created),
            [MON, MON + timedelta(days=3), MON + timedelta(days=6)],
        )

    def test_authorized_weekdays_and_window_times(self):
        # 仅允许周三、周五 14:00-16:00
        window = AuthorizedWindow(weekdays=[2, 4], start="14:00", end="16:00")
        self.svc.add_worker(mk_worker())
        self.svc.add_elder(mk_elder(level=CareLevel.LEVEL_3, windows=[window]))
        created = self.svc.generate_plan("admin", start=MON, end=MON + timedelta(days=13))
        days = sorted(v.scheduled_start.date() for v in created)
        self.assertEqual(days[0], date(2026, 9, 23))       # 周三
        self.assertEqual(days[1], date(2026, 9, 25))       # 周五
        self.assertEqual(days[2], date(2026, 9, 30))       # 下周三
        self.assertEqual(created[0].scheduled_start.hour, 14)

    def test_no_plan_without_consent_or_while_paused(self):
        self.svc.add_worker(mk_worker())
        self.svc.add_elder(mk_elder("e_no_consent", consent=False))
        self.svc.add_elder(mk_elder("e_paused"))
        self.svc.pause_service("e_paused", "admin", "住院")
        created = self.svc.generate_plan("admin", start=MON, end=MON + timedelta(days=2))
        self.assertEqual(created, [])

    def test_worker_area_scoping_and_load_balance(self):
        self.svc.add_worker(mk_worker("w1", areas=("A",)))
        self.svc.add_worker(mk_worker("w2", areas=("A",)))
        self.svc.add_worker(mk_worker("w3", areas=("B",)))
        # B 区只有 w3 能负责；A 区两位社工负载应均衡
        self.svc.add_elder(mk_elder("eA1", area="A"))
        self.svc.add_elder(mk_elder("eA2", area="A"))
        self.svc.add_elder(mk_elder("eB1", area="B"))
        self.svc.generate_plan("admin", start=MON, end=MON)
        assigned = {v.elder_id: v.worker_id for v in self.store.visits.values()}
        self.assertEqual(assigned["eB1"], "w3")
        a_workers = {assigned["eA1"], assigned["eA2"]}
        self.assertEqual(a_workers, {"w1", "w2"})

    def test_overlapping_windows_unassigned_when_all_busy(self):
        self.svc.add_worker(mk_worker())
        self.svc.add_elder(mk_elder("e1"))
        self.svc.add_elder(mk_elder("e2"))
        self.svc.generate_plan("admin", start=MON, end=MON)
        by_elder = {}
        for v in self.store.visits.values():
            by_elder.setdefault(v.elder_id, []).append(v)
        # 同一社工无法同时段上门两个老人：其一未分配，等待管理员调度
        assigned = [visits[0].worker_id for visits in by_elder.values()]
        self.assertIn(None, assigned)

    def test_regeneration_is_idempotent(self):
        self.svc.add_worker(mk_worker())
        self.svc.add_elder(mk_elder())
        first = self.svc.generate_plan("admin", start=MON, end=MON + timedelta(days=2))
        second = self.svc.generate_plan("admin", start=MON, end=MON + timedelta(days=2))
        self.assertEqual(len(second), 0)
        self.assertEqual(
            sorted(v.id for v in first),
            sorted(v.id for v in self.store.visits.values()),
        )


class VisitExecutionTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock, self.path = make_service()
        self.svc.add_worker(mk_worker())
        self.svc.add_elder(mk_elder())
        self.svc.generate_plan("admin", start=MON, end=MON)
        self.visit = next(iter(self.store.visits.values()))

    def test_checkin_is_idempotent_by_key(self):
        self.clock.set(datetime(2026, 9, 21, 9, 5))
        v = self.svc.check_in(self.visit.id, "w1", "scan-evt-1")
        self.assertEqual(v.status, VisitStatus.IN_PROGRESS)
        # 同一事件重放（网络重试）：幂等拒绝，且不产生新访问
        with self.assertRaises(DuplicateCheckIn):
            self.svc.check_in(self.visit.id, "w1", "scan-evt-1")
        self.assertEqual(len(self.store.visits), 1)
        self.assertEqual(len(v.checkins), 1)
        # 换 key 对同一访问再签也算重复签到，访问次数仍为 1
        with self.assertRaises(DuplicateCheckIn):
            self.svc.check_in(self.visit.id, "w1", "scan-evt-2")
        self.assertEqual(self.svc.visit_count("e1"), 1)

    def test_worker_outside_area_cannot_checkin(self):
        self.svc.add_worker(mk_worker("wX", areas=("B",)))
        with self.assertRaises(OutOfServiceArea):
            self.svc.check_in(self.visit.id, "wX", "k1")

    def test_responded_completes_visit(self):
        self.clock.set(datetime(2026, 9, 21, 9, 10))
        self.svc.check_in(self.visit.id, "w1", "k1")
        v = self.svc.record_observations(
            self.visit.id, "w1",
            [(ObservationKind.RESPONDED, "老人精神良好"),
             (ObservationKind.REFUSED_SERVICE, "拒绝测量血压")],
        )
        self.assertEqual(v.status, VisitStatus.COMPLETED)
        self.assertEqual(len(v.observations), 2)
        # 完成后下一轮排班按照护节奏（level_3 次日）到期
        self.svc.generate_plan("admin", start=MON + timedelta(days=1),
                               end=MON + timedelta(days=1))
        self.assertEqual(self.svc.visit_count("e1"), 2)

    def test_refused_service_completes_not_missing(self):
        self.clock.set(datetime(2026, 9, 21, 9, 10))
        self.svc.check_in(self.visit.id, "w1", "k1")
        v = self.svc.record_observations(
            self.visit.id, "w1", [(ObservationKind.REFUSED_SERVICE, "老人表示不需要")]
        )
        self.assertEqual(v.status, VisitStatus.COMPLETED)
        self.assertIsNone(next(iter(self.store.escalations.values()), None))

    def test_cannot_record_before_checkin(self):
        with self.assertRaises(InvalidState):
            self.svc.record_observations(
                self.visit.id, "w1", [ObservationKind.NO_RESPONSE]
            )

    def test_urgent_sign_escalates_immediately(self):
        self.clock.set(datetime(2026, 9, 21, 9, 10))
        self.svc.check_in(self.visit.id, "w1", "k1")
        v = self.svc.record_observations(
            self.visit.id, "w1",
            [(ObservationKind.NO_RESPONSE, "敲门无应答"),
             (ObservationKind.URGENT_SIGN, "窗缝有煤气味")],
        )
        self.assertEqual(v.status, VisitStatus.ESCALATED)
        self.assertEqual(len(self.store.escalations), 1)
        esc = next(iter(self.store.escalations.values()))
        self.assertEqual(esc.reason, "urgent_sign")
        # 紧急动作即刻到期，不等阶梯
        self.assertTrue(all(a.due_at <= self.clock.t for a in esc.actions
                            if "电话" in a.name or "上报" in a.name))
        self.svc.complete_escalation_action(esc.id, "电话联系老人本人及紧急联系人", "w1")
        self.svc.complete_escalation_action(esc.id, "报警(110)并协调入户救援", "admin")
        esc = self.store.escalations[esc.id]
        resolved = self.svc.resolve_escalation(esc.id, "admin", "破门送医，老人平安")
        self.assertFalse(resolved.active)


class NoResponseLadderTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock, self.path = make_service()
        self.svc.add_worker(mk_worker())
        self.svc.add_elder(mk_elder())
        self.svc.generate_plan("admin", start=MON, end=MON)
        self.first = next(iter(self.store.visits.values()))

    def _checkin_record(self, visit, key, no_response_note="敲门无应答"):
        self.svc.check_in(visit.id, "w1", key)
        return self.svc.record_observations(
            visit.id, "w1", [(ObservationKind.NO_RESPONSE, no_response_note)]
        )

    def test_three_attempt_ladder_then_missing_escalation(self):
        # 第 1 轮 09:xx 未回应 → 当日 +30 分钟第 2 轮
        self.clock.set(datetime(2026, 9, 21, 9, 10))
        v1 = self._checkin_record(self.first, "k1")
        self.assertEqual(v1.status, VisitStatus.RESCHEDULED)
        attempts = sorted(self.store.visits.values(), key=lambda x: x.attempt_no)
        self.assertEqual(len(attempts), 2)
        second = attempts[1]
        self.assertEqual(second.attempt_no, 2)
        self.assertEqual(second.group_id, self.first.group_id)
        self.assertEqual(second.scheduled_start.date(), MON)
        self.assertGreaterEqual(
            second.scheduled_start - self.first.scheduled_start, timedelta(minutes=30)
        )

        # 第 2 轮仍未回应 → 下一授权日第 3 轮
        self.clock.set(datetime(2026, 9, 21, 9, 45))
        self._checkin_record(second, "k2")
        third = next(v for v in self.store.visits.values() if v.attempt_no == 3)
        self.assertEqual(third.scheduled_start.date(), MON + timedelta(days=1))

        # 第 3 轮仍未回应 → 立即升级疑似失联
        self.clock.set(datetime(2026, 9, 22, 9, 10))
        v3 = self._checkin_record(third, "k3")
        self.assertEqual(v3.status, VisitStatus.ESCALATED)
        esc = next(iter(self.store.escalations.values()))
        self.assertEqual(esc.reason, "no_response_missing")
        self.assertTrue(len(esc.actions) >= 3)
        # 三轮尝试属于同一次访问，计数只加 1
        self.assertEqual(self.svc.visit_count("e1"), 1)

    def test_responded_on_second_attempt_closes_group(self):
        self.clock.set(datetime(2026, 9, 21, 9, 10))
        self._checkin_record(self.first, "k1")
        second = next(v for v in self.store.visits.values() if v.attempt_no == 2)
        self.clock.set(datetime(2026, 9, 21, 9, 45))
        self.svc.check_in(second.id, "w1", "k2")
        v = self.svc.record_observations(
            second.id, "w1", [(ObservationKind.RESPONDED, "老人午睡没听见")]
        )
        self.assertEqual(v.status, VisitStatus.COMPLETED)
        self.assertEqual(len(self.store.escalations), 0)
        self.assertEqual(self.svc.visit_count("e1"), 1)

    def test_temporary_absence_reschedules_without_escalation(self):
        self.clock.set(datetime(2026, 9, 21, 9, 10))
        self.svc.check_in(self.first.id, "w1", "k1")
        v = self.svc.record_observations(
            self.first.id, "w1",
            [(ObservationKind.TEMPORARY_ABSENCE, "邻居称老人去女儿家，次日回")],
        )
        self.assertEqual(v.status, VisitStatus.RESCHEDULED)
        self.assertEqual(len(self.store.escalations), 0)
        makeup = [x for x in self.store.visits.values() if x.id != self.first.id]
        self.assertEqual(len(makeup), 1)
        self.assertNotEqual(makeup[0].group_id, self.first.group_id)
        self.assertGreaterEqual(makeup[0].scheduled_start.date(), MON + timedelta(days=1))

    def test_retry_paused_when_service_paused_mid_visit(self):
        self.clock.set(datetime(2026, 9, 21, 9, 10))
        self.svc.check_in(self.first.id, "w1", "k1")
        # 已到场后老人住院，家属要求暂停；本轮未回应收尾时，重试轮次应自动挂起
        self.svc.pause_service("e1", "family", "老人突发住院")
        self.svc.record_observations(
            self.first.id, "w1", [ObservationKind.NO_RESPONSE]
        )
        second = next(v for v in self.store.visits.values() if v.attempt_no == 2)
        self.assertEqual(second.status, VisitStatus.PAUSED)


class LeaveAndPauseTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock, self.path = make_service()
        self.svc.add_worker(mk_worker("w1", areas=("A",)))
        self.svc.add_worker(mk_worker("w2", areas=("A", "B")))
        self.svc.add_elder(mk_elder())
        self.svc.generate_plan("admin", start=MON, end=MON + timedelta(days=2))

    def test_leave_reassigns_to_substitute(self):
        leave = self.svc.request_leave(
            "w1", MON, MON + timedelta(days=2), "家中急事", "w2", "scheduler"
        )
        for visit in self.store.visits.values():
            self.assertEqual(visit.worker_id, "w2")
        self.assertEqual(
            sorted(leave.reassigned_visit_ids),
            sorted(v.id for v in self.store.visits.values()),
        )
        # 替班社工可正常签到执行
        visit = next(iter(self.store.visits.values()))
        self.svc.check_in(visit.id, "w2", "k1")

    def test_substitute_must_cover_area(self):
        self.svc.add_worker(mk_worker("w3", areas=("C",)))
        with self.assertRaises(OutOfServiceArea):
            self.svc.request_leave("w1", MON, MON, "事病假", "w3", "scheduler")

    def test_worker_on_leave_not_auto_assigned(self):
        # 已有的 w1 任务被 w2 替班；再生成更远期的计划时 w1 在请假区间内不被选中
        self.svc.request_leave("w1", MON, MON + timedelta(days=5), "培训", "w2", "scheduler")
        self.svc.generate_plan("admin", start=MON + timedelta(days=3),
                               end=MON + timedelta(days=4))
        for visit in self.store.visits.values():
            if MON + timedelta(days=3) <= visit.scheduled_start.date() <= MON + timedelta(days=4):
                self.assertEqual(visit.worker_id, "w2")

    def test_pause_cancels_pending_but_keeps_in_progress(self):
        visits = sorted(self.store.visits.values(), key=lambda v: v.scheduled_start)
        self.svc.check_in(visits[0].id, "w1", "k1")
        self.svc.pause_service("e1", "family", "老人住院两周")
        self.assertEqual(visits[0].status, VisitStatus.IN_PROGRESS)  # 已到场，正常收尾
        self.assertEqual(visits[1].status, VisitStatus.PAUSED)
        # 暂停期间不再生成新计划
        created = self.svc.generate_plan("admin", start=MON + timedelta(days=5),
                                         end=MON + timedelta(days=6))
        self.assertEqual(created, [])

    def test_resume_creates_next_visit(self):
        self.svc.pause_service("e1", "family", "老人住院")
        self.clock.set(datetime(2026, 10, 5, 8, 0))
        _, visit = self.svc.resume_service("e1", "family")
        self.assertIsNotNone(visit)
        self.assertEqual(visit.status, VisitStatus.PENDING)
        self.assertGreaterEqual(visit.scheduled_start.date(), date(2026, 10, 5))

    def test_resume_without_pause_invalid(self):
        with self.assertRaises(InvalidState):
            self.svc.resume_service("e1", "family")


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock, self.path = make_service()
        self.svc.add_worker(mk_worker("w1", areas=("A",)))
        self.svc.add_worker(mk_worker("w2", areas=("B",)))
        self.svc.add_elder(mk_elder())
        self.svc.generate_plan("admin", start=MON, end=MON)

    def test_worker_can_read_own_area_identity(self):
        info = self.svc.get_elder_identity("w1", "e1")
        self.assertEqual(info["name"], "老人e1")
        self.assertEqual(info["id_card"], "110101199001011234")

    def test_cross_area_identity_denied(self):
        with self.assertRaises(AuthorizationError):
            self.svc.get_elder_identity("w2", "e1")

    def test_identity_hidden_without_consent(self):
        self.svc.add_elder(mk_elder("e2", consent=False))
        with self.assertRaises(ConsentRequired):
            self.svc.get_elder_identity("w1", "e2")

    def test_worker_day_view_masks_id_card_and_scopes_area(self):
        view_w1 = self.svc.worker_day_view("w1", MON)
        self.assertEqual(len(view_w1), 1)
        self.assertIn("*", view_w1[0]["elder"]["id_card"])
        self.assertEqual(view_w1[0]["elder"]["id_card"][:4], "1101")
        self.assertEqual(view_w1[0]["elder"]["id_card"][-4:], "1234")
        self.assertEqual(self.svc.worker_day_view("w2", MON), [])

    def test_withdraw_consent_cancels_pending_but_keeps_history(self):
        self.svc.set_privacy_consent("e1", False, "admin")
        for visit in self.store.visits.values():
            self.assertEqual(visit.status, VisitStatus.CANCELLED)
        self.assertEqual(self.svc.history("e1")[-1]["action"], "consent_withdraw")
        # 重新授权后可重新排班
        self.svc.set_privacy_consent("e1", True, "admin")
        created = self.svc.generate_plan("admin", start=MON, end=MON)
        self.assertEqual(len(created), 1)

    def test_cannot_withdraw_consent_during_visit(self):
        visit = next(iter(self.store.visits.values()))
        self.svc.check_in(visit.id, "w1", "k1")
        with self.assertRaises(InvalidState):
            self.svc.set_privacy_consent("e1", False, "admin")


class AdminViewTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock, self.path = make_service()
        self.svc.add_worker(mk_worker())
        self.svc.add_elder(mk_elder())
        self.svc.generate_plan("admin", start=MON, end=MON)
        self.visit = next(iter(self.store.visits.values()))

    def test_overdue_reasons(self):
        # 超过授权时段仍未签到
        self.clock.set(datetime(2026, 9, 21, 11, 30))
        view = self.svc.admin_day_view(MON)
        self.assertEqual(view["overdue_count"], 1)
        self.assertEqual(view["items"][0]["overdue_reason"], "超过授权时段未签到")
        self.assertEqual(view["items"][0]["next_step"], "按授权时段上门签到")

        # 签到后未回填观察
        self.svc.check_in(self.visit.id, "w1", "k1", at=datetime(2026, 9, 21, 9, 5))
        view = self.svc.admin_day_view(MON)
        self.assertEqual(view["items"][0]["overdue_reason"], "已签到但未提交观察结果")

    def test_escalation_status_and_next_step(self):
        self.clock.set(datetime(2026, 9, 21, 9, 10))
        self.svc.check_in(self.visit.id, "w1", "k1")
        self.svc.record_observations(
            self.visit.id, "w1", [(ObservationKind.URGENT_SIGN, "呼救声")]
        )
        view = self.svc.admin_day_view(MON)
        item = view["items"][0]
        self.assertEqual(item["status"], "escalated")
        self.assertIsNotNone(item["escalation"])
        self.assertTrue(item["escalation"]["active"])
        self.assertIn("紧急升级", item["next_step"])
        self.assertEqual(len(view["active_escalations"]), 1)
        # 管理端可见完整身份信息
        self.assertEqual(item["elder"]["id_card"], "110101199001011234")

    def test_total_visits_counts_groups_not_attempts(self):
        self.clock.set(datetime(2026, 9, 21, 9, 10))
        self.svc.check_in(self.visit.id, "w1", "k1")
        self.svc.record_observations(
            self.visit.id, "w1", [ObservationKind.NO_RESPONSE]
        )
        view = self.svc.admin_day_view(MON)
        self.assertEqual(view["total_visits"], 1)  # 两次尝试仍是同一次访问


class VersionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock, self.path = make_service()
        self.svc.add_worker(mk_worker("w1"))
        self.svc.add_worker(mk_worker("w2", areas=("A",)))
        self.svc.add_elder(mk_elder())

    def test_every_change_has_version_and_operator(self):
        self.svc.generate_plan("alice", start=MON, end=MON)
        visit = next(iter(self.store.visits.values()))
        self.clock.set(datetime(2026, 9, 21, 9, 5))
        self.svc.check_in(visit.id, "w1", "k1")
        self.svc.record_observations(
            visit.id, "w1", [ObservationKind.NO_RESPONSE]
        )
        self.svc.request_leave("w1", MON + timedelta(days=30),
                               MON + timedelta(days=30), "调休", "w2", "bob")
        self.svc.pause_service("e1", "carol", "住院")
        self.svc.resume_service("e1", "carol")

        versions = self.svc.history()
        actions = [v["action"] for v in versions]
        self.assertEqual(actions, [
            "generate", "retry_ladder", "leave_reassign", "pause", "resume",
        ])
        numbers = [v["version"] for v in versions]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        self.assertEqual([v["operator"] for v in versions],
                         ["alice", "w1", "bob", "carol", "carol"])
        for v in versions:
            self.assertIn("at", v)

    def test_retry_carries_source_version(self):
        self.svc.generate_plan("alice", start=MON, end=MON)
        visit = next(iter(self.store.visits.values()))
        self.clock.set(datetime(2026, 9, 21, 9, 5))
        self.svc.check_in(visit.id, "w1", "k1")
        self.svc.record_observations(
            visit.id, "w1", [ObservationKind.NO_RESPONSE]
        )
        retry = next(v for v in self.store.visits.values() if v.attempt_no == 2)
        self.assertGreater(retry.source_version, 0)


class PersistenceTests(unittest.TestCase):
    def test_progress_consent_and_escalation_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "careplan.json")
            svc, store, clock, _ = make_service(path)
            svc.add_worker(mk_worker())
            svc.add_elder(mk_elder())
            svc.generate_plan("admin", start=MON, end=MON)
            visit = next(iter(store.visits.values()))
            visit_id = visit.id

            clock.set(datetime(2026, 9, 21, 9, 10))
            svc.check_in(visit_id, "w1", "k1")
            svc.record_observations(
                visit_id, "w1",
                [(ObservationKind.URGENT_SIGN, "屋内电视大声、老人久无应答迹象")],
            )

            # 应用重启：用同一文件重新加载
            store2 = Store(path)
            clock2 = Clock(datetime(2026, 9, 21, 9, 30))
            svc2 = CarePlanService(store2, clock2)

            visit2 = store2.visits[visit_id]
            self.assertEqual(visit2.status, VisitStatus.ESCALATED)
            self.assertTrue(store2.elders["e1"].privacy_consent)
            self.assertEqual(len(visit2.checkins), 1)
            self.assertEqual(len(visit2.observations), 1)
            self.assertEqual(len(store2.escalations), 1)
            self.assertGreaterEqual(len(store2.versions), 2)

            # 进度可继续推进：重复签到依旧被拦截，不增加访问次数
            with self.assertRaises(DuplicateCheckIn):
                svc2.check_in(visit_id, "w1", "k1")
            esc = next(iter(store2.escalations.values()))
            svc2.complete_escalation_action(
                esc.id, "电话联系老人本人及紧急联系人", "w1"
            )
            self.assertFalse(esc.actions[0].pending)

    def test_paused_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "careplan.json")
            svc, _, _, _ = make_service(path)
            svc.add_worker(mk_worker())
            svc.add_elder(mk_elder("e2"))
            svc.pause_service("e2", "admin", "随子女暂住")
            store2 = Store(path)
            self.assertTrue(store2.elders["e2"].paused)
            self.assertEqual(store2.elders["e2"].pause_reason, "随子女暂住")

if __name__ == "__main__":
    unittest.main()
