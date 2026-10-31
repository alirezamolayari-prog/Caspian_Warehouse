"""Test report issues 1 + 2: an open session must not outlive a deactivation or a role downgrade.
Every protected change re-checks the user's active status and role in the database."""

import datetime as dt
from decimal import Decimal

import pytest

import approval_helpers as slips
from caspian.db.models import DocStatus, DocType, PersonKind
from caspian.services import auth, documents, items, master, protected, stocktake, users
from caspian.services.errors import ServiceError
from caspian.services.items import ItemInput
from caspian.services.protected import ProtectedAction
from conftest import ADMIN_PIN

PW = "Str0ngPass1"


@pytest.fixture
async def shop(db, admin):
    unit = (await master.list_units(db))[0].id
    wh = (await master.list_warehouses(db))[0].id
    lamp = await items.create_item(db, admin, ItemInput("2001", "لامپ", unit))
    await slips.create_and_post(db, admin, documents.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [documents.LineInput(lamp, unit, Decimal(50))]))
    person = await master.save_person(db, admin, "حسین کریمی", PersonKind.EMPLOYEE)
    await users.create_user(db, admin, "keeper1", "انباردار", PW, "storekeeper")
    keeper = (await auth.login(db, "keeper1", PW)).actor
    return {"unit": unit, "wh": wh, "lamp": lamp, "person": person, "keeper": keeper}


def _issue(shop, qty=5):
    return documents.DocumentInput(DocType.ISSUE, dt.date.today(), shop["wh"],
                                   [documents.LineInput(shop["lamp"], shop["unit"], Decimal(qty))],
                                   person_id=shop["person"])


async def _stock(db, item_id) -> Decimal:
    return sum((q for _w, q in await documents.stock_by_warehouse(db, item_id)), Decimal(0))


async def test_deactivated_session_is_blocked_at_once(db, admin, shop):
    keeper = shop["keeper"]
    draft = await documents.create_document(db, keeper, _issue(shop))
    await users.set_active(db, admin, keeper.user_id, False)  # from another PC
    actions = {
        "post": lambda: slips.post_document(db, keeper, draft),
        "create": lambda: documents.create_document(db, keeper, _issue(shop)),
        "edit": lambda: documents.update_document(db, keeper, draft, 1, _issue(shop, 2)),
        "delete": lambda: documents.delete_draft(db, keeper, draft),
        "item": lambda: items.create_item(db, keeper, ItemInput("2002", "سیم", shop["unit"])),
        "stocktake": lambda: stocktake.create_stocktake(db, keeper, shop["wh"]),
    }
    for name, action in actions.items():
        with pytest.raises(ServiceError) as exc:
            await action()
        assert "غیرفعال" in exc.value.message, name
    assert await _stock(db, shop["lamp"]) == 50
    assert (await documents.get_document(db, admin, draft)).status == DocStatus.DRAFT


async def test_downgraded_role_loses_its_rights_at_once(db, admin, shop):
    keeper = shop["keeper"]
    approval = await protected.approve(db, admin, ProtectedAction.CHANGE_ROLE, "admin", ADMIN_PIN)
    await users.change_role(db, admin, keeper.user_id, "viewer", approval)
    with pytest.raises(ServiceError) as exc:
        await slips.create_and_post(db, keeper, _issue(shop))
    assert "دوباره وارد شوید" in exc.value.message
    assert await _stock(db, shop["lamp"]) == 50
    viewer = (await auth.login(db, "keeper1", PW)).actor  # signing in again gives the new role
    with pytest.raises(ServiceError):
        await documents.create_document(db, viewer, _issue(shop))


async def test_upgraded_role_keeps_working(db, admin, shop):
    keeper = shop["keeper"]
    approval = await protected.approve(db, admin, ProtectedAction.CHANGE_ROLE, "admin", ADMIN_PIN)
    await users.change_role(db, admin, keeper.user_id, "manager", approval)
    with pytest.raises(ServiceError):  # a different role is a different session: sign in again
        await slips.create_and_post(db, keeper, _issue(shop))
    manager = (await auth.login(db, "keeper1", PW)).actor
    await slips.create_and_post(db, manager, _issue(shop))
    assert await _stock(db, shop["lamp"]) == 45


async def test_active_user_and_the_assistant_still_work(db, admin, shop):
    keeper = shop["keeper"]
    await slips.create_and_post(db, keeper, _issue(shop, 1))
    draft = await documents.create_document(db, keeper.as_ai(), _issue(shop, 1))
    assert (await documents.get_document(db, admin, draft)).status == DocStatus.DRAFT
    await users.set_active(db, admin, keeper.user_id, False)
    with pytest.raises(ServiceError):  # the assistant acts as the same (now inactive) person
        await documents.create_document(db, keeper.as_ai(), _issue(shop, 1))


async def test_validate_and_listeners(db, admin, shop):
    from caspian.services import access

    keeper = shop["keeper"]
    assert await access.validate(db, keeper) is None
    heard = []
    listener = heard.append
    access.add_listener(listener)
    try:
        await users.set_active(db, admin, keeper.user_id, False)
        assert isinstance(await access.validate(db, keeper), access.AccountDisabled)
        with pytest.raises(access.AccountDisabled):
            await documents.create_document(db, keeper, _issue(shop))
        assert len(heard) == 1 and "غیرفعال" in heard[0].message
    finally:
        access.remove_listener(listener)
