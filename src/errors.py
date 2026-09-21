"""领域异常。"""


class CarePlanError(Exception):
    """所有关怀排班领域错误的基类。"""


class NotFound(CarePlanError):
    """实体不存在。"""


class ConsentRequired(CarePlanError):
    """老人未授予隐私授权，不能安排或查看身份信息。"""


class OutOfServiceArea(CarePlanError):
    """社工不在该老人片区的服务范围内。"""


class SchedulingConflict(CarePlanError):
    """同一社工同一时段已有安排。"""


class InvalidState(CarePlanError):
    """当前状态不允许该操作（如暂停后再签到）。"""


class DuplicateCheckIn(CarePlanError):
    """重复签到：幂等命中，不会增加访问次数。"""

    def __init__(self, visit_id: str, key: str):
        super().__init__(f"重复签到已忽略：visit={visit_id} key={key}")
        self.visit_id = visit_id
        self.key = key


class AuthorizationError(CarePlanError):
    """查看/操作权限不足（如跨片区查看身份信息）。"""
