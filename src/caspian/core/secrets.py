"""Secrets live in the Windows Credential Manager (via keyring), never in plain files."""

import contextlib
import logging

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from caspian import APP_NAME

log = logging.getLogger(__name__)


def _account(kind: str, name: str) -> str:
    return f"{kind}:{name}"


def get_secret(kind: str, name: str) -> str | None:
    try:
        return keyring.get_password(APP_NAME, _account(kind, name))
    except KeyringError:
        log.exception("Credential store read failed")
        return None


def set_secret(kind: str, name: str, value: str) -> None:
    keyring.set_password(APP_NAME, _account(kind, name), value)


def delete_secret(kind: str, name: str) -> None:
    with contextlib.suppress(PasswordDeleteError):
        keyring.delete_password(APP_NAME, _account(kind, name))
