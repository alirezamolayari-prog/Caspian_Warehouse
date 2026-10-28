"""Personal Caspian Server accounts: register with an SMS code, log in with mobile + password,
forgotten password, change password, «who am I». Exactly the server's contract
(Caspian-Server docs/owner-auth-api.md, requests-api.md). Each error is shown as one Persian sentence
chosen by `X-Error-Code`; the server's raw text and HTTP status only go to the log."""

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from caspian.core.persian import normalize_mobile
from caspian.core.text import to_ascii_digits
from caspian.services.server_link.client import error_code, retry_after
from caspian.services.server_link.errors import (
    ACCOUNT_TEXT,
    ServerError,
    ServerUnreachable,
    wait_text,
)

log = logging.getLogger(__name__)

DEFAULT_URL = "https://api.caspianmoblserver.ir"
TIMEOUT_SECONDS = 20.0  # an SMS request waits for the SMS provider (up to ~15 s)
REGISTER, RESET = "register", "reset_password"
_LATIN = re.compile(r"[A-Za-z]")

AUTH_TEXT = {
    **ACCOUNT_TEXT,
    "invalid_credentials": "شماره موبایل یا رمز عبور اشتباه است.",
    "invalid_mobile": "شماره موبایل درست نیست؛ مثلاً ۰۹۱۲۳۴۵۶۷۸۹ بنویسید.",
    "invalid_code": "کد ۶ رقمی پیامک را کامل وارد کنید.",
    "code_wrong": "کد اشتباه است.",
    "code_attempts_exceeded": "کد چند بار اشتباه وارد شد و دیگر کار نمی‌کند؛ «ارسال دوباره» را بزنید.",
    "code_expired": "کد منقضی شده یا قبلاً استفاده شده است؛ «ارسال دوباره» را بزنید.",
    "verification_invalid": "زمان این مرحله تمام شد؛ لطفاً دوباره از اول شروع کنید.",
    "otp_cooldown": "کد قبلی همین الان فرستاده شد؛ {wait} دیگر دوباره امتحان کنید.",
    "otp_rate_limited": "تعداد درخواست کد زیاد بوده است؛ {wait} دیگر دوباره امتحان کنید.",
    "otp_mobile_daily_limit": "سقف ارسال کد امروز برای این شماره پر شده است؛ فردا دوباره امتحان کنید.",
    "otp_ip_daily_limit": "سقف ارسال کد امروز برای این شبکه پر شده است؛ فردا دوباره امتحان کنید.",
    "otp_global_daily_limit": "امروز پیامک بیشتری ارسال نمی‌شود؛ فردا دوباره امتحان کنید یا با مدیر "
                              "تماس بگیرید.",
    "sms_not_sent": "پیامک ارسال نشد؛ {wait} دیگر دوباره امتحان کنید.",
    "mobile_taken": "این شماره قبلاً ثبت‌نام کرده است. وارد شوید یا «رمز را فراموش کرده‌ام» را بزنید.",
    "account_not_found": "حسابی با این شماره نیست. ابتدا ثبت‌نام کنید.",
    "sms_login_disabled": "ورود با پیامک فعال نیست؛ با رمز عبور وارد شوید.",
}
_FIELD_TEXT = {
    "first_name": "نام و نام خانوادگی را با حروف فارسی بنویسید (۲ تا ۵۰ حرف)",
    "last_name": "نام و نام خانوادگی را با حروف فارسی بنویسید (۲ تا ۵۰ حرف)",
    "password": "رمز عبور قابل قبول نیست: حداقل ۸ حرف، ساده نباشد و حداقل ۴ حرف متفاوت داشته باشد.",
    "new_password": "رمز عبور قابل قبول نیست: حداقل ۸ حرف، ساده نباشد و حداقل ۴ حرف متفاوت داشته باشد.",
    "mobile": AUTH_TEXT["invalid_mobile"],
    "code": AUTH_TEXT["invalid_code"],
}
GENERIC = "سرور این درخواست را نپذیرفت؛ دوباره امتحان کنید."


class AuthError(ServerError):
    """`code` = the server's X-Error-Code ("" for a body validation error); `field` = the form field
    to mark red, when the server named one."""

    def __init__(self, message: str, code: str = "", field: str = "", retry_after: int | None = None,
                 status: int = 0) -> None:
        super().__init__(message)
        self.code, self.field, self.retry_after, self.status = code, field, retry_after, status


@dataclass(frozen=True)
class CodeSent:
    expires_in: int
    resend_after: int


@dataclass(frozen=True)
class LoginAnswer:
    access_token: str
    role: str
    full_name: str
    must_change_password: bool


