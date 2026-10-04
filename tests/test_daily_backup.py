"""Daily automatic backup at a chosen time (Settings → پشتیبان‌گیری), on the existing scheduler."""

import datetime as dt
import logging

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import ScheduledTask, TaskKind, TaskStatus
from caspian.services import auth, backup, scheduler, users
from caspian.services.backup import SqliteDumper
from caspian.services.errors import PermissionDenied
from caspian.services.messaging import Messenger

EVENING = dt.datetime(2026, 10, 4, 21, 30)


async def _tasks(db) -> list[ScheduledTask]:
    async with db.session() as s:
        return list((await s.scalars(select(ScheduledTask))).all())


async def test_save_load_idempotent_and_disable(db, admin):
    assert await scheduler.daily_backup(db) == (False, None)
    await scheduler.set_daily_backup(db, admin, True, dt.time(20, 0), now=EVENING)
    await scheduler.set_daily_backup(db, admin, True, dt.time(20, 0), now=EVENING)  # saved twice
    [task] = await _tasks(db)
    assert task.kind is TaskKind.BACKUP and task.cron == "0 20 * * *"
    assert task.status is TaskStatus.ACTIVE and task.machine == scheduler.MACHINE
    assert task.params == {"auto_daily": True, "label": "خودکار"}
    # 21:30 local time already passed 20:00 today -> tomorrow 20:00
    assert task.next_run_at == dt.datetime(2026, 10, 5, 20, 0)
    assert await scheduler.daily_backup(db) == (True, dt.time(20, 0))

    await scheduler.set_daily_backup(db, admin, True, dt.time(22, 15), now=EVENING)
    [task] = await _tasks(db)
    assert task.cron == "15 22 * * *" and task.next_run_at == dt.datetime(2026, 10, 4, 22, 15)
    [row] = await scheduler.list_tasks(db, admin)  # shows in «کارهای زمان‌بندی‌شده»
    assert row.runs_here and row.status is TaskStatus.ACTIVE

    await scheduler.set_daily_backup(db, admin, False, dt.time(22, 15))
    assert await _tasks(db) == [] and await scheduler.daily_backup(db) == (False, None)
    await scheduler.set_daily_backup(db, admin, False, dt.time(22, 15))  # nothing to remove: fine


async def test_only_humans_with_settings_edit(db, admin):
    with pytest.raises(PermissionDenied):
        await scheduler.set_daily_backup(db, admin.as_ai(), True, dt.time(20, 0))
    await users.create_user(db, admin, "mgr", "مدیر انبار", "Mgr#2026pass", "manager")
    manager = (await auth.login(db, "mgr", "Mgr#2026pass")).actor
    with pytest.raises(PermissionDenied):
        await scheduler.set_daily_backup(db, manager, True, dt.time(20, 0))
    assert await _tasks(db) == []


async def test_one_catch_up_after_a_missed_day(db, admin, caplog):
    ran = []
    saved = scheduler.HANDLERS.get(TaskKind.BACKUP)

    async def handler(_db, params):
        ran.append(params["label"])
        return "ok"

    scheduler.HANDLERS[TaskKind.BACKUP] = handler
    try:
        await scheduler.set_daily_backup(db, admin, True, dt.time(20, 0), now=dt.datetime(2026, 10, 1, 9))
        # The app was closed on 1, 2 and 3 Oct at 20:00; opened on 4 Oct at 10:00.
        login = dt.datetime(2026, 10, 4, 10, 0)
        with caplog.at_level(logging.INFO, logger="caspian.services.scheduler"):
            assert await scheduler.catch_up(db, Messenger(db), login) == 1
        assert ran == ["خودکار"] and "Catch-up run of task" in caplog.text
        assert await scheduler.catch_up(db, Messenger(db), login + dt.timedelta(minutes=5)) == 0
        assert await scheduler.run_due(db, Messenger(db), login + dt.timedelta(minutes=5)) == 0
        [task] = await _tasks(db)
        assert task.next_run_at == dt.datetime(2026, 10, 4, 20, 0) and ran == ["خودکار"]
    finally:
        scheduler.HANDLERS[TaskKind.BACKUP] = saved


