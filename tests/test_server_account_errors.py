"""Phase 1.1 / 1.4: a refused *account* (401/403) never fails a request; refused requests can be sent
again by hand; the connection test makes an authorised call and names the exact reason."""

import pytest
from sqlalchemy import func, select, update

from caspian.db.models import GoodsRequest, RequestStatus, ServerOutbox
from caspian.services.server_link import connection, goods
from caspian.services.server_link.errors import ServerAccountProblem, ServerUnreachable, account_message
from caspian.services.server_link.sync import SyncState, unfail_account_rows
from request_helpers import SERVER_URL, request_input


async def _status(db, request_id) -> RequestStatus:
    async with db.session() as s:
        return (await s.get(GoodsRequest, request_id)).status


async def _failed(db) -> int:
    async with db.session() as s:
        return await s.scalar(select(func.count()).select_from(ServerOutbox).where(ServerOutbox.failed))


async def test_password_change_required_keeps_the_request_queued_then_sends_it(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    server.users["warehouse"]["must_change"] = True
    request_id = await goods.create_request(db, admin, request_input(linked))
    result = await engine.tick(admin)
    assert result.state == SyncState.ACCOUNT_PROBLEM
    assert result.message == account_message("password_change_required")
    assert await _failed(db) == 0 and await _status(db, request_id) == RequestStatus.QUEUED
    [row] = await goods.list_requests(db, admin)
    assert not row.failed and not row.last_error

    server.users["warehouse"]["must_change"] = False  # the password was changed on the server
    assert (await engine.tick(admin)).state == SyncState.CONNECTED
    assert await _status(db, request_id) == RequestStatus.PENDING and len(server.requests) == 1


@pytest.mark.parametrize("code", ["account_pending", "account_rejected"])
async def test_unapproved_account_is_named_and_nothing_fails(linked, admin, code):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    server.users["warehouse"]["approval"] = code.removeprefix("account_")
    await goods.create_request(db, admin, request_input(linked))
    result = await engine.tick(admin)
    assert result.state == SyncState.ACCOUNT_PROBLEM and result.message == account_message(code)
    assert await _failed(db) == 0
    server.users["warehouse"]["approval"] = "approved"
    engine.reset_rejection()  # e.g. the status button or a saved connection
    assert (await engine.tick(admin)).state == SyncState.CONNECTED and len(server.requests) == 1


async def test_login_rate_limit_says_how_long_to_wait(linked, admin):
    server, engine = linked["server"], linked["engine"]
    server.login_retry_after = 300
    result = await engine.tick(admin)
    assert result.state == SyncState.ACCOUNT_PROBLEM and "۵ دقیقه" in result.message
    logins = server.calls.count(("POST", "/auth/login"))
    await engine.tick(admin)
    assert server.calls.count(("POST", "/auth/login")) == logins  # waits instead of hammering


async def test_refused_request_is_retried_by_hand_without_a_duplicate(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id = await goods.create_request(db, admin, request_input(linked))
    server.refuse_next = [(422, "")]
    assert (await engine.tick(admin)).state == SyncState.CONNECTED
    [row] = await goods.list_requests(db, admin)
    assert row.failed and row.last_error.startswith("ارسال نشد:") and not server.requests
    await engine.tick(admin)
    assert not server.requests  # a refused row is not resent by itself

    assert await goods.retry_failed(db, admin, request_id)
    [row] = await goods.list_requests(db, admin)
    assert not row.failed and not row.last_error
    await engine.tick(admin)
    await engine.tick(admin)
    assert len(server.requests) == 1 and await _status(db, request_id) == RequestStatus.PENDING
    async with db.session() as s:
        cid = (await s.get(GoodsRequest, request_id)).client_request_id
    assert server.requests[1]["client_request_id"] == cid
    assert not await goods.retry_failed(db, admin, request_id)  # nothing left to retry


async def test_retry_after_a_lost_answer_never_duplicates(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id = await goods.create_request(db, admin, request_input(linked))
    server.drop_next = 1  # saved on the server, the answer lost
    await engine.tick(admin)
    await engine.tick(admin)
    assert len(server.requests) == 1 and await _status(db, request_id) == RequestStatus.PENDING


async def test_rows_failed_by_an_older_version_for_a_403_are_repaired_at_startup(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id = await goods.create_request(db, admin, request_input(linked))
    async with db.session() as s:  # what 1.2.0rc1 left behind for the stuck request
        await s.execute(update(ServerOutbox).values(
            failed=True, last_error="حساب انبار در سرور اجازه این کار را ندارد."))
        await s.execute(update(GoodsRequest).values(
            last_error="ارسال نشد: حساب انبار در سرور اجازه این کار را ندارد."))
    assert (await engine.tick(admin)).state == SyncState.CONNECTED  # first tick repairs, then sends
    assert await _status(db, request_id) == RequestStatus.PENDING and len(server.requests) == 1
    assert await unfail_account_rows(db) == 0


async def test_a_422_is_not_repaired_as_an_account_problem(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    await goods.create_request(db, admin, request_input(linked))
    server.refuse_next = [(422, "")]
    await engine.tick(admin)
    assert await unfail_account_rows(db) == 0 and await _failed(db) == 1


# ----- «آزمایش اتصال» -----


async def test_connection_test_success_names_the_account(fake_server):
    text = await connection.test_connection(SERVER_URL, "warehouse", "warehouse123", fake_server.transport())
    assert "انبار پاساژ" in text and "برقرار" in text


@pytest.mark.parametrize(("setup", "code"), [
    (lambda s: None, "invalid_credentials"),
    (lambda s: s.users["warehouse"].update(approval="pending"), "account_pending"),
    (lambda s: s.users["warehouse"].update(approval="rejected"), "account_rejected"),
    (lambda s: s.deactivate("warehouse"), "account_disabled"),
    (lambda s: s.users["warehouse"].update(must_change=True), "password_change_required"),
])
async def test_connection_test_reports_the_exact_account_problem(fake_server, setup, code):
    setup(fake_server)
    password = "wrong" if code == "invalid_credentials" else "warehouse123"
    with pytest.raises(ServerAccountProblem) as exc:
        await connection.test_connection(SERVER_URL, "warehouse", password, fake_server.transport())
    assert exc.value.code == code and exc.value.message == account_message(code)


async def test_connection_test_rate_limited_and_unreachable(fake_server):
    fake_server.login_retry_after = 90
    with pytest.raises(ServerAccountProblem) as exc:
        await connection.test_connection(SERVER_URL, "warehouse", "x", fake_server.transport())
    assert "۲ دقیقه" in exc.value.message
    fake_server.online = False
    with pytest.raises(ServerUnreachable):
        await connection.test_connection(SERVER_URL, "warehouse", "x", fake_server.transport())


def test_account_messages_are_distinct_and_persian():
    codes = ["invalid_credentials", "account_pending", "account_rejected", "account_disabled",
             "password_change_required", "login_rate_limited", "forbidden"]
    texts = [account_message(c, 60) for c in codes]
    assert len(set(texts)) == len(texts)
    assert all(not any("a" <= ch.lower() <= "z" for ch in t) for t in texts)


async def test_retry_button_is_enabled_only_for_a_refused_request(qtbot, themes, linked, admin):
    from caspian.core.settings import Settings
    from caspian.db.database import DbConfig
    from caspian.services.server_link.sync import SyncRunner
    from caspian.ui.app_context import AppContext
    from caspian.ui.requests_page import RequestsPage
    from caspian.ui.server_status import ServerLink

    db, server, engine = linked["db"], linked["server"], linked["engine"]
    ok = await goods.create_request(db, admin, request_input(linked, qty=2))
    await engine.tick(admin)
    bad = await goods.create_request(db, admin, request_input(linked, qty=3))
    server.refuse_next = [(422, "")]
    await engine.tick(admin)
    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    ctx.server_link = ServerLink(SyncRunner(engine))
    page = RequestsPage(ctx)
    qtbot.addWidget(page)
    page.show()
    await page.refresh()
    page.table.select_id(ok)
    assert not page.retry_button.isEnabled()
    page.table.select_id(bad)
    assert page.retry_button.isEnabled()
    page.on_retry()
    assert await _wait(lambda: not page.retry_button.isEnabled())
    await engine.tick(admin)
    assert await _status(db, bad) == RequestStatus.PENDING and len(server.requests) == 2


async def _wait(condition) -> bool:
    from helpers import wait_until

    return await wait_until(condition)
