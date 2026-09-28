import datetime as dt
import json

import httpx
import pytest

from caspian.core import jalali
from caspian.core.cron import Cron, CronError, describe
from caspian.db.models import TaskKind, TaskStatus
from caspian.services import messaging, scheduler
from caspian.services.errors import PermissionDenied
from caspian.services.messaging import EmailConfig, MessagingConfig, Messenger, TelegramConfig
from caspian.services.scheduler import TaskProposal, parse_instructions

SAT = dt.datetime(2026, 9, 26, 10, 0)  # a Saturday


# ----- cron -----


def test_cron_next_runs():
    assert Cron("0 20 * * *").next_after(SAT) == dt.datetime(2026, 9, 26, 20, 0)
    after_eight = dt.datetime(2026, 9, 26, 20, 0)
    assert Cron("0 20 * * *").next_after(after_eight) == dt.datetime(2026, 9, 27, 20, 0)
    assert Cron("30 9 * * 6").next_after(SAT) == dt.datetime(2026, 10, 3, 9, 30)  # next Saturday
    late = dt.datetime(2026, 9, 26, 9, 50)
    assert Cron("*/15 8-9 * * *").next_after(late) == dt.datetime(2026, 9, 27, 8, 0)
    assert Cron("0 0 1 * *").next_after(SAT) == dt.datetime(2026, 10, 1, 0, 0)
    assert Cron("0 12 * * 0,7").next_after(SAT).isoweekday() == 7  # Sunday either way


@pytest.mark.parametrize("bad", ["", "* * * *", "61 * * * *", "0 25 * * *", "a b c d e", "0 0 31 2 *"])
def test_cron_errors(bad):
    with pytest.raises(CronError):
        Cron(bad).next_after(SAT)


def test_describe():
    assert describe("0 20 * * *") == "هر روز ساعت 20:00"
    assert describe("30 9 * * 6") == "هر هفته شنبه ساعت 09:30"


# ----- instruction parsing -----


def test_parse_persian_instructions():
    text = """# وظایف انبار
- هر روز ساعت ۸ شب پشتیبان بگیر.
- هر شنبه ساعت ۹ صبح گزارش موجودی را به تلگرام بفرست
- هر هفته گزارش کالاهای نیازمند سفارش را ایمیل کن
- take a backup daily at 8 PM
- چای بیاورید"""
    tasks = parse_instructions(text)
    assert [(t.kind, t.cron, t.params) for t in tasks] == [
        (TaskKind.BACKUP, "0 20 * * *", {}),
        (TaskKind.REPORT, "0 9 * * 6", {"report": "stock_balance", "channels": ["telegram"]}),
        (TaskKind.REPORT, "0 8 * * 6", {"report": "reorder", "channels": ["email"]}),
        (TaskKind.BACKUP, "0 20 * * *", {}),
    ]


async def test_ai_parsing_with_offline_fallback(db, admin):
    class FakeGateway:
        def __init__(self, content):
            self.content = content

        async def available(self):
            return True

        async def chat(self, messages, **kw):
            assert kw.get("json_mode")
            return type("R", (), {"content": self.content})()

    good = json.dumps({"tasks": [{"name": "بکاپ شبانه", "kind": "BACKUP", "cron": "0 20 * * *"}]})
    proposals, used_ai = await scheduler.propose_from_text("x", FakeGateway(good))
    assert used_ai and proposals[0].name == "بکاپ شبانه"
    proposals, used_ai = await scheduler.propose_from_text("هر روز ساعت ۲۱ پشتیبان بگیر",
                                                           FakeGateway("not json"))
    assert not used_ai and proposals[0].cron == "0 21 * * *"


# ----- lifecycle -----


async def test_proposal_requires_human_approval(db, admin):
    [task_id] = await scheduler.add_proposals(db, admin.as_ai(), [
        TaskProposal("پشتیبان", TaskKind.BACKUP, "0 20 * * *")])
    [row] = await scheduler.list_tasks(db, admin)
    assert row.status is TaskStatus.PROPOSED and row.next_run_at is None
    with pytest.raises(PermissionDenied):
        await scheduler.approve(db, admin.as_ai(), task_id)
    await scheduler.approve(db, admin, task_id, now=SAT)
    [row] = await scheduler.list_tasks(db, admin)
    assert row.status is TaskStatus.ACTIVE and row.runs_here
    assert row.next_run_at == dt.datetime(2026, 9, 26, 20, 0)
    await scheduler.update_task(db, admin, task_id, "پشتیبان", "0 21 * * *", {})
    assert (await scheduler.list_tasks(db, admin))[0].status is TaskStatus.PROPOSED  # re-approve