async def test_no_catch_up_right_before_the_regular_run(db, admin):
    ran = []
    saved = scheduler.HANDLERS.get(TaskKind.BACKUP)

    async def handler(_db, params):
        ran.append(1)
        return "ok"

    scheduler.HANDLERS[TaskKind.BACKUP] = handler
    try:
        await scheduler.set_daily_backup(db, admin, True, dt.time(20, 0), now=dt.datetime(2026, 10, 1, 9))
        assert await scheduler.catch_up(db, Messenger(db), dt.datetime(2026, 10, 4, 19, 30)) == 0
        [task] = await _tasks(db)
        assert ran == [] and task.next_run_at == dt.datetime(2026, 10, 4, 20, 0)
    finally:
        scheduler.HANDLERS[TaskKind.BACKUP] = saved


async def test_runner_waits_for_the_catch_up_delay(db, admin):
    import asyncio

    ran = []
    saved = scheduler.HANDLERS.get(TaskKind.BACKUP)

    async def handler(_db, params):
        ran.append(1)
        return "ok"

    scheduler.HANDLERS[TaskKind.BACKUP] = handler
    runner = scheduler.SchedulerRunner(db, Messenger(db), interval=0.02, catch_up_delay=0.3)
    try:
        await scheduler.set_daily_backup(db, admin, True, dt.time(0, 0),
                                         now=dt.datetime.now() - dt.timedelta(days=3))
        runner.start()
        await asyncio.sleep(0.15)
        assert ran == []  # not at the very first tick
        for _ in range(100):
            if ran:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        assert ran == [1]  # exactly one catch-up
    finally:
        await runner.stop()
        scheduler.HANDLERS[TaskKind.BACKUP] = saved


async def test_scheduled_backup_uses_label_folder_and_retention(db, admin, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "backup_password", lambda: "Backup#Pass2026")
    monkeypatch.setattr(backup, "make_dumper",
                        lambda *a, **k: SqliteDumper(make_url(str(db.url)).database))
    folder = tmp_path / "bk"
    settings = Settings(backup_dir=str(folder), backup_keep=2)
    handler = backup.make_scheduled_handler(db, DbConfig(), "", settings)
    for _ in range(3):
        await handler(db, {"auto_daily": True, "label": "خودکار"})
    found = backup.list_backups(folder)
    assert len(found) == 2 and {b.label for b in found} == {"خودکار"} and all(b.encrypted for b in found)


async def test_backup_tab_saves_and_reloads_the_daily_backup(qtbot, themes, db, admin, tmp_path, monkeypatch):
    from PySide6.QtCore import QTime

    from caspian.ui import backup_settings
    from caspian.ui.app_context import AppContext
    from caspian.ui.backup_settings import BackupTab

    monkeypatch.setattr(backup_settings, "show_info", lambda parent, text: None)
    settings = Settings(backup_dir=str(tmp_path / "bk"))
    monkeypatch.setattr(settings, "save", lambda: None)
    ctx = AppContext(db, DbConfig(), settings, themes, admin)
    tab = BackupTab(ctx)
    qtbot.addWidget(tab)
    await tab.load_daily()
    assert not tab.daily.isChecked() and not tab.daily_time.isEnabled()
    tab.daily.setChecked(True)
    assert tab.daily_time.isEnabled()
    tab.daily_time.setTime(QTime(19, 45))
    assert await scheduler.daily_backup(db) == (False, None)  # nothing until «ذخیره تنظیمات»
    tab.keep.setValue(7)
    await tab.on_save_settings()
    assert settings.backup_keep == 7 and await scheduler.daily_backup(db) == (True, dt.time(19, 45))
    [row] = await scheduler.list_tasks(db, admin)
    assert row.cron == "45 19 * * *" and row.runs_here

    reopened = BackupTab(ctx)  # e.g. after restarting the app
    qtbot.addWidget(reopened)
    await reopened.load_daily()
    assert reopened.daily.isChecked() and reopened.daily_time.time() == QTime(19, 45)
    reopened.daily.setChecked(False)
    await reopened.on_save_settings()
    assert await scheduler.list_tasks(db, admin) == []

    await users.create_user(db, admin, "mgr", "مدیر انبار", "Mgr#2026pass", "manager")
    manager = (await auth.login(db, "mgr", "Mgr#2026pass")).actor
    limited = BackupTab(AppContext(db, DbConfig(), settings, themes, manager))
    qtbot.addWidget(limited)
    await limited.load_daily()
    assert not limited.daily.isEnabled() and not limited.daily_time.isEnabled()
