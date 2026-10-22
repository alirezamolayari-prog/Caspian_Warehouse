"""Background sync with Caspian Server: send the outbox, then ask `/changes`.

One tick = flush the outbox in order → `GET /changes?since=<next_since>` → upsert by server id →
make draft vouchers for approved requests. The server repeats rows on purpose (a 2 s overlap), so
every row is matched by its id, never appended twice. Sending is safe to repeat:
- a create carries `client_request_id`, so a retry after a lost answer gets the same request (200);
- «issued» with the same number is accepted again (200);
- a resubmit or «issued» answered 409 is checked against the server's current state;
- a chat message sent before is recognised when it comes back from `/changes`.
"""

import asyncio
import contextlib
import datetime as dt
import enum
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.db.database import Database
from caspian.db.models import (
    AppSetting,
    GoodsRequest,
    GoodsRequestLine,
    GoodsRequestMessage,
    Item,
    OutboxOp,
    RequestStatus,
    ServerOutbox,
    Unit,
)
from caspian.services.actor import Actor
from caspian.services.server_link import connection, goods, issue_link
from caspian.services.server_link.client import ServerClient
from caspian.services.server_link.errors import (
    ServerAccountRejected,
    ServerError,
    ServerRejected,
)

log = logging.getLogger(__name__)

SYNC_KEY = "server.sync"  # {"next_since": "<ISO time the server gave>"}
INTERVAL_SECONDS = 10.0
MAX_INTERVAL_SECONDS = 60.0

_STATUS = {"pending": RequestStatus.PENDING, "approved": RequestStatus.APPROVED,
           "rejected": RequestStatus.REJECTED, "issued": RequestStatus.ISSUED}
_STATE_OPS = (OutboxOp.CREATE, OutboxOp.RESUBMIT, OutboxOp.ISSUED)


class SyncState(enum.StrEnum):
    NOT_CONFIGURED = "not_configured"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ACCOUNT_REJECTED = "account_rejected"


@dataclass(frozen=True)
class SyncEvent:
    request_id: int
    kind: str  # "approved" | "rejected" | "message"
    text: str


@dataclass
class TickResult:
    state: SyncState
    message: str = ""
    events: list[SyncEvent] = field(default_factory=list)
    changed: bool = False  # local requests/messages/documents changed: refresh what's on screen


def parse_time(value: str | None) -> dt.datetime | None:
    """Server UTC time → this PC's local time (naive, like every other time in the app)."""
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone().replace(tzinfo=None)


@dataclass
class _Op:
    """Everything one outbox row needs for its HTTP call, read before the call."""

    id: int
    request_id: int
    op: OutboxOp
    attempts: int
    server_id: int | None
    payload: dict
    message_id: int | None = None


