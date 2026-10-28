"""Phase 2: personal Caspian Server accounts — SMS registration, password login, forgotten password,
must-change password, offline login, local login for admins only, and every queued row sent under
its own author's account (never someone else's)."""

import pytest
from sqlalchemy import select, update

from caspian.core.permissions import Perm
from caspian.core.secrets import get_secret
from caspian.db.models import Role, User
from caspian.services import auth, users
from caspian.services.actor import Actor
from caspian.services.errors import AuthenticationError
from caspian.services.server_link import connection, goods, session
from caspian.services.server_link.accounts import REGISTER, RESET, AuthApi, AuthError
from caspian.services.server_link.errors import ServerUnreachable
from caspian.services.server_link.session import MustChange, PersonLogin
from caspian.services.server_link.sync import WAITING_FOR, SyncEngine, SyncState
from request_helpers import SERVER_URL, request_input

MOBILE = "09121234567"


async def _sign_in(db, server, login=MOBILE, password="anbar-1403", remember=False):
    return await session.sign_in(db, login, password, remember, server.transport())


async def _make_manager(db, actor: Actor) -> Actor:
    """The local admin gives the new person a real role (on «کاربران»)."""
    async with db.session() as s:
        role = await s.scalar(select(Role).where(Role.code == "manager"))
        await s.execute(update(User).where(User.id == actor.user_id).values(role_id=role.id))
        user = await s.get(User, actor.user_id)
        await s.refresh(user, ["role"])
        return Actor.from_user(user)


# ----- the server's flows -----


async def test_register_with_sms_code_then_wait_for_approval(fake_server):
    api = AuthApi(SERVER_URL, fake_server.transport())
    sent = await api.request_code("۰۹۱۲ ۱۲۳-۴۵۶۷", REGISTER)
    assert (sent.expires_in, sent.resend_after) == (120, 90) and fake_server.sms == [(MOBILE, REGISTER)]
    with pytest.raises(AuthError) as exc:
        await api.verify_code(MOBILE, "000000", REGISTER)
    assert exc.value.code == "code_wrong" and "۴ بار" in exc.value.message and exc.value.field == "code"
    token = await api.verify_code(MOBILE, "482913", REGISTER)
    await api.register(token, "مؤمنی", "اسماعیل‌زاده", "anbar-1403")
    with pytest.raises(AuthError) as exc:
        await api.login(MOBILE, "anbar-1403")
    assert exc.value.code == "account_pending" and "تأیید" in exc.value.message
    with pytest.raises(AuthError) as exc:
        await api.request_code(MOBILE, REGISTER)
    assert exc.value.code == "mobile_taken"


async def test_server_name_refusal_shows_its_persian_text(fake_server):
    api = AuthApi(SERVER_URL, fake_server.transport())
    await api.request_code(MOBILE, REGISTER)
    token = await api.verify_code(MOBILE, "482913", REGISTER)
    with pytest.raises(AuthError) as exc:
        await api.register(token, "Ali", "رضایی", "anbar-1403")
    assert exc.value.field == "first_name" and exc.value.message.startswith("نام و نام خانوادگی")
    assert "Value error" not in exc.value.message


async def test_sms_limits_say_how_long_to_wait(fake_server):
    fake_server.otp_refuse = ("otp_cooldown", 75)
    with pytest.raises(AuthError) as exc:
        await AuthApi(SERVER_URL, fake_server.transport()).request_code(MOBILE, REGISTER)
    assert exc.value.code == "otp_cooldown" and "۲ دقیقه" in exc.value.message


async def test_forgotten_password(db, fake_server):
    fake_server.add_person(MOBILE, "old-pass-99", "رضا انباردار")
    api = AuthApi(SERVER_URL, fake_server.transport())
    await api.request_code(MOBILE, RESET)
    token = await api.verify_code(MOBILE, "482913", RESET)
    await api.reset_password(token, "new-pass-77")
    assert isinstance(await _sign_in(db, fake_server, password="new-pass-77"), PersonLogin)
    with pytest.raises(AuthError) as exc:
        await _sign_in(db, fake_server, password="old-pass-99")
    assert exc.value.code == "invalid_credentials"


# ----- signing in on this PC -----


async def test_first_sign_in_makes_a_local_user_with_minimal_rights(db, fake_server, memory_keyring):
    fake_server.add_person(MOBILE, "anbar-1403", "رضا انباردار")
    result = await _sign_in(db, fake_server, login="+98 912 123 4567")
    assert isinstance(result, PersonLogin) and not result.offline
    assert result.actor.display_name == "رضا انباردار" and result.actor.role_code == "new_staff"
    assert not result.actor.can(Perm.REQUESTS_MANAGE)
    async with db.session() as s:
        user = await s.get(User, result.actor.user_id)
        assert user.mobile == MOBILE and user.server_user_id is not None
        assert "anbar-1403" not in user.password_hash  # only the argon2 hash
    assert get_secret(session.TOKEN_KIND, session.token_name(user.server_user_id, session.DEFAULT_URL))
    assert get_secret(session.PASSWORD_KIND, f"{MOBILE}@api.caspianmoblserver.ir") is None
    again = await _sign_in(db, fake_server, remember=True)
    assert again.actor.user_id == result.actor.user_id  # the same local user, not a second one
    assert get_secret(session.PASSWORD_KIND, f"{MOBILE}@api.caspianmoblserver.ir") == "anbar-1403"


async def test_must_change_password_without_ssh(db, fake_server):
    name = fake_server.add_person(MOBILE, "temp-pass-11", "رضا انباردار")
    fake_server.users[name]["must_change"] = True
    pending = await _sign_in(db, fake_server, password="temp-pass-11")
    assert isinstance(pending, MustChange)
    result = await session.change_and_sign_in(db, pending, "temp-pass-11", "mine-pass-55",
                                              transport=fake_server.transport())
    assert isinstance(result, PersonLogin)
    account = fake_server.users[name]
    assert account["password"] == "mine-pass-55" and not account["must_change"]


