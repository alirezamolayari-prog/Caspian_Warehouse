"""HTTP client for Caspian Server (the `warehouse` role account).

The token is kept in memory only. A 401 means the token expired *or* the account was deactivated
(the server answers both the same way), so the client logs in again once, silently; if that login
is refused too, the account itself is the problem (`ServerAccountProblem`). A 403 is always about the
account (`X-Error-Code`: pending, rejected, disabled, password_change_required …).
"""

import logging
from typing import Any

import httpx

from caspian.services.server_link.errors import (
    ServerAccountProblem,
    ServerRejected,
    ServerUnreachable,
)

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0


class ServerClient:
    def __init__(self, base_url: str, username: str, password: str,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = password
        self._transport = transport
        self._timeout = timeout
        self._token: str | None = None
        self.must_change_password = False

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=self._timeout,
                                 transport=self._transport)

    async def _send(self, http: httpx.AsyncClient, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = await http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:  # timeouts, refused connections, DNS, broken answers
            log.info("Caspian Server unreachable (%s %s): %s", method, path, exc)
            raise ServerUnreachable() from exc
        if response.status_code >= 500:
            log.warning("Caspian Server error %s on %s %s: %s", response.status_code, method, path,
                        response.text[:300])
            raise ServerUnreachable()
        return response

    async def _login(self, http: httpx.AsyncClient) -> None:
        self._token = None
        response = await self._send(http, "POST", "/auth/login",
                                    data={"username": self.username, "password": self._password})
        if response.status_code in (401, 403, 429):
            log.warning("Caspian Server refused the login of %r: %s %s %s", self.username,
                        response.status_code, error_code(response), _detail(response))
            raise _account_problem(response, "invalid_credentials")
        if response.status_code != 200:
            raise ServerRejected(response.status_code, _detail(response), error_code(response))
        body = _json(response)
        self._token = body["access_token"]
        self.must_change_password = bool(body.get("must_change_password"))

    async def call(self, method: str, path: str, *, json: Any = None, params: dict | None = None,
                   ok: tuple[int, ...] = (200, 201)) -> Any:
        """Send one authenticated request; returns the decoded JSON body."""
        async with self._http() as http:
            if self._token is None:
                await self._login(http)
            response = await self._send(http, method, path, json=json, params=params,
                                        headers={"Authorization": f"Bearer {self._token}"})
            if response.status_code == 401:  # expired token or deactivated account: log in again once
                await self._login(http)
                response = await self._send(http, method, path, json=json, params=params,
                                            headers={"Authorization": f"Bearer {self._token}"})
        if response.status_code in (401, 403):
            log.warning("Caspian Server refused the account on %s %s: %s %s %s", method, path,
                        response.status_code, error_code(response), _detail(response))
            raise _account_problem(response, "invalid_credentials" if response.status_code == 401
                                   else "forbidden")
        if response.status_code not in ok:
            detail = _detail(response)
            log.warning("Caspian Server refused %s %s (%s, %s): %s", method, path, response.status_code,
                        error_code(response), detail)
            raise ServerRejected(response.status_code, detail, error_code(response))
        return _json(response)

    async def health(self) -> dict:
        async with self._http() as http:
            response = await self._send(http, "GET", "/health")
        if response.status_code != 200:
            raise ServerRejected(response.status_code, _detail(response))
        return _json(response)


def error_code(response: httpx.Response) -> str:
    return response.headers.get("x-error-code", "").strip()


def retry_after(response: httpx.Response) -> int | None:
    try:
        return int(float(response.headers.get("retry-after", "")))
    except ValueError:
        return None


def _account_problem(response: httpx.Response, default: str) -> ServerAccountProblem:
    return ServerAccountProblem(error_code(response) or default, retry_after(response),
                                response.status_code)


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:  # e.g. an HTML error page of a proxy in between
        log.warning("Caspian Server sent a non-JSON answer: %s", response.text[:300])
        raise ServerUnreachable() from exc


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", "")
    except (ValueError, AttributeError):
        detail = response.text
    return str(detail)[:300]
