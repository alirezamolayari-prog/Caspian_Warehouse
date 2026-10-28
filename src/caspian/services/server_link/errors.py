"""Errors talking to Caspian Server. Messages are short Persian; the server's own text, HTTP codes and
`X-Error-Code` only go to the log.

401/403 are about the *account* (wrong password, not approved yet, deactivated, must change its
password), never about one request: `ServerAccountProblem` stops the sync and leaves the queue as it
is, so everything is sent once the account is fixed. Other 4xx refuse one operation (`ServerRejected`).
"""

import math

from caspian.core.text import to_persian_digits
from caspian.services.errors import ServiceError

UNREACHABLE = ("سرور در دسترس نیست (اینترنت یا سرور قطع است). درخواست‌ها در صف می‌مانند و پس از "
               "وصل شدن خودکار ارسال می‌شوند.")
NOT_CONFIGURED = "اتصال به سرور تنظیم نشده است (تنظیمات ← اتصال به سرور)."
QUEUE_KEPT = "درخواست‌ها در صف می‌مانند و پس از درست شدن حساب، خودکار ارسال می‌شوند."

ACCOUNT_TEXT = {
    "invalid_credentials": "شماره موبایل (یا نام کاربری) یا رمز عبور حساب سرور اشتباه است.",
    "account_pending": "حساب هنوز توسط مدیر سرور تأیید نشده است. پس از تأیید دوباره امتحان کنید.",
    "account_rejected": "مدیر سرور این حساب را تأیید نکرده است. با مدیر سرور تماس بگیرید.",
    "account_disabled": "این حساب در سرور غیرفعال شده است. با مدیر سرور تماس بگیرید.",
    "password_change_required": "رمز این حساب باید عوض شود؛ تا رمز تازه انتخاب نشود سرور کاری انجام نمی‌دهد.",
    "login_rate_limited": "چند بار پشت سر هم رمز اشتباه وارد شد. {wait} دیگر دوباره امتحان کنید.",
    "forbidden": "این حساب در سرور اجازهٔ این کار را ندارد (نقش حساب باید «انبار» باشد).",
    "session_expired": "زمان ورود این شخص به سرور تمام شده است؛ با ورود دوباره‌اش به برنامه، ارسال "
                       "خودکار ادامه پیدا می‌کند.",
}
ACCOUNT_CODES = frozenset(ACCOUNT_TEXT)

_STATUS_TEXT = {
    400: "سرور درخواست را نامعتبر دانست.",
    404: "این درخواست در سرور پیدا نشد.",
    409: "وضعیت این درخواست در سرور تغییر کرده است.",
    422: "سرور اطلاعات این درخواست را نپذیرفت.",
    429: "سرور فعلاً درخواست بیشتری نمی‌پذیرد؛ کمی بعد دوباره امتحان کنید.",
}


def wait_text(seconds: int | None) -> str:
    """«۳ دقیقه» / «۴۵ ثانیه» for a Retry-After value."""
    if not seconds or seconds <= 0:
        return "چند دقیقه"
    if seconds < 60:
        return f"{to_persian_digits(seconds)} ثانیه"
    return f"{to_persian_digits(math.ceil(seconds / 60))} دقیقه"


def account_message(code: str, retry_after: int | None = None) -> str:
    text = ACCOUNT_TEXT.get(code, ACCOUNT_TEXT["forbidden"])
    return text.format(wait=wait_text(retry_after)) if "{wait}" in text else text


class ServerError(ServiceError):
    pass


class ServerNotConfigured(ServerError):
    def __init__(self) -> None:
        super().__init__(NOT_CONFIGURED)


class ServerUnreachable(ServerError):
    """Connection failed, timed out or the server had an internal error: try again later."""

    def __init__(self, message: str = UNREACHABLE) -> None:
        super().__init__(message)


class ServerAccountProblem(ServerError):
    """The account (not the operation) was refused: `code` is the server's `X-Error-Code`."""

    def __init__(self, code: str = "invalid_credentials", retry_after: int | None = None,
                 status: int = 401) -> None:
        code = code if code in ACCOUNT_CODES else "forbidden"
        super().__init__(account_message(code, retry_after))
        self.code = code
        self.retry_after = retry_after
        self.status = status


ServerAccountRejected = ServerAccountProblem  # the old name, still used by callers and tests


class ServerRejected(ServerError):
    """The server answered and refused this one operation (4xx). Retrying it unchanged won't help."""

    def __init__(self, status: int, detail: str = "", code: str = "") -> None:
        super().__init__(_STATUS_TEXT.get(status, "سرور این کار را نپذیرفت."))
        self.status = status
        self.detail = detail
        self.code = code
