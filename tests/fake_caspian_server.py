"""In-memory Caspian Server for tests (an httpx.MockTransport handler).

Follows the real server's rules (E:/My App/Caspian-Server, README «فهرست APIها»): form login with a
bearer token, 201/200 on create by `client_request_id`, PUT only from rejected, «issued» only from
approved (same number again = 200), owners shown to the warehouse as «مالک», `/changes` with a 2 s
overlap. Switches: `online`, `drop_next` (the server commits but the answer is lost), deactivate,
`users[name]["must_change"]` / `["approval"]` ("pending" / "rejected"), `refuse_next`,
`login_retry_after`. Errors carry `X-Error-Code` (and `Retry-After` on 429) like the real server.
"""

import datetime as dt
import itertools
import json
from urllib.parse import parse_qs

import httpx

OVERLAP = dt.timedelta(seconds=2)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


class FakeServer:
    def __init__(self) -> None:
        self.users = {"warehouse": {"id": 2, "password": "warehouse123", "role": "warehouse",
                                    "full_name": "انبار پاساژ", "active": True},
                      "owner1": {"id": 3, "password": "owner123", "role": "owner",
                                 "full_name": "مالک ۱", "active": True}}
        self.tokens: dict[str, str] = {}
        self.requests: dict[int, dict] = {}
        self.messages: list[dict] = []
        self.online = True
        self.drop_next = 0  # commit, then fail the connection (the client never sees the answer)
        self.refuse_next: list[tuple[int, str]] = []
        self.codes: dict[tuple[str, str], str] = {}  # (mobile, purpose) -> the SMS code
        self.verified: dict[str, tuple[str, str]] = {}
        self.sms: list[tuple[str, str]] = []
        self.otp_refuse: tuple[str, int] | None = None  # (X-Error-Code, Retry-After) for code requests
        self.logins: list[str] = []
        self.ai_requests: list[dict] = []
        self.ai_used = 0
        self.ai_refuse: tuple[int, str] | None = None  # (status, X-Error-Code) for /ai/chat
        self.ai_tool: tuple[str, dict] | None = None  # the tool the fake AI calls first
        self.ai_answer = "پاسخ آزمایشی دستیار"
        self._user_ids = itertools.count(10)  # (status, X-Error-Code) for the next calls
        self.login_retry_after: int | None = None  # set: every login answers 429 login_rate_limited
        self.calls: list[tuple[str, str]] = []
        self._ids = itertools.count(1)
        self._msg_ids = itertools.count(1)
        self._tokens = itertools.count(1)
        self._clock = dt.datetime(2026, 10, 22, 8, 0, tzinfo=dt.UTC)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def now(self) -> dt.datetime:
        self._clock += dt.timedelta(seconds=3)  # every change is later than the last overlap
        return self._clock

    # ----- what owners do on their phones -----

    def approve(self, request_id: int) -> None:
        req = self.requests[request_id]
        assert req["status"] == "pending"
        req.update(status="approved", decided_at=self.now(), updated_at=self._clock)

    def reject(self, request_id: int, reason: str) -> None:
        req = self.requests[request_id]
        assert req["status"] == "pending"
        req.update(status="rejected", rejection_reason=reason, decided_at=self.now(),
                   updated_at=self._clock)

    def owner_message(self, request_id: int, text: str) -> None:
        self.messages.append({"id": next(self._msg_ids), "request_id": request_id, "sender_id": 3,
                              "sender_name": "مالک ۱", "sender_role": "owner", "text": text,
                              "created_at": self.now()})

    def deactivate(self, username: str) -> None:
        self.users[username]["active"] = False

    def expire_tokens(self) -> None:
        self.tokens.clear()

    # ----- HTTP -----

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if not self.online:
            raise httpx.ConnectError("offline", request=request)
        response = self._route(request, method, path)
        if self.drop_next and method != "GET" and response.status_code < 300 and path != "/auth/login":
            self.drop_next -= 1
            raise httpx.ReadTimeout("answer lost", request=request)
        return response

    def _json(self, status: int, body) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(body, ensure_ascii=False).encode(),
                              headers={"content-type": "application/json"})

    def _error(self, status: int, detail: str, code: str = "",
               retry_after: int | None = None) -> httpx.Response:
        response = self._json(status, {"detail": detail})
        if code:
            response.headers["X-Error-Code"] = code
        if retry_after is not None:
            response.headers["Retry-After"] = str(retry_after)
        return response

    def _login(self, request: httpx.Request) -> httpx.Response:
        """Like the real server: rate limit first, then the password, and only after a correct
        password the account's state (pending / rejected / disabled)."""
        if self.login_retry_after is not None:
            return self._error(429, "تلاش‌های ناموفق زیاد", "login_rate_limited", self.login_retry_after)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        name = self._find(form.get("username", ""))
        user = self.users.get(name) if name else None
        if user is None or user["password"] != form.get("password"):
            return self._error(401, "نام کاربری یا رمز عبور اشتباه است", "invalid_credentials")
        if not user["active"]:
            return self._error(403, "حساب غیرفعال است", "account_disabled")
        state = user.get("approval", "approved")
        if state != "approved":
            return self._error(403, "حساب تأیید نشده", f"account_{state}")
        token = f"t{next(self._tokens)}"
        self.tokens[token] = name
        self.logins.append(name)
        return self._json(200, {"access_token": token, "token_type": "bearer", "role": user["role"],
                                "full_name": user["full_name"],
                                "must_change_password": user.get("must_change", False)})

    def _find(self, login: str) -> str | None:
        """A username, or a mobile number in any accepted form (like the real server)."""
        if login in self.users:
            return login
        digits = "".join(ch for ch in login if ch.isdigit())[-10:]
        return next((n for n, u in self.users.items() if u.get("mobile", "")[-10:] == digits and digits),
                    None)

    def add_person(self, mobile: str, password: str, full_name: str, approval: str = "approved",
                   role: str = "warehouse") -> str:
        """A personal account (what SMS registration + the admin's approval make)."""
        name = f"m{mobile}"
        self.users[name] = {"id": next(self._user_ids), "password": password, "role": role,
                            "full_name": full_name, "active": True, "mobile": mobile, "approval": approval}
        return name

    def _otp(self, path: str, body: dict) -> httpx.Response:
        mobile = "0" + "".join(ch for ch in body.get("mobile", "") if ch.isdigit())[-10:]
        if path == "/auth/otp/request":
            if len(mobile) != 11 or not mobile.startswith("09"):
                return self._error(422, "شماره موبایل نامعتبر است", "invalid_mobile")
            taken = self._find(mobile) is not None
            if body["purpose"] == "register" and taken:
                return self._error(409, "این شماره قبلاً ثبت شده", "mobile_taken")
            if body["purpose"] == "reset_password" and not taken:
                return self._error(404, "حسابی نیست", "account_not_found")
            if self.otp_refuse:
                code, wait = self.otp_refuse
                return self._error(429, "سقف", code, wait)
            self.codes[(mobile, body["purpose"])] = "482913"
            self.sms.append((mobile, body["purpose"]))
            return self._json(200, {"expires_in": 120, "resend_after": 90})
        if path == "/auth/otp/verify":
            if self.codes.get((mobile, body["purpose"])) != body["code"]:
                return self._error(400, "کد اشتباه است؛ ۴ بار دیگر می‌توانید امتحان کنید", "code_wrong")
            del self.codes[(mobile, body["purpose"])]
            token = f"v{next(self._tokens)}"
            self.verified[token] = (mobile, body["purpose"])
            return self._json(200, {"verified_token": token, "expires_in": 300})
        mobile, purpose = self.verified.pop(body.get("verified_token"), (None, None))
        if path == "/auth/otp/register" and purpose == "register":
            if any(ch.isascii() and ch.isalpha() for ch in body["first_name"] + body["last_name"]):
                return self._json(422, {"detail": [{"loc": ["body", "first_name"], "type": "value_error",
                                                    "msg": "Value error, نام و نام خانوادگی را با حروف "
                                                           "فارسی بنویسید (۲ تا ۵۰ حرف)"}]})
            self.add_person(mobile, body["password"], f"{body['first_name']} {body['last_name']}",
                            approval="pending")
            return self._json(201, {"status": "pending"})
        if path == "/auth/otp/reset-password" and purpose == "reset_password":
            name = self._find(mobile)
            self.users[name].update(password=body["new_password"], must_change=False)
            for token in [t for t, n in self.tokens.items() if n == name]:
                del self.tokens[token]
            return self._json(200, {"status": "ok"})
        return self._error(401, "مرحلهٔ تأیید نامعتبر است", "verification_invalid")

    def _user(self, request: httpx.Request) -> dict | None:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        username = self.tokens.get(token)
        user = self.users.get(username) if username else None
        return user if user and user["active"] else None

    def _out(self, req: dict) -> dict:
        return {**{k: v for k, v in req.items() if k not in ("decided_at", "issued_at", "created_at",
                                                              "updated_at")},
                "decided_at": _iso(req["decided_at"]), "issued_at": _iso(req["issued_at"]),
                "created_at": _iso(req["created_at"]), "updated_at": _iso(req["updated_at"]),
                "decided_by_me": False}

    def _msg_out(self, m: dict) -> dict:
        owner = m["sender_role"] == "owner"
        return {**m, "sender_id": None if owner else m["sender_id"],
                "sender_name": "مالک" if owner else m["sender_name"], "created_at": _iso(m["created_at"])}

    def _route(self, request: httpx.Request, method: str, path: str) -> httpx.Response:
        if path == "/health":
            return self._json(200, {"status": "ok", "server_time": self._clock.isoformat()})
        if path == "/auth/login":
            return self._login(request)
        if path.startswith("/auth/otp/"):
            return self._otp(path, json.loads(request.content))
        user = self._user(request)
        if user is None:
            return self._error(401, "توکن نامعتبر است یا منقضی شده — دوباره وارد شوید")
        parts = path.strip("/").split("/")
        body = json.loads(request.content) if request.content else None
        if path == "/auth/change-password" and method == "POST":
            if body.get("current_password") != user["password"]:
                return self._error(400, "رمز فعلی اشتباه است", "invalid_current_password")
            user.update(password=body["new_password"], must_change=False)
            for token in [t for t, name in self.tokens.items() if self.users.get(name) is user]:
                del self.tokens[token]
            token = f"t{next(self._tokens)}"
            self.tokens[token] = next(name for name, u in self.users.items() if u is user)
            return self._json(200, {"access_token": token, "token_type": "bearer"})
        if user.get("must_change"):
            return self._error(403, "ابتدا رمز را عوض کنید", "password_change_required")
        if self.refuse_next:
            status, code = self.refuse_next.pop(0)
            return self._error(status, "رد شد", code)
        if path == "/ai/chat":
            return self._ai_chat(body, user)
        if path == "/ai/limits/me":
            return self._json(200, {"used_today": self.ai_used, "daily_limit": 20000, "unlimited": False,
                                    "remaining": 20000 - self.ai_used, "resets_in_seconds": 3600})
        if path == "/auth/me":
            return self._json(200, {"id": user["id"], "username": "warehouse", "full_name": user["full_name"],
                                    "role": user["role"], "mobile": None, "approval_status": "approved",
                                    "must_change_password": False})
        if path == "/changes":
            since = request.url.params.get("since")
            since_dt = dt.datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
            server_time = self._clock
            reqs = [self._out(r) for r in sorted(self.requests.values(), key=lambda r: r["updated_at"])
                    if since_dt is None or r["updated_at"] > since_dt]
            msgs = [self._msg_out(m) for m in self.messages if since_dt is None or m["created_at"] > since_dt]
            return self._json(200, {"server_time": _iso(server_time),
                                    "next_since": _iso(server_time - OVERLAP),
                                    "requests": reqs, "messages": msgs})
        if parts == ["requests"] and method == "POST":
            return self._create(body, user)
        if len(parts) >= 2 and parts[0] == "requests":
            req = self.requests.get(int(parts[1]))
            if req is None:
                return self._error(404, "درخواست پیدا نشد")
            if len(parts) == 2 and method == "GET":
                return self._json(200, self._out(req))
            if len(parts) == 2 and method == "PUT":
                if req["status"] != "rejected":
                    return self._error(409, f"این کار فقط برای درخواست با وضعیت «rejected» ممکن است "
                                            f"(وضعیت فعلی: {req['status']})")
                req.update(self._fields(body), status="pending", rejection_reason=None, decided_at=None,
                           updated_at=self.now())
                return self._json(200, self._out(req))
            if parts[2:] == ["issued"]:
                number = body["issue_number"]
                if req["status"] == "issued" and req["issue_number"] == number:
                    return self._json(200, self._out(req))
                if req["status"] != "approved":
                    return self._error(409, "این کار فقط برای درخواست با وضعیت «approved» ممکن است")
                req.update(status="issued", issue_number=number, issued_at=self.now(),
                           updated_at=self._clock)
                return self._json(200, self._out(req))
            if parts[2:] == ["messages"]:
                if method == "GET":
                    return self._json(200, [self._msg_out(m) for m in self.messages
                                            if m["request_id"] == req["id"]])
                message = {"id": next(self._msg_ids), "request_id": req["id"], "sender_id": user["id"],
                           "sender_name": user["full_name"], "sender_role": user["role"],
                           "text": body["text"], "created_at": self.now()}
                self.messages.append(message)
                return self._json(201, self._msg_out(message))
        return self._error(404, "Not Found")

    def _ai_chat(self, body: dict, user: dict) -> httpx.Response:
        """Like the server's dev mode: a call to `ai_tool` (if the app sent tools), then text."""
        self.ai_requests.append(body)
        if self.ai_refuse:
            status, code = self.ai_refuse
            return self._error(status, "رد شد", code, 3600 if status == 429 else None)
        if body.get("app") != "warehouse" or body.get("capability") != "assistant":
            return self._error(400, "نامعتبر", "ai_unknown_capability")
        self.ai_used += 100
        last = body["messages"][-1]
        names = [t["function"]["name"] for t in body.get("tools") or []]
        if last["role"] != "tool" and self.ai_tool and self.ai_tool[0] in names:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{len(self.ai_requests)}", "type": "function",
                "function": {"name": self.ai_tool[0], "arguments": json.dumps(self.ai_tool[1],
                                                                              ensure_ascii=False)}}]}
            finish = "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": self.ai_answer, "tool_calls": None}, "stop"
        return self._json(200, {"message": message, "finish_reason": finish, "model": "dev", "mode": "dev",
                                "usage": {"total_tokens": 100}, "used_today": self.ai_used,
                                "limit_today": 20000})

    def _fields(self, body: dict) -> dict:
        return {"requester_name": body["requester_name"], "requester_phone": body.get("requester_phone"),
                "consumption_location": body["consumption_location"], "note": body.get("note"),
                "items": [{"id": n, **item} for n, item in enumerate(body["items"], start=1)]}

    def _create(self, body: dict, user: dict) -> httpx.Response:
        assert body["items"] and body["consumption_location"] and body["requester_name"]
        for item in body["items"]:
            assert item["quantity"] > 0 and len(item["unit"]) <= 50
        cid = body.get("client_request_id")
        for req in self.requests.values():
            if cid and req["client_request_id"] == cid:
                return self._json(200, self._out(req))
        now = self.now()
        req = {"id": next(self._ids), "client_request_id": cid, **self._fields(body), "status": "pending",
               "rejection_reason": None, "created_by_id": user["id"],
               "created_by_name": user["full_name"], "decided_at": None, "issue_number": None,
               "issued_at": None, "created_at": now, "updated_at": now}
        self.requests[req["id"]] = req
        return self._json(201, self._out(req))
