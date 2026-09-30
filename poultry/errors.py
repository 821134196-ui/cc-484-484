"""业务错误码。"""
from __future__ import annotations


class BusinessError(Exception):
    """所有可预期的业务异常，HTTP 层映射为 409。"""

    code = "business_error"

    def __init__(self, message: str, *, code: str | None = None, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.extra = extra or {}


class NotFound(BusinessError):
    code = "not_found"


class ValidationError(BusinessError):
    code = "validation_error"


class AccessDenied(BusinessError):
    code = "access_denied"


class DuplicateCertError(BusinessError):
    code = "dup_cert"


class WeightMismatchError(BusinessError):
    code = "weight_diff"


class GoodsMismatchError(BusinessError):
    code = "goods_mismatch"


class MixedCellError(BusinessError):
    code = "mixed_cell"


class FrozenError(BusinessError):
    code = "frozen"


class InsufficientError(BusinessError):
    code = "insufficient_weight"


class TempError(BusinessError):
    code = "temp_exceeded"


class LabelLostError(BusinessError):
    code = "label_lost"
