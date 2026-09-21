"""持久化：应用重启后排班进度与隐私授权保持一致。"""

from datetime import datetime, timedelta

from src.models import EscalationStatus, ObservationType, PlanStatus, PrivacyConsent
from src.scheduling import DEFAULT_LADDER, DEFAULT_POLICY
from src.service import CareService
from conftest import MONDAY, NOW, time_windows


def test_state_survives_restart(service, make_elder, tmp_path):
    worker = service.register_worker("王社工", ["东区"])
    elder_a = make_elder(
        name="张桂芳", id_number="110101194001011234", phone="13812345678",
        address="东区幸福里3栋502", windows=time_windows(0, 1),
    )
    elder_b = make_elder(windows=time_windows(0, 3))
    plan_a = service.create_plan(elder_a.id, operator="管理员", start_date=MONDAY, days_ahead=7, now=NOW)
    plan_b = service.create_plan(elder_b.id, operator="管理员", start_date=MONDAY, days_ahead=7, now=NOW)

    visits_a = sorted((v for v in service.visits.values() if v.elder_id == elder_a.id),
                      key=lambda v: v.start)
    mon_visit, tue_visit = visits_a
    # 周一：正常访问
    service.check_in(mon_visit.id, worker.id, at=datetime(2026, 9, 21, 9, 5))
    service.finish_visit(mon_visit.id, [ObservationType.NORMAL], recorded_by="王社工",
                         at=datetime(2026, 9, 21, 9, 30))
    # 周二：发现紧急迹象，立即升级
    service.check_in(tue_visit.id, worker.id, at=datetime(2026, 9, 22, 9, 5))
    service.finish_visit(tue_visit.id, [(ObservationType.EMERGENCY, "老人呼救")],
                         recorded_by="王社工", at=datetime(2026, 9, 22, 9, 10))
    # 暂停另一位老人的计划；收回老人 A 的身份信息查看授权
    service.pause_plan(elder_b.id, reason="住院", operator="管理员", now=NOW)
    service.update_consent(elder_a.id, PrivacyConsent(share_identity_with_worker=False))

    path = tmp_path / "state.json"
    service.save(path)
    loaded = CareService.load(path)  # 模拟应用重启

    # 隐私授权一致
    assert loaded.elders[elder_a.id].consent.share_identity_with_worker is False
    assert loaded.view_elder_identity(worker.id, elder_a.id)["masked"] is True
    # 排班进度一致
    assert loaded.plans[plan_a.id].version == 1
    assert loaded.plans[plan_b.id].status == PlanStatus.PAUSED
    assert loaded.plans[plan_b.id].version == 2
    assert loaded.visit_count(elder_a.id) == 2
    (esc,) = loaded.escalations.values()
    assert esc.status == EscalationStatus.OPEN
    assert esc.reason == "访问中发现紧急迹象"
    # 重启后重复签到仍幂等，不增加访问次数
    again = loaded.check_in(mon_visit.id, worker.id, at=datetime(2026, 9, 21, 9, 40))
    assert again.at == datetime(2026, 9, 21, 9, 5)
    assert loaded.visit_count(elder_a.id) == 2
    # 重启后可恢复暂停的计划
    loaded.resume_plan(elder_b.id, operator="管理员", now=NOW + timedelta(days=3), days_ahead=3)
    assert loaded.plans[plan_b.id].status == PlanStatus.ACTIVE
    resumed = [v for v in loaded.visits.values()
               if v.elder_id == elder_b.id and v.status.value == "PENDING"]
    assert len(resumed) == 1  # 周四 09:00 的授权时段
    # 排班与重试策略配置一致
    assert loaded.policy == DEFAULT_POLICY
    assert loaded.ladder == DEFAULT_LADDER
