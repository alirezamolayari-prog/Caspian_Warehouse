"""Shared MCP switches (app_settings). Secrets such as the HTTP token stay per PC (keyring)."""

import secrets

from caspian.core.permissions import Perm
from caspian.core.secrets import get_secret, set_secret
from caspian.db.database import Database
from caspian.db.models import AppSetting
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.errors import PermissionDenied

ALLOW_DRAFTS_KEY = "mcp_allow_drafts"
TOKEN_SECRET = ("mcp", "http_token")


async def allow_drafts(db: Database) -> bool:
    """Whether external agents may create DRAFT documents (never post them). Off by default."""
    async with db.session() as s:
        row = await s.get(AppSetting, ALLOW_DRAFTS_KEY)
        return bool(row and row.value)


async def set_allow_drafts(db: Database, actor: Actor, enabled: bool) -> None:
    actor.require(Perm.SETTINGS_EDIT)
    if actor.is_ai:
        raise PermissionDenied("دستیار هوشمند نمی‌تواند دسترسی MCP را تغییر دهد.")
    async with db.session(actor.user_id) as s:
        row = await s.get(AppSetting, ALLOW_DRAFTS_KEY)
        if row is None:
            s.add(AppSetting(key=ALLOW_DRAFTS_KEY, value=enabled))
        else:
            row.value = enabled
        audit.record(s, actor, "mcp.drafts_enabled" if enabled else "mcp.drafts_disabled")


def http_token() -> str | None:
    return get_secret(*TOKEN_SECRET)


def new_http_token() -> str:
    """A fresh random bearer token, stored in this PC's Windows Credential Manager."""
    token = secrets.token_urlsafe(32)
    set_secret(*TOKEN_SECRET, token)
    return token
