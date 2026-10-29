"""QA round 1, Phase 1: the assistant works like a user but never destructively."""

import datetime as dt
import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, update

from caspian.db.models import (
    AuditLog,
    DocStatus,
    DocType,
    ImportKind,
    ImportSource,
    Item,
    LineStatus,
    PersonKind,
)
from caspian.services import backup, documents, imports, items, master, stocktake, users
from caspian.services.ai import config
from caspian.services.ai.assistant import Assistant
from caspian.services.ai.gateway import AIUnavailable, Gateway
from caspian.services.ai.tools import ToolContext, available_tools, run_tool
from caspian.services.errors import PermissionDenied, ValidationError
from caspian.services.import_files import RawRow
from caspian.services.items import ItemInput
from test_assistant import call


@pytest.fixture
async def shop(db, admin, monkeypatch):
    monkeypatch.setattr(config, "get_secret", lambda k, n: None)
    u = {x.name: x.id for x in await master.list_units(db)}
    wh = (await master.list_warehouses(db))[0].id
    jaro1 = await items.create_item(db, admin, ItemInput("1006", "جارو", u["عدد"]))
    # Two active «جارو» exist in older data (new duplicates are refused since round 2, feature A).
    jaro2 = await items.create_item(db, admin, ItemInput("1017", "سطل موقت", u["عدد"]))
    async with db.session() as s:
        await s.execute(update(Item).where(Item.id == jaro2).values(name="جارو", name_normalized="جارو"))
    drill = await items.create_item(db, admin, ItemInput("1001", "دریل بوش", u["عدد"]))
    await documents.create_and_post(db, admin, documents.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh,
        [documents.LineInput(i, u["عدد"], Decimal(10)) for i in (jaro1, jaro2, drill)]))
    person = await master.save_person(db, admin, "علی مولایاری", PersonKind.EMPLOYEE)
    await master.save_person(db, admin, "رضا کریمی", PersonKind.EMPLOYEE)
    await config.save_provider(db, admin, "Local", config.ProviderKind.OLLAMA,
                               "http://localhost:11434/v1", "q")
    return {"u": u, "wh": wh, "jaro1": jaro1, "jaro2": jaro2, "drill": drill, "person": person}


def _ctx(db, admin):
    return ToolContext(db, admin.as_ai(), [])


async def _tool(ctx, name, **args):
    return json.loads(await run_tool(ctx, name, args))


def _line(shop, qty=1):
    return documents.LineInput(shop["drill"], shop["u"]["عدد"], Decimal(qty))


# ----- #2 service-level guards -----


async def test_services_refuse_destructive_actions_for_the_ai(db, admin, shop, tmp_path):
    ai = admin.as_ai()
    draft = await documents.create_document(db, ai, documents.DocumentInput(
        DocType.RECEIPT, dt.date.today(), shop["wh"], [_line(shop)]))
    posted = await documents.create_and_post(db, admin, documents.DocumentInput(
        DocType.RECEIPT, dt.date.today(), shop["wh"], [_line(shop)]))
    batch = await imports.create_batch(db, ai, ImportKind.ITEMS, ImportSource.TEXT, [RawRow(name="x")])
    sid = await stocktake.create_stocktake(db, admin, shop["wh"])
    me = admin.user_id
    attempts = [
        documents.delete_draft(db, ai, draft),
        documents.cancel_document(db, ai, posted, "x"),
        items.set_item_active(db, ai, shop["drill"], False, None),
        items.delete_item(db, ai, shop["drill"], None),
        users.create_user(db, ai, "bad", "", "Bad#Pass2026", "viewer"),
        users.rename_user(db, ai, me, "boss"),
        users.set_active(db, ai, me, False),
        users.reset_password(db, ai, me, "Temp#2026x"),
        users.change_role(db, ai, me, "viewer", None),
        master.set_person_active(db, ai, shop["person"], False),
        master.set_warehouse_active(db, ai, shop["wh"], False),
        master.set_unit_active(db, ai, shop["u"]["متر"], False),
        imports.discard_batch(db, ai, batch),
        stocktake.cancel(db, ai, sid),
        stocktake.approve(db, ai, sid),
        backup.restore_backup(db, ai, None, tmp_path / "x.bak", None, None, tmp_path),
        documents.post_document(db, ai, draft),  # stock changes need a person (QA Part A #1)
    ]
    for attempt in attempts:
        with pytest.raises(PermissionDenied, match="دستیار کاسپین"):
            await attempt
    assert (await documents.get_document(db, admin, posted)).status is DocStatus.POSTED
    assert (await documents.get_document(db, admin, draft)).status is DocStatus.DRAFT


# ----- #1 person; the assistant only prepares drafts, the person confirms (QA Part A #1) -----


async def _stock(db, item_id) -> Decimal:
    return sum((q for _w, q in await documents.stock_by_warehouse(db, item_id)), Decimal(0))


