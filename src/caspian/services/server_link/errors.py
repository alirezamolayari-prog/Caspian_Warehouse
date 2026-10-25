"""Errors talking to Caspian Server. Messages are short Persian; the server's own text only goes to
the log."""

from caspian.core.text import to_persian_digits
from caspian.services.errors import ServiceError

UNREACHABLE = ("سرور در دسترس نیست (اینترنت یا سرور قطع است). درخواست‌ها در صف می‌مانند و پس از "
               "وصل شدن خودکار ارسال می‌شوند.")
ACCOUNT_REJECTED = ("حساب کاربری انبار در سرور غیرفعال شده یا رمز آن تغییر کرده است. با مدیر سرور تماس "
                    "بگیرید و سپس در «تنظیمات ← اتصال به سرور» رمز را دوباره وارد کنید.")
NOT_CONFIGURED = "اتصال به سرور تنظیم نشده است (تنظیمات ← اتصال به سرور)."

_STATUS_TEXT = {
    400: "سرور درخواست را نامعتبر دانست.",
    403: "حساب انبار در سرور اجازه این کار را ندارد.",
    404: "این درخواست در سرور پیدا نشد.",
    409: "وضعیت این درخواست در سرور تغییر کرده است.",
    422: "سرور اطلاعات این درخواست را نپذیرفت.",
}


class ServerError(ServiceError):
    pass


class ServerNotConfigured(ServerError):
    def __init__(self) -> None:
        super().__init__(NOT_CONFIGURED)


class ServerUnreachable(ServerError):
    """Connection failed, timed out or the server had an internal error: try again later."""

    def __init__(self, message: str = UNREACHABLE) -> None:
        super().__init__(message)


class ServerAccountRejected(ServerError):
    """Even a fresh login was refused: the account was deactivated or its password changed."""

    def __init__(self) -> None:
        super().__init__(ACCOUNT_REJECTED)


class ServerRejected(ServerError):
    """The server answered and refused this one operation (4xx). Retrying it unchanged won't help."""

    def __init__(self, status: int, detail: str = "") -> None:
        fallback = f"سرور این کار را نپذیرفت (کد {to_persian_digits(status)})."
        super().__init__(_STATUS_TEXT.get(status, fallback))
        self.status = status
        self.detail = detail
