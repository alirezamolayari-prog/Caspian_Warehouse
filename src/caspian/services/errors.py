class ServiceError(Exception):
    """Base for errors whose message is safe and meaningful to show the user (Persian)."""

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""


class ValidationError(ServiceError):
    pass


class AuthenticationError(ServiceError):
    pass


class PermissionDenied(ServiceError):
    pass


class ApprovalError(ServiceError):
    """Missing, invalid, reused or mismatched admin approval for a protected action."""


class NotFound(ServiceError):
    pass


class ConcurrencyError(ServiceError):
    """The record changed (another user/PC) since it was loaded for editing."""

    def __init__(self, message: str = "این رکورد در این فاصله توسط کاربر دیگری تغییر کرده است. "
                 "لطفاً دوباره باز کنید و تغییرات را اعمال کنید.") -> None:
        super().__init__(message)
