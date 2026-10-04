"""Scheduled tasks: backups and emailed/Telegram reports.

Instructions (typed or from a .md file) are parsed — by the AI when available, otherwise by
an offline Persian parser — into PROPOSED tasks. An admin must approve a task before it
runs; it then runs only on the PC it was approved on (so a LAN doesn't send duplicates).
"""

import asyncio
import contextlib
import datetime as dt
import json
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import select

from caspian.core import jalali
from caspian.core.cron import Cron, CronError, describe
from caspian.core.permissions import Perm
from caspian.core.text import normalize, to_ascii_digits
from caspian.db.database import Database
from caspian.db.models import ScheduledTask, TaskKind, TaskStatus
from caspian.services import audit, reports
from caspian.services.actor import Actor
from caspian.services.errors import NotFound, PermissionDenied, ServiceError, ValidationError
from caspian.services.excel_export import xlsx_bytes
from caspian.services.messaging import XLSX_MIME, Messenger

log = logging.getLogger(__name__)
MACHINE = socket.gethostname()[:128]

REPORT_NAMES = {
    "stock_balance": "گزارش موجودی کالا",
    "reorder": "کالاهای نیازمند سفارش",
    "loans": "امانی‌های باز",
    "activity": "فعالیت کاربران (۲۴ ساعت گذشته)",
}
KIND_NAMES = {TaskKind.BACKUP: "پشتیبان‌گیری", TaskKind.REPORT: "ارسال گزارش"}
STATUS_NAMES = {TaskStatus.PROPOSED: "در انتظار تأیید", TaskStatus.ACTIVE: "فعال",
                TaskStatus.PAUSED: "متوقف"}

# Scheduled work runs as this identity (reads everything reports need, can't change data).
SCHEDULER_ACTOR = Actor(None, "scheduler", "زمان‌بند", "system", frozenset(
    p.value for p in (Perm.REPORTS_VIEW, Perm.STOCK_VIEW, Perm.DOCUMENTS_VIEW, Perm.ITEMS_VIEW,
                      Perm.USERS_MANAGE, Perm.BACKUP_CREATE)))

# Task kind -> handler(db, params) -> result text. BACKUP is registered by the backup module.
HANDLERS: dict[TaskKind, Callable[[Database, dict], Awaitable[str]]] = {}


@dataclass
class TaskProposal:
    name: str
    kind: TaskKind
    cron: str
    params: dict = field(default_factory=dict)
    source_text: str = ""


@dataclass(frozen=True)
class TaskRow:
    id: int
    name: str
    kind: TaskKind
    cron: str
    schedule_text: str
    params: dict
    status: TaskStatus
    machine: str
    next_run_at: dt.datetime | None
    last_run_at: dt.datetime | None
    last_ok: bool | None
    last_result: str

    @property
    def runs_here(self) -> bool:
        return self.machine == MACHINE


# ----- offline instruction parser -----

_WEEKDAYS = [("پنجشنبه", 4), ("چهارشنبه", 3), ("سه‌شنبه", 2), ("سه شنبه", 2), ("دوشنبه", 1),
             ("یکشنبه", 0), ("جمعه", 5), ("شنبه", 6)]
_EN_WEEKDAYS = {"sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4,
                "friday": 5, "saturday": 6}


def _parse_time(text: str) -> tuple[int, int] | None:
    t = to_ascii_digits(text.lower())
    if m := re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t):
        hour, minute = int(m.group(1)) % 12, int(m.group(2) or 0)
        return (hour + 12 if m.group(3) == "pm" else hour), minute
    if m := re.search(r"ساعت\s*(\d{1,2})(?:[:٫.](\d{2}))?\s*(صبح|ظهر|بعدازظهر|بعد از ظهر|عصر|شب)?", t):
        hour, minute, part = int(m.group(1)), int(m.group(2) or 0), m.group(3) or ""
        if part in ("بعدازظهر", "بعد از ظهر", "عصر", "شب") and hour < 12:
            hour += 12
        if part == "شب" and hour == 24:
            hour = 0
        return hour % 24, minute
    if m := re.search(r"\b(\d{1,2}):(\d{2})\b", t):
        return int(m.group(1)) % 24, int(m.group(2))
    return None


