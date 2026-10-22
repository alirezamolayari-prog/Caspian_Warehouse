"""Helpers for the goods-request tests (fixtures `fake_server`, `request_shop` and `linked` are in
conftest.py)."""

import datetime as dt
from decimal import Decimal

from caspian.db.models import DocType, PersonKind
from caspian.services import documents, items, locations, master
from caspian.services.items import ItemInput
from caspian.services.server_link import goods

SERVER_URL = "https://caspian.test"


async def make_shop(db, admin) -> dict:
    """Two items in stock, a requester with a phone, and a fixed-floor place."""
    unit = (await master.list_units(db))[0].id
    wh = (await master.list_warehouses(db))[0].id
    bags = await items.create_item(db, admin, ItemInput("1024", "کیسه زباله", unit))
    soap = await items.create_item(db, admin, ItemInput("1030", "مایع دستشویی", unit))
    await documents.create_and_post(db, admin, documents.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [documents.LineInput(bags, unit, Decimal(50)),
                                               documents.LineInput(soap, unit, Decimal(10))]))
    person = await master.save_person(db, admin, "علی رضایی", PersonKind.EMPLOYEE, phone="0912 345 6789")
    index = await locations.load_index(db)
    plate = next(loc for loc in index.locations if loc.is_active and not loc.needs_floor
                 and loc.type == "plate")
    return {"db": db, "unit": unit, "warehouse": wh, "bags": bags, "soap": soap, "person": person,
            "location": plate}


def request_input(shop, qty: int = 20, **changes) -> goods.RequestInput:
    data = goods.RequestInput(shop["person"], shop["location"].id, None, "فوری",
                              [goods.RequestLineInput(shop["bags"], shop["unit"], Decimal(qty))])
    for key, value in changes.items():
        setattr(data, key, value)
    return data


async def stock(db, item_id) -> Decimal:
    return sum((q for _w, q in await documents.stock_by_warehouse(db, item_id)), Decimal(0))
