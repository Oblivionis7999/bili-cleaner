"""统一的异常类型。"""


class BiliCleanerError(Exception):
    """工具内部异常的基类。"""

    code = "INTERNAL"

    def __init__(self, message: str = "", code: str | None = None, **extra):
        super().__init__(message or self.code)
        self.message = message or self.code
        if code:
            self.code = code
        self.extra = extra


class AuthRequiredError(BiliCleanerError):
    code = "NOT_LOGGED_IN"


class BiliApiError(BiliCleanerError):
    """B 站接口返回了非 0 code。"""

    def __init__(self, code: int, message: str = "", path: str = ""):
        super().__init__(f"B站接口错误 {code}: {message}", code=f"BILI_{code}")
        self.bili_code = code
        self.path = path


class RiskControlError(BiliApiError):
    """-352 / -412 / -509 / -799 等风控返回。"""

    code = "RISK_CONTROL"


class BackupError(BiliCleanerError):
    code = "BACKUP_FAILED"


class TaskError(BiliCleanerError):
    code = "TASK_ERROR"
