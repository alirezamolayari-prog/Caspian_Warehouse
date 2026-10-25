"""Caspian Server client and connection settings: login, silent re-login, refused accounts,
unreachable servers; the password lives only in the credential store."""

import httpx
import pytest
from sqlalchemy import select

from caspian.db.models import AppSetting
from caspian.services import auth, users
from caspian.services.errors import PermissionDenied, ValidationError
from caspian.services.server_link import connection
from caspian.services.server_link.client import ServerClient
from caspian.services.server_link.errors import (
    ServerAccountRejected,
    ServerRejected,
    ServerUnreachable,
)
from fake_caspian_server import FakeServer
from request_helpers import SERVER_URL


def _client(server: FakeServer, password: str = "warehouse123") -> ServerClient:
    return ServerClient(SERVER_URL, "warehouse", password, server.transport())


async def test_logs_in_once_and_reuses_the_token():
    server = FakeServer()
    client = _client(server)
    assert (await client.call("GET", "/auth/me"))["role"] == "warehouse"
    await client.call("GET", "/changes")
    assert server.calls.count(("POST", "/auth/login")) == 1


async def test_expired_token_logs_in_again_silently():
    server = FakeServer()
    client = _client(server)
    await client.call("GET", "/auth/me")
    server.expire_tokens()
    assert (await client.call("GET", "/auth/me"))["role"] == "warehouse"
    assert server.calls.count(("POST", "/auth/login")) == 2


async def test_deactivated_account_is_reported_clearly():
    server = FakeServer()
    client = _client(server)
    await client.call("GET", "/auth/me")
    server.deactivate("warehouse")  # the token stops working at once, and so does the login
    with pytest.raises(ServerAccountRejected) as exc:
        await client.call("GET", "/changes")
    assert "غیرفعال" in exc.value.message
    with pytest.raises(ServerAccountRejected):
        await _client(FakeServer(), "wrong").call("GET", "/auth/me")


async def test_unreachable_and_server_errors():
    server = FakeServer()
    server.online = False
    with pytest.raises(ServerUnreachable):
        await _client(server).call("GET", "/changes")

    def broken(request):
        return httpx.Response(502, text="<html>bad gateway</html>")

    with pytest.raises(ServerUnreachable):
        await ServerClient(SERVER_URL, "warehouse", "x", httpx.MockTransport(broken)).call("GET", "/x")


async def test_refusal_carries_a_persian_message():
    server = FakeServer()
    with pytest.raises(ServerRejected) as exc:
        await _client(server).call("GET", "/requests/99")
    assert exc.value.status == 404 and exc.value.message == "این درخواست در سرور پیدا نشد."


async def test_connection_saved_by_admin_password_only_in_credential_store(db, admin, memory_keyring):
    saved = await connection.save_connection(db, admin, " https://caspian.test/ ", "warehouse",
                                             "warehouse123")
    assert saved.url == "https://caspian.test"
    assert await connection.get_connection(db) == saved
    assert connection.get_password(saved) == "warehouse123"
    async with db.session() as s:
        stored = (await s.scalars(select(AppSetting).where(AppSetting.key == connection.KEY))).one()
    assert "warehouse123" not in str(stored.value)
    assert any(v == "warehouse123" for v in memory_keyring.store.values())
    # Keeping the password: None leaves it as it is.
    await connection.save_connection(db, admin, "https://caspian.test", "warehouse", None)
    assert connection.get_password(saved) == "warehouse123"


async def test_connection_rules(db, admin):
    with pytest.raises(ValidationError):
        await connection.save_connection(db, admin, "caspian.test", "warehouse", "pw")
    with pytest.raises(ValidationError):  # no password stored yet for this account
        await connection.save_connection(db, admin, "https://caspian.test", "warehouse", None)
    with pytest.raises(PermissionDenied):
        await connection.save_connection(db, admin.as_ai(), "https://caspian.test", "warehouse", "pw")
    await users.create_user(db, admin, "manager1", "مدیر پاساژ", "Str0ngPass1", "manager")
    manager = (await auth.login(db, "manager1", "Str0ngPass1")).actor
    with pytest.raises(PermissionDenied):
        await connection.save_connection(db, manager, "https://caspian.test", "warehouse", "pw")
    assert connection.ServerConnection("http://192.168.1.5:8000", "w").insecure
    assert not connection.ServerConnection("http://localhost:8000", "w").insecure


async def test_test_connection_checks_the_role():
    server = FakeServer()
    text = await connection.test_connection(SERVER_URL, "warehouse", "warehouse123", server.transport())
    assert "انبار پاساژ" in text
    with pytest.raises(ValidationError):
        await connection.test_connection(SERVER_URL, "owner1", "owner123", server.transport())
