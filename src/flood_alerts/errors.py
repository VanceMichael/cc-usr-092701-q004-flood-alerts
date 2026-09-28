"""领域错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """所有领域规则违例的基类。"""


class ValidationError(DomainError):
    """入参结构不合法。"""


class NotFoundError(DomainError):
    """引用的预警、任务或观测不存在。"""


class JurisdictionError(DomainError):
    """操作员无权处理该行政区或发起该级别的动作。"""


class StateError(DomainError):
    """当前状态不允许该操作（重复确认、未闭环解除等）。"""


class SegregationError(DomainError):
    """职责分离违例，例如发布人批准自己的解除。"""