def parse_instruction(line: str) -> TaskProposal | None:
    text = normalize(line)
    raw = line.strip()
    if not text:
        return None
    if any(k in text for k in ("پشتیبان", "بکاپ", "backup")):
        kind, params, name = TaskKind.BACKUP, {}, "پشتیبان‌گیری"
    elif any(k in text for k in ("گزارش", "report")):
        report = "stock_balance"
        if any(k in text for k in ("سفارش", "کمبود", "reorder")):
            report = "reorder"
        elif "امانی" in text or "loan" in text:
            report = "loans"
        elif "فعالیت" in text or "activity" in text:
            report = "activity"
        channels = [c for c, keys in (("telegram", ("تلگرام", "telegram")),
                                      ("email", ("ایمیل", "email", "رایانامه", "پست الکترونیک")))
                    if any(k in text for k in keys)]
        kind, params, name = TaskKind.REPORT, {"report": report, "channels": channels}, \
            f"ارسال {REPORT_NAMES[report]}"
    else:
        return None
    hour, minute = _parse_time(raw) or ((20, 0) if kind == TaskKind.BACKUP else (8, 0))
    dow, dom = "*", "*"
    days = []
    for word, value in _WEEKDAYS:
        if normalize(word) in text:
            days.append(value)
            text = text.replace(normalize(word), " ")
    days += [v for k, v in _EN_WEEKDAYS.items() if k in text]
    if days:
        dow = ",".join(str(d) for d in sorted(set(days)))
    elif any(k in text for k in ("هر ماه", "ماهانه", "monthly")):
        m = re.search(r"روز\s*(\d{1,2})", to_ascii_digits(text))
        dom = str(int(m.group(1))) if m else "1"
    elif any(k in text for k in ("هر هفته", "هفتگی", "weekly")):
        dow = "6"  # Saturday: the first day of the Persian week
    return TaskProposal(name, kind, f"{minute} {hour} {dom} * {dow}", params, raw)


def parse_instructions(text: str) -> list[TaskProposal]:
    proposals = []
    for line in re.split(r"[\n\r]+|(?<=[.!؟?])\s+", text):
        line = line.strip().lstrip("-*#0123456789.) ").strip()
        if (p := parse_instruction(line)) is not None:
            proposals.append(p)
    return proposals


AI_PROMPT = """دستورالعمل‌های زیر را به کارهای زمان‌بندی‌شده تبدیل کن. فقط JSON برگردان به شکل
{"tasks": [{"name": str, "kind": "BACKUP"|"REPORT", "cron": "دقیقه ساعت روزماه ماه روزهفته",
"report": "stock_balance"|"reorder"|"loans"|"activity" (فقط برای REPORT),
"channels": ["telegram","email"] (فقط برای REPORT)}]}
روز هفته در cron: ۰=یکشنبه، ۱=دوشنبه ... ۶=شنبه. «۸ شب» یعنی ساعت 20. کارهای دیگر را نادیده بگیر.
دستورالعمل‌ها:
"""


async def propose_from_text(text: str, gateway=None) -> tuple[list[TaskProposal], bool]:
    """(proposals, used_ai). Nothing is scheduled until an admin approves."""
    if gateway is not None and await gateway.available():
        try:
            result = await gateway.chat([{"role": "user", "content": AI_PROMPT + text}],
                                        json_mode=True, temperature=0)
            data = json.loads(result.content)
            proposals = []
            for t in data.get("tasks", []):
                kind = TaskKind(t["kind"])
                params = {}
                if kind == TaskKind.REPORT:
                    report = t.get("report", "stock_balance")
                    params = {"report": report if report in REPORT_NAMES else "stock_balance",
                              "channels": [c for c in t.get("channels", []) if c in ("telegram", "email")]}
                Cron(t["cron"])  # validate
                proposals.append(TaskProposal(str(t.get("name") or KIND_NAMES[kind])[:200], kind,
                                              t["cron"], params, text[:2000]))
            if proposals:
                return proposals, True
        except (ServiceError, ValueError, KeyError, TypeError, CronError):
            log.info("AI task parsing failed; using the offline parser", exc_info=True)
    return parse_instructions(text), False