async def test_issue_for_a_named_person_is_prepared_as_a_draft(db, admin, shop):
    ctx = _ctx(db, admin)
    before = await _stock(db, shop["drill"])
    result = await _tool(ctx, "create_document", doc_type="ISSUE", person="آقای مولایاری",
                         lines=[{"code": "1001", "qty": 1}], post=True)  # a stray post=true is ignored
    assert result["status"] == "DRAFT" and result["needs_confirmation"] and result["number"].startswith("ح-")
    assert "علی مولایاری" in result["summary"] and "دریل بوش" in result["summary"]
    detail = await documents.get_document(db, admin, result["document_id"])
    assert detail.status is DocStatus.DRAFT and detail.input.person_id == shop["person"]
    assert await _stock(db, shop["drill"]) == before  # nothing changed yet

    # «بله، ثبت کن» on the card: the person posts it, recorded as on the assistant's suggestion.
    await documents.post_document(db, admin, result["document_id"], via="assistant")
    assert await _stock(db, shop["drill"]) == before - 1
    async with db.session() as s:
        created = await s.scalar(select(AuditLog).where(AuditLog.action == "document.created")
                                 .order_by(AuditLog.id.desc()))
        posted = await s.scalar(select(AuditLog).where(AuditLog.action == "document.posted")
                                .order_by(AuditLog.id.desc()))
    assert created.details["via_ai"] and created.details["on_behalf_of"] == admin.display_name
    assert posted.details["via"] == "assistant" and posted.user_id == admin.user_id


async def test_unknown_or_ambiguous_person_is_asked(db, admin, shop):
    await master.save_person(db, admin, "علی مولایی", PersonKind.EMPLOYEE)
    ctx = _ctx(db, admin)
    for name in ("علی مولا", "حسن نادری"):
        result = await _tool(ctx, "create_document", doc_type="ISSUE", person=name,
                             lines=[{"code": "1001", "qty": 1}])
        assert result["needs_choice"] == "person", name
    assert ctx.created_documents == []
    missing = await _tool(ctx, "create_document", doc_type="ISSUE", lines=[{"code": "1001", "qty": 1}])
    assert missing["needs_choice"] == "person"


async def test_the_assistant_can_never_post(db, admin, shop):
    from caspian.db.models import AppSetting

    async with db.session() as s:  # the former «اجازه ثبت نهایی سند توسط دستیار» = on: ignored now
        s.add(AppSetting(key="ai_allow_post", value=True))
    assert "post_document" not in {t.name for t in available_tools(admin.as_ai())}
    ctx = _ctx(db, admin)
    result = await _tool(ctx, "create_document", doc_type="RECEIPT",
                         lines=[{"code": "1001", "qty": 2}], post=True)
    assert result["status"] == "DRAFT" and "posted" not in result
    assert "error" in await _tool(ctx, "post_document", document=str(result["document_id"]))
    with pytest.raises(PermissionDenied, match="دستیار کاسپین"):  # enforced in the service
        await documents.post_document(db, admin.as_ai(), result["document_id"])
    await documents.post_document(db, admin, result["document_id"])  # people still can


async def test_post_errors_keep_the_draft(db, admin, shop):
    ctx = _ctx(db, admin)
    result = await _tool(ctx, "create_document", doc_type="ISSUE", person="علی مولایاری",
                         lines=[{"code": "1001", "qty": 99}])
    with pytest.raises(ValidationError, match="کافی نیست"):  # the person's «بله» fails
        await documents.post_document(db, admin, result["document_id"], via="assistant")
    assert (await documents.get_document(db, admin, result["document_id"])).status is DocStatus.DRAFT


async def test_ambiguous_items_and_missing_quantities_are_asked(db, admin, shop):
    u = shop["u"]["عدد"]
    await items.create_item(db, admin, ItemInput("2001", "شمع وارمر", u))
    await items.create_item(db, admin, ItemInput("2002", "شمع تولد", u))
    ctx = _ctx(db, admin)
    asked = await _tool(ctx, "create_document", doc_type="ISSUE", person="علی مولایاری",
                        lines=[{"name": "شمع", "qty": 1}])
    assert asked["needs_choice"] == "item" and {c["code"] for c in asked["candidates"]} >= {"2001", "2002"}
    for qty in (None, "", 0, "چند تا", -1):
        asked = await _tool(ctx, "create_document", doc_type="ISSUE", person="علی مولایاری",
                            lines=[{"code": "1001", **({} if qty is None else {"qty": qty})}])
        assert asked["needs_choice"] == "qty", qty
    assert ctx.created_documents == []
    made = await _tool(ctx, "create_document", doc_type="ISSUE", person="علی مولایاری",
                       lines=[{"name": "شمع تولد", "qty": "۲"}])  # exact name, Persian digits
    assert made["status"] == "DRAFT" and made["lines"][0]["qty"] == 2


# ----- #3 ambiguous items -----