@pytest.fixture
def secrets(monkeypatch):
    store = {("msg", "telegram_token"): "123:ABC", ("msg", "smtp_password"): "pw"}
    monkeypatch.setattr(messaging, "get_secret", lambda k, n: store.get((k, n)))
    monkeypatch.setattr(messaging, "set_secret", lambda k, n, v: store.__setitem__((k, n), v))
    return store


async def test_report_task_runs_and_sends_to_configured_chats(db, admin, secrets):
    await messaging.save_config(db, admin, MessagingConfig(TelegramConfig(True, ["111", " 222 "])))
    sent = []

    def handler(request: httpx.Request):
        assert request.url.path == "/bot123:ABC/sendDocument"
        sent.append(request.content)
        return httpx.Response(200, json={"ok": True})

    messenger = Messenger(db, httpx.MockTransport(handler))
    [task_id] = await scheduler.add_proposals(db, admin, [TaskProposal(
        "گزارش موجودی", TaskKind.REPORT, "0 9 * * *", {"report": "stock_balance", "channels": ["telegram"]})])
    await scheduler.approve(db, admin, task_id, now=SAT)
    assert await scheduler.run_due(db, messenger, now=SAT) == 0  # not yet 09:00 tomorrow
    ran = await scheduler.run_due(db, messenger, now=dt.datetime(2026, 9, 27, 9, 0))
    assert ran == 1 and len(sent) == 2
    stamp = jalali.format_date(dt.date.today(), persian_digits=False).replace("/", "-")
    assert f"stock_balance-{stamp}.xlsx".encode() in sent[0]  # file named after today (Jalali)
    [row] = await scheduler.list_tasks(db, admin)
    assert row.last_ok and row.next_run_at == dt.datetime(2026, 9, 28, 9, 0)


async def test_backup_task_without_handler_is_reported_not_crashing(db, admin, secrets):
    [task_id] = await scheduler.add_proposals(db, admin, [TaskProposal("b", TaskKind.BACKUP, "0 1 * * *")])
    await scheduler.approve(db, admin, task_id, now=SAT)
    saved = scheduler.HANDLERS.pop(TaskKind.BACKUP, None)
    try:
        ok, result = await scheduler.run_task(db, task_id, Messenger(db))
    finally:
        if saved:
            scheduler.HANDLERS[TaskKind.BACKUP] = saved
    assert not ok and "قابل اجرا نیست" in result


async def test_email_delivery(db, admin, secrets):
    config = MessagingConfig(email=EmailConfig(True, "smtp.example.com", 587, "starttls", "anbar@example.com",
                                               "anbar@example.com", ["boss@example.com"]))
    await messaging.save_config(db, admin, config)
    delivered = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, **kw):
            delivered["server"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            delivered["tls"] = True

        def login(self, user, password):
            delivered["login"] = (user, password)

        def send_message(self, message):
            delivered["to"] = message["To"]
            delivered["attachments"] = [p.get_filename() for p in message.iter_attachments()]

    messenger = Messenger(db, smtp_factory=FakeSMTP)
    result = await scheduler.send_report(db, messenger, "loans", ["email"])
    assert "email" in result
    assert delivered["tls"] and delivered["login"] == ("anbar@example.com", "pw")
    assert delivered["to"] == "boss@example.com" and delivered["attachments"][0].startswith("loans-")


async def test_messaging_config_validation(db, admin, secrets):
    from caspian.services.errors import ValidationError

    with pytest.raises(ValidationError):
        await messaging.save_config(db, admin, MessagingConfig(email=EmailConfig(recipients=["nope"])))
    with pytest.raises(ValidationError):
        await messaging.save_config(db, admin.as_ai(), MessagingConfig())
    with pytest.raises(messaging.SendError, match="پیکربندی"):
        await Messenger(db).send("x")