# ----- CRUD -----


def _row(t: ScheduledTask) -> TaskRow:
    return TaskRow(t.id, t.name, t.kind, t.cron, describe(t.cron), t.params or {}, t.status,
                   t.machine, t.next_run_at, t.last_run_at, t.last_ok, t.last_result)


async def list_tasks(db: Database, actor: Actor) -> list[TaskRow]:
    actor.require(Perm.SETTINGS_EDIT)
    async with db.session() as s:
        return [_row(t) for t in (await s.scalars(select(ScheduledTask).order_by(ScheduledTask.id))).all()]


async def add_proposals(db: Database, actor: Actor, proposals: list[TaskProposal]) -> list[int]:
    """Store as PROPOSED. The AI may call this; approval is always human."""
    if not actor.is_ai:
        actor.require(Perm.SETTINGS_EDIT)
    ids = []
    async with db.session(actor.user_id) as s:
        for p in proposals:
            Cron(p.cron)
            if p.kind == TaskKind.REPORT and p.params.get("report") not in REPORT_NAMES:
                raise ValidationError("نوع گزارش نامعتبر است.")
            task = ScheduledTask(name=p.name, kind=p.kind, cron=p.cron, params=p.params,
                                 status=TaskStatus.PROPOSED, source_text=p.source_text[:4000])
            s.add(task)
            await s.flush()
            audit.record(s, actor, "task.proposed", "task", task.id, {"name": p.name, "cron": p.cron})
            ids.append(task.id)
    return ids


async def _load(s, task_id: int) -> ScheduledTask:
    task = await s.get(ScheduledTask, task_id)
    if task is None:
        raise NotFound("کار زمان‌بندی‌شده پیدا نشد.")
    return task


async def approve(db: Database, actor: Actor, task_id: int, now: dt.datetime | None = None) -> None:
    """Activate a task on THIS PC. Humans only."""
    if actor.is_ai:
        raise PermissionDenied("تأیید کارهای زمان‌بندی‌شده فقط با کاربر است.")
    actor.require(Perm.SETTINGS_EDIT)
    async with db.session(actor.user_id) as s:
        task = await _load(s, task_id)
        if task.kind == TaskKind.REPORT and not task.params.get("channels"):
            raise ValidationError("برای ارسال گزارش، کانال (تلگرام یا ایمیل) را مشخص کنید.")
        _activate(task, actor, now)
        audit.record(s, actor, "task.approved", "task", task.id, {"machine": MACHINE, "cron": task.cron})


def _activate(task: ScheduledTask, actor: Actor, now: dt.datetime | None) -> None:
    """Run `task` on THIS PC from its next scheduled time (local clock)."""
    task.status = TaskStatus.ACTIVE
    task.machine = MACHINE
    task.approved_by_id = actor.user_id
    task.approved_at = dt.datetime.now()
    task.next_run_at = Cron(task.cron).next_after(now or dt.datetime.now())


# ----- daily automatic backup (Settings → پشتیبان‌گیری) -----

DAILY_BACKUP_NAME = "پشتیبان‌گیری خودکار روزانه"
DAILY_BACKUP_LABEL = "خودکار"


async def _daily_task(s) -> ScheduledTask | None:
    """This PC's daily backup task: one per machine, marked with params["auto_daily"]."""
    rows = (await s.scalars(select(ScheduledTask).where(
        ScheduledTask.kind == TaskKind.BACKUP, ScheduledTask.machine == MACHINE)
        .order_by(ScheduledTask.id))).all()
    return next((t for t in rows if (t.params or {}).get("auto_daily")), None)


async def daily_backup(db: Database) -> tuple[bool, dt.time | None]:
    """(enabled, time) of this PC's daily backup."""
    async with db.session() as s:
        task = await _daily_task(s)
    if task is None:
        return False, None
    minute, hour = task.cron.split()[:2]
    return task.status == TaskStatus.ACTIVE, dt.time(int(hour), int(minute))