async def test_two_items_with_the_same_name_are_asked(db, admin, shop):
    ctx = _ctx(db, admin)
    result = await _tool(ctx, "create_document", doc_type="ISSUE", person="علی مولایاری",
                         lines=[{"name": "جارو", "qty": 1}])
    assert result["needs_choice"] == "item"
    assert sorted(c["code"] for c in result["candidates"]) == ["1006", "1017"]
    assert all(c["stock"] == 10 for c in result["candidates"])
    batch = await imports.create_batch(db, admin, ImportKind.STOCK, ImportSource.TEXT,
                                       [RawRow(name="جارو", qty=Decimal(1))], "t", DocType.RECEIPT,
                                       shop["wh"])
    [line] = (await imports.get_batch(db, admin, batch)).lines
    assert line.status is LineStatus.CONFLICT and len(line.candidates) >= 2


# ----- #4 one create per turn; created things survive a failure -----


async def test_same_create_twice_in_one_turn_is_not_duplicated(db, admin, shop):
    ctx = _ctx(db, admin)
    args = {"doc_type": "ISSUE", "person": "علی مولایاری", "lines": [{"code": "1001", "qty": 1}]}
    first = await _tool(ctx, "create_document", **args)
    second = await _tool(ctx, "create_document", **args)
    assert second["duplicate"] and second["document_id"] == first["document_id"]
    draft_a = await _tool(ctx, "create_stock_draft", doc_type="ISSUE", person="علی مولایاری",
                          title="خروج کالا - تحویل به آقای مولایاری", lines=[{"code": "1001", "qty": 1}])
    draft_b = await _tool(ctx, "create_stock_draft", doc_type="ISSUE", person="علی مولایاری",
                          title="تحویل به آقای مولایاری", lines=[{"code": "1001", "qty": 1}])
    assert draft_b["duplicate"] and len(ctx.created_batches) == 1
    detail = await imports.get_batch(db, admin, draft_a["draft_id"])
    assert detail.person_id == shop["person"]  # the receiver is no longer lost (#1)


async def test_failure_after_side_effects_reports_what_was_done(db, admin, shop):
    responses = [{"content": None, "tool_calls": [call(
        "create_document", doc_type="ISSUE", person="علی مولایاری",
        lines=[{"code": "1001", "qty": 1}])]}]

    def handler(request):
        if responses:
            message = responses.pop(0)
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", **message}}]})
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    gw = Gateway(db, httpx.MockTransport(handler), online_check=lambda: True)
    reply = await Assistant(db, gw, admin).send("حواله بزن برای آقای مولایاری یک دریل")
    assert "ح-" in reply.text and "پیش‌نویس" in reply.text
    assert reply.created_documents and reply.created_documents[0]["status"] == "DRAFT"
    assert reply.created_documents[0]["needs_confirmation"]


# ----- #5 honest failure reasons, #8 total timeout -----


async def test_chat_failure_names_each_provider_and_reason(db, admin, shop):
    await config.save_provider(db, admin, "Groq", config.ProviderKind.CUSTOM, "http://localhost:9/v1", "m")

    def handler(request):
        if request.url.port == 11434:
            return httpx.Response(404, json={"error": {"message": "model q not found"}})
        return httpx.Response(429)

    gw = Gateway(db, httpx.MockTransport(handler), online_check=lambda: True)
    with pytest.raises(AIUnavailable) as caught:
        await Assistant(db, gw, admin).send("سلام")
    text = caught.value.message
    assert "Local" in text and "404" in text and "پیدا نشد" in text
    assert "not found" not in text  # raw server text only in the log (round 2 #3)
    assert "Groq" in text and "429" in text


async def test_gemini_503_shows_a_short_persian_reason(db, admin, shop, caplog):
    """Chat showed «[{'error': {'code': 503, 'message': 'This model is currently experiencing high
    demand…'}}]» (round 2 #3)."""
    body = [{"error": {"code": 503, "message": "This model is currently experiencing high demand. "
                                               "Spikes in demand are usually temporary.",
                       "status": "UNAVAILABLE"}}]
    gw = Gateway(db, httpx.MockTransport(lambda r: httpx.Response(503, json=body)),
                 online_check=lambda: True)
    with caplog.at_level("WARNING"), pytest.raises(AIUnavailable) as caught:
        await Assistant(db, gw, admin).send("سلام")
    text = caught.value.message
    assert "سرور سرویس شلوغ است (503)" in text
    assert "{" not in text and "error" not in text and "high demand" not in text
    assert "high demand" in caplog.text  # kept for support
    provider = (await config.list_providers(db))[0]
    ok, message, _ = await gw.test_provider(provider)  # settings test: short server message, isolated
    assert not ok and "(503)" in message and "⁦This model is currently" in message


async def test_whole_request_times_out(db, admin, shop):
    import asyncio

    class Slow:
        async def chat(self, *a, **k):
            await asyncio.sleep(5)

    with pytest.raises(AIUnavailable, match="طول کشید"):
        await Assistant(db, Slow(), admin).send("x", timeout=0.1)