async def test_offline_sign_in_with_the_last_good_password(db, fake_server):
    fake_server.add_person(MOBILE, "anbar-1403", "رضا انباردار")
    first = await _sign_in(db, fake_server)
    fake_server.online = False
    offline = await _sign_in(db, fake_server)
    assert offline.offline and offline.actor.user_id == first.actor.user_id
    with pytest.raises(AuthenticationError):
        await _sign_in(db, fake_server, password="wrong-pass")
    with pytest.raises(ServerUnreachable) as exc:
        await _sign_in(db, fake_server, login="09350000000")
    assert exc.value.message == session.OFFLINE_UNKNOWN


@pytest.mark.parametrize(("approval", "code"), [("pending", "account_pending"),
                                                ("rejected", "account_rejected")])
async def test_unapproved_accounts_are_told_why(db, fake_server, approval, code):
    fake_server.add_person(MOBILE, "anbar-1403", "رضا انباردار", approval=approval)
    with pytest.raises(AuthError) as exc:
        await _sign_in(db, fake_server)
    assert exc.value.code == code


async def test_local_username_login_is_for_admins_once_a_server_is_set(db, admin, fake_server):
    await users.create_user(db, admin, "ali", "علی", "Local-pass-1", "storekeeper")
    async with db.session() as s:
        await s.execute(update(User).where(User.username == "ali").values(must_change_password=False))
    assert isinstance(await _sign_in(db, fake_server, "ali", "Local-pass-1"), PersonLogin)  # no server yet
    await connection.save_connection(db, admin, SERVER_URL, "warehouse", "warehouse123")
    with pytest.raises(AuthenticationError) as exc:
        await _sign_in(db, fake_server, "ali", "Local-pass-1")
    assert exc.value.message == session.USE_MOBILE
    from conftest import ADMIN_PW

    assert (await _sign_in(db, fake_server, "admin", ADMIN_PW)).actor.is_admin  # emergency fallback


# ----- whose account sends what -----


async def test_queued_request_goes_under_its_authors_account(linked, admin, fake_server):
    db = linked["db"]
    fake_server.add_person(MOBILE, "anbar-1403", "رضا انباردار")
    fake_server.add_person("09350000000", "other-pass-2", "مریم کریمی")
    reza = await _make_manager(db, (await _sign_in(db, fake_server)).actor)
    request_id = await goods.create_request(db, reza, request_input(linked))
    maryam = await _make_manager(db, (await _sign_in(db, fake_server, "09350000000", "other-pass-2")).actor)
    engine = SyncEngine(db, fake_server.transport())
    assert (await engine.tick(maryam)).state == SyncState.CONNECTED  # Maryam is the one logged in
    [sent] = fake_server.requests.values()
    assert sent["created_by_name"] == "رضا انباردار"
    [row] = [r for r in await goods.list_requests(db, maryam) if r.id == request_id]
    assert row.status.value == "PENDING"


async def test_expired_token_waits_for_its_author_never_another_account(linked, admin, fake_server):
    db = linked["db"]
    fake_server.add_person(MOBILE, "anbar-1403", "رضا انباردار")
    reza = await _make_manager(db, (await _sign_in(db, fake_server)).actor)
    request_id = await goods.create_request(db, reza, request_input(linked))
    fake_server.expire_tokens()  # e.g. Reza changed his password on his phone
    engine = SyncEngine(db, fake_server.transport())
    await engine.tick(admin)  # the admin (shared account) is logged in
    assert not fake_server.requests  # not sent under the shared account's name
    [row] = [r for r in await goods.list_requests(db, admin) if r.id == request_id]
    assert row.last_error == WAITING_FOR.format(name="رضا انباردار") and not row.failed

    await _sign_in(db, fake_server)  # Reza signs in again
    engine.reset_rejection()
    await engine.tick(admin)
    assert [r["created_by_name"] for r in fake_server.requests.values()] == ["رضا انباردار"]


async def test_rows_of_local_users_go_through_the_shared_account(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    await goods.create_request(db, admin, request_input(linked))
    await engine.tick(admin)
    [sent] = server.requests.values()
    assert sent["created_by_name"] == "انبار پاساژ"
    [row] = await goods.list_requests(db, admin)
    async with db.session() as s:
        from caspian.db.models import GoodsRequest

        assert (await s.get(GoodsRequest, row.id)).created_by_name == "انبار پاساژ"


async def test_change_password_from_the_user_menu(db, fake_server):
    name = fake_server.add_person(MOBILE, "anbar-1403", "رضا انباردار")
    reza = (await _sign_in(db, fake_server)).actor
    await session.change_password(db, reza, "anbar-1403", "fresh-pass-88", fake_server.transport())
    assert fake_server.users[name]["password"] == "fresh-pass-88"
    fake_server.online = False
    assert (await _sign_in(db, fake_server, password="fresh-pass-88")).offline  # the hash follows


async def test_a_deactivated_local_user_cannot_sign_in(db, admin, fake_server):
    fake_server.add_person(MOBILE, "anbar-1403", "رضا انباردار")
    reza = (await _sign_in(db, fake_server)).actor
    async with db.session() as s:
        await s.execute(update(User).where(User.id == reza.user_id).values(is_active=False))
    with pytest.raises(AuthenticationError) as exc:
        await _sign_in(db, fake_server)
    assert exc.value.message == session.LOCAL_DISABLED
    assert auth  # the same lockout rules as local login are used offline
