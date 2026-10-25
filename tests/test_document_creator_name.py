"""Bug list round 1 #5: the documents list shows who registered a document by their full name (the
login name only when no full name was given)."""

import datetime as dt
from decimal import Decimal

from caspian.db.models import DocType
from caspian.services import auth, documents, items, master, users
from caspian.services.items import ItemInput


async def test_list_shows_the_full_name(db, admin):
    unit = (await master.list_units(db))[0].id
    wh = (await master.list_warehouses(db))[0].id
    lamp = await items.create_item(db, admin, ItemInput("2001", "لامپ", unit))
    await users.create_user(db, admin, "keeper1", "رضا انباردار", "Str0ngPass1", "storekeeper")
    keeper = (await auth.login(db, "keeper1", "Str0ngPass1")).actor
    await documents.create_document(db, keeper, documents.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [documents.LineInput(lamp, unit, Decimal(1))]))
    [row] = await documents.list_documents(db, admin)
    assert row.created_by == "رضا انباردار"