async def set_daily_backup(db: Database, actor: Actor, enabled: bool, at: dt.time,
                           now: dt.datetime | None = None) -> None:
    """Create/update (enabled) or remove (disabled) this PC's single daily backup task. It is
    approved for this PC right away (it is the admin's own setting) and shows in the tasks tab."""
    actor.require_human("تغییر پشتیبان‌گیری خودکار")
    actor.require(Perm.SETTINGS_EDIT)
    async with db.session(actor.user_id) as s:
        task = await _daily_task(s)
        if not enabled:
            if task is not None:
                audit.record(s, actor, "task.daily_backup", "task", task.id, {"enabled": False})
                await s.delete(task)
            return
        if task is None:
            task = ScheduledTask(name=DAILY_BACKUP_NAME, kind=TaskKind.BACKUP, cron="0 0 * * *",
                                 source_text=DAILY_BACKUP_NAME)
            s.add(task)
        task.cron = f"{at.minute} {at.hour} * * *"
        task.params = {"auto_daily": True, "label": DAILY_BACKUP_LABEL}
        _activate(task, actor, now)
        await s.flush()
        audit.record(s, actor, "task.daily_backup", "task", task.id,
                     {"enabled": True, "time": at.strftime("%H:%M"), "machine": MACHINE})


async def update_task(db: Database, actor: Actor, task_id: int, name: str, cron: str,
                      params: dict) -> None:
    actor.require(Perm.SETTINGS_EDIT)
    Cron(cron)
    async with db.session(actor.user_id) as s:
        task = await _load(s, task_id)
        task.name, task.cron, task.params = name.strip() or task.name, cron, params
        if task.status == TaskStatus.ACTIVE:
            # An edited task must be re-approved (e.g. new recipients or schedule).
            task.status = TaskStatus.PROPOSED
        audit.record(s, actor, "task.updated", "task", task.id, {"cron": cron})


async def set_paused(db: Database, actor: Actor, task_id: int, paused: bool) -> None:
    actor.require(Perm.SETTINGS_EDIT)
    async with db.session(actor.user_id) as s:
        task = await _load(s, task_id)
        if task.status == TaskStatus.PROPOSED:
            raise ValidationError("ابتدا کار را تأیید کنید.")
        task.status = TaskStatus.PAUSED if paused else TaskStatus.ACTIVE
        if not paused:
            task.next_run_at = Cron(task.cron).next_after(dt.datetime.now())
        audit.record(s, actor, "task.paused" if paused else "task.resumed", "task", task.id)


async def delete_task(db: Database, actor: Actor, task_id: int) -> None:
    actor.require(Perm.SETTINGS_EDIT)
    async with db.session(actor.user_id) as s:
        task = await _load(s, task_id)
        audit.record(s, actor, "task.deleted", "task", task.id, {"name": task.name})
        await s.delete(task)


# ----- execution -----


async def build_report(db: Database, name: str) -> reports.ReportTable:
    actor = SCHEDULER_ACTOR
    if name == "stock_balance":
        return await reports.stock_balance(db, actor)
    if name == "reorder":
        return reports.burn_rate_table(
            await reports.burn_rates(db, actor, only_needing_order=True), reports.DEFAULT_PARAMS)
    if name == "loans":
        return await reports.loans_report(db, actor)
    if name == "activity":
        today = dt.date.today()
        return await reports.user_activity(db, actor, date_from=today - dt.timedelta(days=1),
                                           date_to=today)
    raise ValidationError("نوع گزارش نامعتبر است.")


async def send_report(db: Database, messenger: Messenger, name: str,
                      channels: list[str] | None = None) -> str:
    table = await build_report(db, name)
    stamp = jalali.format_date(dt.date.today(), persian_digits=False).replace("/", "-")
    caption = f"{table.title} — {jalali.format_date(dt.date.today())}\n" + " | ".join(table.meta)
    used = await messenger.send(caption, (f"{name}-{stamp}.xlsx", xlsx_bytes(table), XLSX_MIME),
                                channels)
    return f"ارسال شد ({'، '.join(used)}) — {len(table.rows)} ردیف"


