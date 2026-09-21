# 独居老人关怀排班

该项目服务于网格员和社区管理工作，负责独居老人关怀排班相关信息的规范化处理与留痕。

运行环境：Python 3.11。代码位于 `src` 目录，配置与数据文件应按部署环境提供。

## 模块结构

- `src/models.py` — 领域模型：老人（含隐私授权）、社工、访问计划（版本留痕）、访问记录、升级事件
- `src/scheduling.py` — 排班策略：照护等级对应的探访频次、未回应阶梯重试规则
- `src/service.py` — `CareService` 领域服务门面（兼容旧入口名 `Service`）
- `src/storage.py` — JSON 原子持久化，重启后恢复排班进度与隐私授权

## 核心能力

- **计划生成**：按老人授权时段、照护等级（HIGH 每日 / MEDIUM 每周三次 / LOW 每周一次）与社工服务片区生成访问计划，同片区社工间按当日负载均衡指派
- **计划变更**：请假替班（`request_leave`）、暂停/恢复（`pause_plan` / `resume_plan`）、授权时段与照护等级调整，每次变更追加版本记录（版本号 + 经办人）
- **访问执行**：签到幂等（重复签到不增加访问次数），一次访问可记录多种观察结果
- **升级规则**：紧急迹象立即升级为紧急事件；普通未回应按阶梯规则重试（默认 2 小时后、次日），耗尽后升级为常规事件
- **隐私控制**：`view_elder_identity` 仅向负责该片区的社工展示经老人授权的身份信息，其余脱敏；管理端可见全部
- **管理端视图**：`admin_dashboard` 返回当天待访、逾期原因（未指派/未签到/请假未替班）、升级状态与下一步安排
- **持久化**：`save` / `CareService.load` 保存并恢复全部状态与策略配置

## 快速示例

```python
from datetime import date, datetime, time
from src import CareService, CareLevel, TimeWindow, ObservationType

svc = CareService()
worker = svc.register_worker("王社工", ["东区"])
elder = svc.register_elder(
    name="张桂芳", id_number="110101194001011234", phone="13812345678",
    address="东区幸福里3栋502", area="东区", care_level=CareLevel.HIGH,
    windows=[TimeWindow(weekday=0, start=time(9, 0), end=time(10, 0))],  # 周一 9-10 点
)
plan = svc.create_plan(elder.id, operator="管理员", start_date=date(2026, 9, 21),
                       days_ahead=7, now=datetime(2026, 9, 21, 8, 0))

visit = next(iter(svc.visits.values()))
svc.check_in(visit.id, worker.id, at=datetime(2026, 9, 21, 9, 5))
svc.finish_visit(visit.id, [ObservationType.NO_RESPONSE], recorded_by="王社工",
                 at=datetime(2026, 9, 21, 9, 10))  # 自动安排阶梯重试

dash = svc.admin_dashboard(day=date(2026, 9, 21), now=datetime(2026, 9, 21, 10, 0))
svc.save("data/state.json")          # 重启后：CareService.load("data/state.json")
```

## 测试

```bash
python3 -m pytest
```