def _persian_detail(detail: Any) -> str:
    """The server's own sentence, if it is plain Persian (no English, no codes)."""
    text = str(detail or "").strip()
    return text if text and not _LATIN.search(text) else ""


def _error(response: httpx.Response) -> AuthError:
    code, wait = error_code(response), retry_after(response)
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    log.warning("Caspian Server auth refused: %s %s %s", response.status_code, code, str(detail)[:300])
    if response.status_code == 422 and isinstance(detail, list):  # FastAPI body validation
        first = detail[0] if detail else {}
        loc = first.get("loc") or []
        field = str(loc[1]) if len(loc) > 1 else ""
        message = _persian_detail(str(first.get("msg", "")).removeprefix("Value error, "))
        return AuthError(message or _FIELD_TEXT.get(field, GENERIC), "", field, None, 422)
    if code == "code_wrong":
        return AuthError(_persian_detail(detail) or AUTH_TEXT["code_wrong"], code, "code", wait,
                         response.status_code)
    if code in AUTH_TEXT:
        text = AUTH_TEXT[code]
        text = text.format(wait=wait_text(wait)) if "{wait}" in text else text
        field = "mobile" if code in ("invalid_mobile", "mobile_taken", "account_not_found") else (
            "code" if code.startswith("code_") or code == "invalid_code" else "")
        return AuthError(text, code, field, wait, response.status_code)
    return AuthError(_persian_detail(detail) or GENERIC, code, "", wait, response.status_code)


class AuthApi:
    """Calls that need no login (and the few that take a personal token)."""

    def __init__(self, base_url: str = DEFAULT_URL, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = TIMEOUT_SECONDS) -> None:
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        self._transport, self._timeout = transport, timeout

    async def _call(self, method: str, path: str, *, json: Any = None, data: Any = None,
                    token: str | None = None, ok: tuple[int, ...] = (200, 201)) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self._timeout,
                                         transport=self._transport) as http:
                response = await http.request(method, path, json=json, data=data, headers=headers)
        except httpx.HTTPError as exc:
            log.info("Caspian Server unreachable (%s %s): %s", method, path, exc)
            raise ServerUnreachable("سرور در دسترس نیست؛ اینترنت را بررسی کنید و دوباره امتحان کنید.") \
                from exc
        if response.status_code >= 500 and error_code(response) != "sms_not_sent":
            log.warning("Caspian Server error %s on %s %s: %s", response.status_code, method, path,
                        response.text[:300])
            raise ServerUnreachable("سرور الان پاسخ نمی‌دهد؛ چند دقیقه بعد دوباره امتحان کنید.")
        if response.status_code not in ok:
            raise _error(response)
        try:
            return response.json()
        except ValueError as exc:
            log.warning("Caspian Server sent a non-JSON answer: %s", response.text[:300])
            raise ServerUnreachable() from exc

    async def request_code(self, mobile: str, purpose: str) -> CodeSent:
        mobile = normalize_mobile(mobile) or mobile
        body = await self._call("POST", "/auth/otp/request", json={"mobile": mobile, "purpose": purpose})
        return CodeSent(int(body.get("expires_in", 120)), int(body.get("resend_after", 90)))

    async def verify_code(self, mobile: str, code: str, purpose: str) -> str:
        code = to_ascii_digits(re.sub(r"\s", "", code))
        body = await self._call("POST", "/auth/otp/verify", json={
            "mobile": normalize_mobile(mobile) or mobile, "code": code, "purpose": purpose})
        return body["verified_token"]

    async def register(self, verified_token: str, first_name: str, last_name: str, password: str) -> None:
        await self._call("POST", "/auth/otp/register", json={
            "verified_token": verified_token, "first_name": first_name, "last_name": last_name,
            "password": password})

    async def reset_password(self, verified_token: str, new_password: str) -> None:
        await self._call("POST", "/auth/otp/reset-password",
                         json={"verified_token": verified_token, "new_password": new_password})

    async def login(self, username: str, password: str) -> LoginAnswer:
        body = await self._call("POST", "/auth/login", data={"username": username, "password": password})
        return LoginAnswer(body["access_token"], body.get("role") or "", body.get("full_name") or "",
                           bool(body.get("must_change_password")))

    async def me(self, token: str) -> dict:
        return await self._call("GET", "/auth/me", token=token)

    async def change_password(self, token: str, current: str, new: str) -> str:
        """Returns the new token (the server revokes every older one)."""
        body = await self._call("POST", "/auth/change-password", token=token,
                                json={"current_password": current, "new_password": new})
        return body["access_token"]
