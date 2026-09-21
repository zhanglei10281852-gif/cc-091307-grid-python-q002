# 独居老人关怀排班

服务于网格员和社区管理工作的独居老人关怀排班模块：按老人**授权时段**、**照护等级**和社工**服务范围**生成上门计划，覆盖签到、观察记录、未回应重试、紧急升级、请假替班、暂停恢复、版本留痕与片区隐私控制。

运行环境：Python 3.11，仅使用标准库。代码位于 `src` 目录，测试位于 `tests` 目录。

## 快速开始

```python
from datetime import date
from src import (
    Store, CarePlanService, Worker, Elder, AuthorizedWindow, CareLevel, ObservationKind,
)

svc = CarePlanService(Store("data/careplan.json"))   # JSON 原子落盘，重启状态一致

svc.add_worker(Worker(id="w1", name="李社工", areas=["A片区"]))
svc.add_elder(Elder(
    id="e1", name="王奶奶", id_card="110101194001011234", phone="...",
    address="...", area_id="A片区", care_level=CareLevel.LEVEL_3,   # 高风险：每日
    windows=[AuthorizedWindow(weekdays=[0,1,2,3,4,5,6], start="09:00", end="11:00")],
    privacy_consent=True,                                           # 无授权不排班
))

svc.generate_plan(operator="admin")                 # 可重复执行，不会重复排班

svc.check_in("v_xxx", worker_id="w1", key="scan-event-001")  # key 幂等
svc.record_observations("v_xxx", "w1", [
    (ObservationKind.NO_RESPONSE, "敲门无应答"),     # 一次访问可记多条观察
])
```

## 核心规则

| 需求 | 实现 |
| --- | --- |
| 按授权时段 / 等级 / 片区排班 | `generate_plan`：等级决定节奏（7/3/1 天），仅在授权星期与时段内安排，社工必须覆盖片区且无时间冲突，负载均衡选人；全员忙时该访问留空待调度 |
| 请假替班 | `request_leave`：校验替班人覆盖全部受影响片区，改派请假区间内待访；请假区间内不再自动排班 |
| 暂停 / 恢复 | `pause_service` 挂起未上门安排（进行中的访问正常收尾）；`resume_service` 在最近授权时段补排首次访问 |
| 一次访问多种观察 | `record_observations` 接受多条 `ObservationKind`：应答、未回应、临时外出、拒绝服务、紧急迹象 |
| 紧急立即升级 | 出现 `URGENT_SIGN` 立即生成升级单（联系家属/到场/报警，含截止时间），不等重试 |
| 普通未回应阶梯 | 当日二次上门（+30 分钟且在授权时段内）→ 下一授权日第三次上门 → 仍未回应则按疑似失联升级 |
| 临时外出 vs 失联 | `TEMPORARY_ABSENCE` 只改约回访、不走失联；`REFUSED_SERVICE` 视为正常闭环，不重复打扰 |
| 重复签到不增加访问次数 | 签到按业务幂等键去重；同一访问重复签到被拒绝。访问次数按访问组 `group_id` 统计，多轮重试共享一个组 |
| 变更留版本与经办人 | 每次计划变更追加不可变 `PlanVersion`（版本号、时间、经办人、动作、关联访问）；重试/回访带 `source_version` |
| 片区隐私 | 社工只能查本片区且已授权老人的身份信息；社工端列表身份证号脱敏，跨片区返回为空；撤回授权取消待访（历史留痕保留），访问进行中禁止撤回 |
| 管理端视图 | `admin_day_view`：当天待访、逾期原因（未签到/未回填观察）、升级状态与下一步动作、活动升级单 |
| 重启一致 | 单文件 JSON + 临时文件 `os.replace` 原子写；排班进度、观察、升级单、授权与暂停状态全部持久化 |

## 测试

```bash
python3 -m unittest tests.test_service -v
```

36 个用例覆盖排班节奏与授权时段、签到幂等、三轮未回应阶梯、紧急升级、临时外出改约、请假替班、暂停恢复、隐私边界、管理端视图、版本链与重启恢复。