class SyncEngine:
    def __init__(self, db: Database, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._db = db
        self._transport = transport
        self._client: ServerClient | None = None
        self._key: tuple | None = None
        self._rejected_key: tuple | None = None

    def reset_rejection(self) -> None:
        """Try a refused account again (e.g. the server admin reactivated it)."""
        self._rejected_key = None

    async def _get_client(self) -> ServerClient | None:
        conn = await connection.get_connection(self._db)
        if conn is None:
            return None
        password = connection.get_password(conn) or ""
        key = (conn.url, conn.username, password)
        if key != self._key:
            self._client = ServerClient(conn.url, conn.username, password, self._transport)
            self._key = key
        return self._client

    async def tick(self, actor: Actor | None) -> TickResult:
        result = TickResult(SyncState.CONNECTED)
        try:
            client = await self._get_client()
            if client is None:
                result.state = SyncState.NOT_CONFIGURED
            elif self._rejected_key == self._key:
                result.state, result.message = SyncState.ACCOUNT_REJECTED, ServerAccountRejected().message
            else:
                result.changed |= await self.flush(client)
                events, changed = await self.poll(client)
                result.events, result.changed = events, result.changed or changed
        except ServerAccountRejected as exc:
            self._rejected_key = self._key
            result.state, result.message = SyncState.ACCOUNT_REJECTED, exc.message
        except ServerError as exc:
            result.state, result.message = SyncState.DISCONNECTED, exc.message
        try:  # approvals already here become vouchers even while the server is unreachable
            result.changed |= bool(await issue_link.ensure_issue_drafts(self._db, actor))
        except Exception:
            log.exception("Making issue vouchers for approved requests failed")
        return result

    # ----- outbox -----

    async def flush(self, client: ServerClient) -> bool:
        """Send waiting rows in order. Stops at a connection problem (they stay for the next tick);
        a row the server refuses is marked failed and later rows of that request wait."""
        async with self._db.session() as s:
            rows = list((await s.scalars(select(ServerOutbox).order_by(ServerOutbox.id))).all())
        blocked = {r.request_id for r in rows if r.failed}
        changed = False
        for row in rows:
            if row.failed or row.request_id in blocked:
                continue
            op = await self._claim(row.id)
            if op is None:
                continue
            try:
                answer = await self._perform(client, op)
            except ServerRejected as exc:
                answer = await self._resolve(client, op, exc)
                if answer is None:
                    await self._fail(op, exc)
                    blocked.add(op.request_id)
                    changed = True
                    continue
            await self._done(op, answer)
            changed = True
        return changed

    async def _claim(self, op_id: int) -> _Op | None:
        """Count the attempt *before* sending, so a retry knows an answer may have been lost."""
        async with self._db.session() as s:
            row = await s.get(ServerOutbox, op_id)
            if row is None:
                return None
            req = await s.get(GoodsRequest, row.request_id)
            row.attempts += 1
            payload = dict(row.payload or {})
            if row.op in (OutboxOp.CREATE, OutboxOp.RESUBMIT):
                payload = goods.payload_of(req)
            elif row.op == OutboxOp.MESSAGE:
                message = await s.get(GoodsRequestMessage, row.message_id)
                payload = {"text": message.text if message else ""}
            return _Op(row.id, row.request_id, row.op, row.attempts, req.server_id, payload, row.message_id)

    async def _perform(self, client: ServerClient, op: _Op):
        if op.op == OutboxOp.CREATE:
            return await client.call("POST", "/requests", json=op.payload)
        if op.server_id is None:  # its create never reached the server
            raise ServerRejected(404)
        if op.op == OutboxOp.RESUBMIT:
            return await client.call("PUT", f"/requests/{op.server_id}", json=op.payload)
        if op.op == OutboxOp.ISSUED:
            return await client.call("POST", f"/requests/{op.server_id}/issued", json=op.payload)
        if op.attempts > 1 and (sent := await self._already_sent(client, op)) is not None:
            return sent
        return await client.call("POST", f"/requests/{op.server_id}/messages", json=op.payload)

    async def _already_sent(self, client: ServerClient, op: _Op) -> dict | None:
        """A message whose answer was lost: is it on the server already (same text, from us)?"""
        async with self._db.session() as s:
            known = set((await s.scalars(select(GoodsRequestMessage.server_id).where(
                GoodsRequestMessage.request_id == op.request_id,
                GoodsRequestMessage.server_id.is_not(None)))).all())
        for message in await client.call("GET", f"/requests/{op.server_id}/messages"):
            if (message["sender_role"] == "warehouse" and message["text"] == op.payload["text"]
                    and message["id"] not in known):
                return message
        return None

    async def _resolve(self, client: ServerClient, op: _Op, exc: ServerRejected) -> dict | None:
        """409 after a retry: the earlier attempt may have worked. Accept the server's state if it
        is exactly what this row wanted."""
        if exc.status != 409 or op.server_id is None or op.op not in (OutboxOp.RESUBMIT, OutboxOp.ISSUED):
            return None
        current = await client.call("GET", f"/requests/{op.server_id}")
        if op.op == OutboxOp.RESUBMIT and current["status"] == "pending":
            return current
        if (op.op == OutboxOp.ISSUED and current["status"] == "issued"
                and current.get("issue_number") == op.payload.get("issue_number")):
            return current
        return None

    async def _fail(self, op: _Op, exc: ServerRejected) -> None:
        log.warning("Outbox %s (%s) refused: %s %s", op.id, op.op, exc.status, exc.detail)
        async with self._db.session() as s:
            row = await s.get(ServerOutbox, op.id)
            if row is not None:
                row.failed, row.last_error = True, exc.message
            req = await s.get(GoodsRequest, op.request_id)
            if req is not None:
                req.last_error = f"ارسال نشد: {exc.message}"

    async def _done(self, op: _Op, answer: dict) -> None:
        async with self._db.session() as s:
            row = await s.get(ServerOutbox, op.id)
            if row is not None:
                await s.delete(row)
                await s.flush()
            req = await s.get(GoodsRequest, op.request_id)
            if req is None:
                return
            req.last_error = ""
            if op.op == OutboxOp.MESSAGE:
                message = await s.get(GoodsRequestMessage, op.message_id)
                if message is not None:
                    message.server_id = answer["id"]
                    message.created_at = parse_time(answer.get("created_at")) or message.created_at
                return
            await _apply_request(s, req, answer, initial=True)

    # ----- changes -----

    async def poll(self, client: ServerClient) -> tuple[list[SyncEvent], bool]:
        async with self._db.session() as s:
            setting = await s.get(AppSetting, SYNC_KEY)
            since = (setting.value or {}).get("next_since") if setting else None
        data = await client.call("GET", "/changes", params={"since": since} if since else None)
        requests = list(data.get("requests", []))
        messages = list(data.get("messages", []))
        # Messages of a request this PC has never seen: fetch the request itself first.
        async with self._db.session() as s:
            known = set((await s.scalars(select(GoodsRequest.server_id).where(
                GoodsRequest.server_id.is_not(None)))).all())
        listed = {r["id"] for r in requests}
        for request_id in sorted({m["request_id"] for m in messages} - known - listed):
            requests.append(await client.call("GET", f"/requests/{request_id}"))
        events: list[SyncEvent] = []
        initial = since is None  # the first sync brings history: no notifications for it
        async with self._db.session() as s:
            changed = False
            for row in requests:
                event, row_changed = await _upsert_request(s, row, initial)
                changed |= row_changed
                if event:
                    events.append(event)
            for row in messages:
                event, row_changed = await _upsert_message(s, row, initial)
                changed |= row_changed
                if event:
                    events.append(event)
            setting = await s.get(AppSetting, SYNC_KEY)
            value = {"next_since": data.get("next_since")}
            if setting is None:
                s.add(AppSetting(key=SYNC_KEY, value=value))
            else:
                setting.value = value
        return events, changed


async def _state_ops(s: AsyncSession, request_id: int) -> int:
    return await s.scalar(select(func.count()).select_from(ServerOutbox).where(
        ServerOutbox.request_id == request_id, ServerOutbox.op.in_(_STATE_OPS))) or 0


async def _apply_request(s: AsyncSession, req: GoodsRequest, row: dict, initial: bool) -> SyncEvent | None:
    """Server row → local request. While this PC still has a create/resubmit/«issued» waiting, the
    local status wins (the server hasn't heard it yet); an issued request never goes back."""
    req.server_id = row["id"]
    req.rejection_reason = row.get("rejection_reason") or ""
    req.decided_at = parse_time(row.get("decided_at"))
    if row.get("issue_number") and not req.issue_number:
        req.issue_number = row["issue_number"]
    new = _STATUS.get(row.get("status"))
    if new is None or new == req.status or req.status == RequestStatus.ISSUED:
        return None
    if await _state_ops(s, req.id):
        return None
    old, req.status = req.status, new
    if (new in (RequestStatus.APPROVED, RequestStatus.REJECTED) and req.origin == "local" and not initial
            and old in (RequestStatus.PENDING, RequestStatus.QUEUED)):
        req.unseen = True
        kind = "approved" if new == RequestStatus.APPROVED else "rejected"
        text = (f"درخواست {req.requester_name} تأیید شد." if kind == "approved"
                else f"درخواست {req.requester_name} رد شد.")
        return SyncEvent(req.id, kind, text)
    return None


async def _upsert_request(s: AsyncSession, row: dict, initial: bool) -> tuple[SyncEvent | None, bool]:
    req = await s.scalar(select(GoodsRequest).where(GoodsRequest.server_id == row["id"]))
    if req is None and row.get("client_request_id"):
        req = await s.scalar(select(GoodsRequest).where(
            GoodsRequest.client_request_id == row["client_request_id"]))
    if req is None:
        s.add(await _remote_request(s, row))
        return None, True
    before = (req.status, req.rejection_reason, req.issue_number, req.decided_at)
    event = await _apply_request(s, req, row, initial)
    return event, event is not None or before != (req.status, req.rejection_reason, req.issue_number,
                                                  req.decided_at)


async def _remote_request(s: AsyncSession, row: dict) -> GoodsRequest:
    """A request registered elsewhere (not on this PC): shown read-only, never issued from here."""
    req = GoodsRequest(
        client_request_id=row.get("client_request_id") or str(uuid.uuid4()), server_id=row["id"],
        status=_STATUS.get(row.get("status"), RequestStatus.PENDING), origin="remote",
        requester_name=(row.get("requester_name") or "")[:100],
        requester_phone=(row.get("requester_phone") or "")[:30],
        location_text=(row.get("consumption_location") or "")[:200], note=row.get("note") or "",
        rejection_reason=row.get("rejection_reason") or "", decided_at=parse_time(row.get("decided_at")),
        issue_number=row.get("issue_number") or "")
    for no, item in enumerate(row.get("items", []), start=1):
        code = item.get("item_code") or ""
        local = await s.scalar(select(Item).where(Item.code == code)) if code else None
        unit = await s.scalar(select(Unit).where(Unit.name == item.get("unit")))
        req.lines.append(GoodsRequestLine(
            line_no=no, item_id=local.id if local else None, unit_id=unit.id if unit else None,
            qty=Decimal(str(item.get("quantity", 0))), item_code=code, item_name=item.get("name") or "",
            unit_name=item.get("unit") or ""))
    return req


async def _upsert_message(s: AsyncSession, row: dict, initial: bool) -> tuple[SyncEvent | None, bool]:
    if await s.scalar(select(GoodsRequestMessage.id).where(GoodsRequestMessage.server_id == row["id"])):
        return None, False
    req = await s.scalar(select(GoodsRequest).where(GoodsRequest.server_id == row["request_id"]))
    if req is None:
        return None, False
    if row.get("sender_role") == "warehouse":
        # Our own message whose answer was lost: link it instead of showing it twice.
        mine = await s.scalar(select(GoodsRequestMessage).where(
            GoodsRequestMessage.request_id == req.id, GoodsRequestMessage.server_id.is_(None),
            GoodsRequestMessage.text == row.get("text")).order_by(GoodsRequestMessage.id).limit(1))
        if mine is not None:
            mine.server_id = row["id"]
            for op in (await s.scalars(select(ServerOutbox).where(ServerOutbox.message_id == mine.id))).all():
                await s.delete(op)
            return None, True
    owner = row.get("sender_role") == "owner"
    s.add(GoodsRequestMessage(
        request_id=req.id, server_id=row["id"], sender_role=row.get("sender_role") or "",
        sender_name=goods.OWNER_NAME if owner else (row.get("sender_name") or "")[:150],
        text=row.get("text") or "", created_at=parse_time(row.get("created_at")) or dt.datetime.now()))
    if row.get("sender_role") == "warehouse" or initial:
        return None, True
    req.unseen = True
    return SyncEvent(req.id, "message", f"پیام تازه از مالک دربارهٔ درخواست {req.requester_name}"), True


class SyncRunner:
    """The background loop: one tick every ~10 s (slower while the server is unreachable). Never
    blocks the UI — everything it waits for is asynchronous. `on_result` receives each TickResult."""

    def __init__(self, engine: SyncEngine, on_result: Callable[[TickResult], None] | None = None,
                 interval: float = INTERVAL_SECONDS, max_interval: float = MAX_INTERVAL_SECONDS) -> None:
        self.engine = engine
        self.on_result = on_result
        self._interval, self._max = interval, max_interval
        self._actor: Actor | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    def set_actor(self, actor: Actor | None) -> None:
        self._actor = actor
        self.wake()

    def wake(self) -> None:
        """Sync now (e.g. right after a request was registered)."""
        self._wake.set()

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
        delay = self._interval
        while True:
            self._wake.clear()
            try:
                result = await self.engine.tick(self._actor)
            except Exception:
                log.exception("Server sync tick failed")
                result = TickResult(SyncState.DISCONNECTED,
                                    "خطای غیرمنتظره در همگام‌سازی؛ جزئیات در فایل گزارش.")
            if self.on_result is not None:
                try:
                    self.on_result(result)
                except Exception:
                    log.exception("Server sync listener failed")
            ok = result.state in (SyncState.CONNECTED, SyncState.NOT_CONFIGURED)
            delay = self._interval if ok else min(delay * 2, self._max)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)
