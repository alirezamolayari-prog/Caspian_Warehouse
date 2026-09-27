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