async def run_task(db: Database, task_id: int, messenger: Messenger,
                   now: dt.datetime | None = None) -> tuple[bool, str]:
    now = now or dt.datetime.now()
    async with db.session() as s:
        task = await _load(s, task_id)
        kind, params = task.kind, dict(task.params or {})
    try:
        if kind == TaskKind.REPORT:
            result = await send_report(db, messenger, params.get("report", "stock_balance"),
                                       params.get("channels") or None)
        else:
            handler = HANDLERS.get(kind)
            if handler is None:
                raise ValidationError("این نوع کار در این نسخه قابل اجرا نیست.")
            result = await handler(db, params)
        ok = True
    except ServiceError as exc:
        ok, result = False, exc.message
    except Exception as exc:  # a failing task must never take the app down
        log.exception("Scheduled task %s failed", task_id)
        ok, result = False, f"خطای غیرمنتظره: {type(exc).__name__}"
    async with db.session() as s:
        task = await _load(s, task_id)
        task.last_run_at, task.last_ok, task.last_result = now, ok, result[:2000]
        task.next_run_at = Cron(task.cron).next_after(now)
        audit.record(s, None, "task.ran", "task", task.id, {"ok": ok, "result": result[:200]})
    return ok, result


async def run_due(db: Database, messenger: Messenger, now: dt.datetime | None = None) -> int:
    """Run ACTIVE tasks assigned to this PC whose time has come. Returns how many ran."""
    now = now or dt.datetime.now()
    async with db.session() as s:
        due = (await s.scalars(select(ScheduledTask.id).where(
            ScheduledTask.status == TaskStatus.ACTIVE, ScheduledTask.machine == MACHINE,
            ScheduledTask.next_run_at <= now))).all()
    for task_id in due:
        await run_task(db, task_id, messenger, now)
    return len(due)


CATCH_UP_MIN_GAP = dt.timedelta(hours=1)


async def catch_up(db: Database, messenger: Messenger, now: dt.datetime | None = None) -> int:
    """Runs missed tasks (the app was closed at their time) once each, shortly after login.
    Skipped when the next regular run is close anyway. Running moves next_run_at past `now`, so a
    task never gets more than one catch-up. Returns how many ran."""
    now = now or dt.datetime.now()
    async with db.session() as s:
        missed = (await s.scalars(select(ScheduledTask).where(
            ScheduledTask.status == TaskStatus.ACTIVE, ScheduledTask.machine == MACHINE,
            ScheduledTask.next_run_at < now))).all()
        todo = []
        for task in missed:
            upcoming = Cron(task.cron).next_after(now)
            if upcoming - now < CATCH_UP_MIN_GAP:
                log.info("Catch-up of task %s (missed %s) skipped: next run at %s", task.id,
                         task.next_run_at, upcoming)
                task.next_run_at = upcoming
            else:
                todo.append((task.id, task.next_run_at))
    for task_id, missed_at in todo:
        log.info("Catch-up run of task %s (missed %s)", task_id, missed_at)
        await run_task(db, task_id, messenger, now)
    return len(todo)


class SchedulerRunner:
    """Background loop inside the desktop app."""

    def __init__(self, db: Database, messenger: Messenger, interval: float = 30.0,
                 catch_up_delay: float = 60.0) -> None:
        self._db, self._messenger, self._interval = db, messenger, interval
        self._catch_up_delay = catch_up_delay
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        last_housekeeping: dt.date | None = None
        clock = asyncio.get_running_loop().time
        catch_up_at = clock() + self._catch_up_delay
        caught_up = False
        while True:
            try:
                if not caught_up and clock() >= catch_up_at:
                    # Runs missed while the app was closed wait until shortly after login, once.
                    caught_up = True
                    await catch_up(self._db, self._messenger)
                if caught_up:
                    await run_due(self._db, self._messenger)
            except Exception:
                log.exception("Scheduler tick failed")
            if last_housekeeping != dt.date.today():  # once a day (and right after start)
                last_housekeeping = dt.date.today()
                try:
                    from caspian.services.ai import history

                    await history.purge(self._db)
                except Exception:
                    log.exception("Housekeeping failed")
            await asyncio.sleep(self._interval)
