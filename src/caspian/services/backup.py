"""Encrypted backups (.bak) and PIN-protected restore.

File format (all free/open tooling):
    b"CWBAK1\\n" + <4-byte header length> + <header JSON> + <AES-256-GCM ciphertext> + <16-byte tag>
The plaintext is a gzip-compressed SQL dump from `mariadb-dump` (or SQLite's iterdump in
development). The key comes from the backup password via scrypt; the header (unencrypted)
records creation time, database, app version and schema revision.

Without a backup password on the PC the payload is the gzip stream itself (header
"cipher": "none", no tag): readable by anyone who gets the file, but a backup instead of none
(#33). gzip's CRC still detects damage. The UI warns while backups are unencrypted.

Everything streams in chunks, so large databases never need to fit in memory. Work runs in
a worker thread (subprocess pipes) so the UI stays responsive.
"""

import asyncio
import datetime as dt
import json
import logging
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from caspian import __version__
from caspian.core.permissions import Perm
from caspian.core.secrets import get_secret, set_secret
from caspian.db.database import CHARSET, COLLATION, Database, DbConfig
from caspian.db.migrate import current_revision
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.errors import ServiceError, ValidationError
from caspian.services.protected import Approval, ProtectedAction, consume

log = logging.getLogger(__name__)

MAGIC = b"CWBAK1\n"
CHUNK = 1024 * 1024
TAG_SIZE = 16
SCRYPT = {"n": 2**15, "r": 8, "p": 1}
MIN_PASSWORD = 8
FILE_PATTERN = re.compile(r"^caspian-\d{8}-\d{6}.*\.bak$")


class BackupError(ServiceError):
    pass


# ----- tools -----


def find_tool(name: str, extra_dirs: list[str] | None = None) -> str | None:
    """Locate a MariaDB client tool (mariadb-dump / mariadb)."""
    exe = name + (".exe" if os.name == "nt" else "")
    # Installed builds ship the client tools next to the app (tools\mariadb).
    bundled = Path(sys.executable).parent / "tools" / "mariadb"
    for directory in [*(extra_dirs or []), str(bundled)]:
        candidate = Path(directory) / exe
        if candidate.is_file():
            return str(candidate)
    if found := shutil.which(name):
        return found
    roots = [Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")), Path("C:/"), Path("D:/"),
             Path("E:/")]
    for root in roots:
        for pattern in ("MariaDB*/bin", "MariaDB/*/bin", "MariaDB*/*/bin"):
            for candidate in sorted(root.glob(f"{pattern}/{exe}"), reverse=True):
                return str(candidate)
    return None


# ----- dumpers -----


class Dumper(Protocol):
    db_name: str

    def dump(self, write: Callable[[bytes], None]) -> None: ...

    def load(self, chunks: Iterator[bytes]) -> None: ...


class MariaDbDumper:
    def __init__(self, config: DbConfig, password: str, tools_dir: str | None = None) -> None:
        self.config, self.password = config, password
        self.db_name = config.name
        dirs = [tools_dir] if tools_dir else []
        self.dump_exe = find_tool("mariadb-dump", dirs)
        self.client_exe = find_tool("mariadb", dirs)

    def _env(self) -> dict:
        # Password via environment, not the command line (visible to other processes).
        return {**os.environ, "MYSQL_PWD": self.password}

    def _common(self) -> list[str]:
        return [f"--host={self.config.host}", f"--port={self.config.port}",
                f"--user={self.config.user}", "--default-character-set=utf8mb4"]

    def dump(self, write: Callable[[bytes], None]) -> None:
        if not self.dump_exe:
            raise BackupError("ابزار mariadb-dump پیدا نشد. مسیر ابزارهای MariaDB را در تنظیمات "
                              "پشتیبان‌گیری مشخص کنید.")
        cmd = [self.dump_exe, *self._common(), "--single-transaction", "--routines", "--hex-blob",
               "--add-drop-table", self.config.name]  # no --databases: restorable under any name
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._env(),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as proc:
            while chunk := proc.stdout.read(CHUNK):
                write(chunk)
            error = proc.stderr.read().decode("utf-8", "replace")
            if proc.wait() != 0:
                raise BackupError(f"mariadb-dump خطا داد: {error.strip()[:300]}")

    def load(self, chunks: Iterator[bytes]) -> None:
        if not self.client_exe:
            raise BackupError("ابزار mariadb (کلاینت) پیدا نشد.")
        name = self.config.name
        prelude = (f"DROP DATABASE IF EXISTS `{name}`;\n"
                   f"CREATE DATABASE `{name}` CHARACTER SET {CHARSET} COLLATE {COLLATION};\n"
                   f"USE `{name}`;\n").encode()
        cmd = [self.client_exe, *self._common(), "--binary-mode"]
        with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, env=self._env(),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as proc:
            try:
                proc.stdin.write(prelude)
                for chunk in chunks:
                    proc.stdin.write(chunk)
                proc.stdin.close()
            except BrokenPipeError:
                pass
            error = proc.stderr.read().decode("utf-8", "replace")
            if proc.wait() != 0:
                raise BackupError(f"بازیابی با خطا متوقف شد: {error.strip()[:300]}")


class SqliteDumper:
    """Development/test backend."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.db_name = self.path.stem

    def dump(self, write: Callable[[bytes], None]) -> None:
        conn = sqlite3.connect(self.path)
        try:
            for line in conn.iterdump():
                write((line + "\n").encode("utf-8"))
        finally:
            conn.close()

    def load(self, chunks: Iterator[bytes]) -> None:
        script = b"".join(chunks).decode("utf-8")
        tmp = self.path.with_suffix(".restoring")
        tmp.unlink(missing_ok=True)
        conn = sqlite3.connect(tmp)
        try:
            conn.executescript(script)
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp, self.path)


# ----- container -----


def _key(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, **SCRYPT).derive(password.encode("utf-8"))


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    created_at: dt.datetime
    database: str
    app_version: str
    schema_revision: str | None
    label: str
    size: int
    encrypted: bool = True

    @property
    def name(self) -> str:
        return self.path.name


def read_header(path: Path) -> tuple[dict, int]:
    """(header, offset of ciphertext)."""
    with path.open("rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise BackupError("این فایل نسخه پشتیبان انبار کاسپین نیست.")
        (length,) = struct.unpack(">I", f.read(4))
        header = json.loads(f.read(length).decode("utf-8"))
    return header, len(MAGIC) + 4 + length


def _info(path: Path) -> BackupInfo:
    header, _ = read_header(path)
    return BackupInfo(path, dt.datetime.fromisoformat(header["created_at"]), header["database"],
                      header.get("app_version", ""), header.get("schema_revision"),
                      header.get("label", ""), path.stat().st_size, _encrypted(header))


def _encrypted(header: dict) -> bool:
    return header.get("cipher", "AES-256-GCM") != "none"


class _Plain:
    """Stand-in encryptor for backups made without a password."""

    tag = b""

    @staticmethod
    def update(data: bytes) -> bytes:
        return data

    @staticmethod
    def finalize() -> bytes:
        return b""


def write_backup(dumper: Dumper, path: Path, password: str | None, header_extra: dict) -> None:
    """Dump -> gzip -> AES-GCM (or nothing without a password) -> file, streaming.
    Removes the partial file on failure."""
    header = {"format": 1, "created_at": dt.datetime.now().isoformat(timespec="seconds"),
              "database": dumper.db_name, "compression": "gzip", **header_extra}
    if password:
        salt, nonce = os.urandom(16), os.urandom(12)
        header.update({"cipher": "AES-256-GCM", "kdf": "scrypt", "scrypt": SCRYPT, "salt": salt.hex(),
                       "nonce": nonce.hex()})
        encryptor = Cipher(algorithms.AES(_key(password, salt)), modes.GCM(nonce)).encryptor()
    else:
        header["cipher"] = "none"
        encryptor = _Plain()
    header_bytes = json.dumps(header, ensure_ascii=False).encode("utf-8")
    partial = path.with_suffix(".partial")
    try:
        with partial.open("wb") as f:
            f.write(MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes)
            compressor = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)  # gzip framing

            def write(chunk: bytes) -> None:
                if data := compressor.compress(chunk):
                    f.write(encryptor.update(data))

            dumper.dump(write)
            f.write(encryptor.update(compressor.flush()))
            f.write(encryptor.finalize())
            f.write(encryptor.tag)
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def iter_plaintext(path: Path, password: str | None) -> Iterator[bytes]:
    """Decrypt + gunzip in chunks. The GCM tag (or, unencrypted, gzip's CRC) is checked at the
    end: callers must fully consume the iterator before trusting (or committing) the data."""
    header, offset = read_header(path)
    if not _encrypted(header):
        yield from _iter_gzip(path, offset)
        return
    if not password:
        raise BackupError("این نسخه پشتیبان رمزدار است؛ رمز آن را وارد کنید.")
    total = path.stat().st_size
    decryptor = Cipher(algorithms.AES(_key(password, bytes.fromhex(header["salt"]))),
                       modes.GCM(bytes.fromhex(header["nonce"]))).decryptor()
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    with path.open("rb") as f:
        f.seek(total - TAG_SIZE)
        tag = f.read(TAG_SIZE)
        f.seek(offset)
        remaining = total - TAG_SIZE - offset
        while remaining > 0:
            chunk = f.read(min(CHUNK, remaining))
            remaining -= len(chunk)
            try:
                data = decompressor.decompress(decryptor.update(chunk))
            except zlib.error as exc:
                raise BackupError("رمز پشتیبان نادرست است یا فایل آسیب دیده است.") from exc
            if data:
                yield data
    try:
        decryptor.finalize_with_tag(tag)
    except Exception as exc:
        raise BackupError("رمز پشتیبان نادرست است یا فایل آسیب دیده است.") from exc
    if tail := decompressor.flush():
        yield tail


def _iter_gzip(path: Path, offset: int) -> Iterator[bytes]:
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        with path.open("rb") as f:
            f.seek(offset)
            while chunk := f.read(CHUNK):
                if data := decompressor.decompress(chunk):
                    yield data
        if tail := decompressor.flush():
            yield tail
    except zlib.error as exc:
        raise BackupError("فایل پشتیبان آسیب دیده است.") from exc
    if not decompressor.eof:
        raise BackupError("فایل پشتیبان ناقص یا آسیب دیده است.")


def verify_backup_file(path: Path, password: str | None) -> BackupInfo:
    """Full decrypt/decompress pass without writing anything."""
    size = 0
    for chunk in iter_plaintext(path, password):
        size += len(chunk)
    if size == 0:
        raise BackupError("نسخه پشتیبان خالی است.")
    return _info(path)


# ----- service API -----


def list_backups(directory: str | Path) -> list[BackupInfo]:
    folder = Path(directory)
    if not folder.is_dir():
        return []
    out = []
    for path in folder.glob("*.bak"):
        try:
            out.append(_info(path))
        except (BackupError, OSError, ValueError, KeyError):
            continue
    return sorted(out, key=lambda b: b.created_at, reverse=True)


def prune(directory: str | Path, keep: int) -> int:
    """Delete our oldest backups beyond `keep` (only files named like ours). Returns count."""
    if keep <= 0:
        return 0
    ours = [b for b in list_backups(directory) if FILE_PATTERN.match(b.name)]
    removed = 0
    for info in ours[keep:]:
        info.path.unlink(missing_ok=True)
        removed += 1
    return removed


def _check_password(password: str) -> None:
    if not password or len(password) < MIN_PASSWORD:
        raise ValidationError(f"رمز پشتیبان باید حداقل {MIN_PASSWORD} کاراکتر باشد. آن را در تنظیمات "
                              "پشتیبان‌گیری تعیین کنید و جایی امن یادداشت کنید.")


async def _schema_revision(db: Database) -> str | None:
    try:
        return await current_revision(db)
    except Exception:
        return None


async def create_backup(db: Database, actor: Actor | None, dumper: Dumper, password: str | None,
                        directory: str | Path, label: str = "", keep: int = 0) -> BackupInfo:
    """`password` None/empty: an unencrypted backup (#33); otherwise it must be strong enough."""
    if actor is not None:
        actor.require(Perm.BACKUP_CREATE)
    if password:
        _check_password(password)
    folder = Path(directory)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"پوشه پشتیبان قابل ساخت نیست: {folder}") from exc
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^\w-]+", "-", label.strip(), flags=re.UNICODE).strip("-")[:40]
    path = folder / f"caspian-{stamp}{'-' + slug if slug else ''}.bak"
    extra = {"app_version": __version__, "schema_revision": await _schema_revision(db),
             "label": label.strip()}
    await asyncio.to_thread(write_backup, dumper, path, password, extra)
    info = _info(path)
    if keep:
        await asyncio.to_thread(prune, folder, keep)
    async with db.session() as s:
        audit.record(s, actor, "backup.created", details={"file": info.name, "size": info.size,
                                                         "label": label, "encrypted": info.encrypted})
    log.info("Backup written: %s (%s bytes)", path, info.size)
    return info


async def verify_backup(path: str | Path, password: str | None) -> BackupInfo:
    return await asyncio.to_thread(verify_backup_file, Path(path), password)


async def restore_backup(db: Database, actor: Actor, dumper: Dumper, path: str | Path,
                         password: str | None, approval: Approval | None, safety_dir: str | Path,
                         after_restore: Callable | None = None) -> BackupInfo:
    """Replace the current database with a backup. Protected: admin PIN approval required.

    Order: validate approval -> verify the whole file (tag + gzip) -> safety backup of the
    current data -> drop & reload -> migrate/seed via `after_restore` -> audit.
    """
    actor.require_human("بازیابی نسخه پشتیبان")
    approver_id = consume(approval, ProtectedAction.RESTORE_BACKUP, actor)
    path = Path(path)
    info = await verify_backup(path, password)
    # The safety copy of the current data is protected like the backup being restored.
    safety = await create_backup(db, actor, dumper, password, safety_dir, label="pre-restore")
    await db.dispose()  # no open connections into the database we are about to drop
    await asyncio.to_thread(dumper.load, iter_plaintext(path, password))
    await db.dispose()
    if after_restore is not None:
        await after_restore()
    async with db.session() as s:
        audit.record(s, actor, "backup.restored", details={
            "file": info.name, "backup_created_at": info.created_at.isoformat(),
            "safety_backup": safety.name}, approved_by_id=approver_id)
    return info


# ----- per-PC settings -----


def backup_password() -> str | None:
    return get_secret("backup", "password")


def set_backup_password(password: str) -> None:
    _check_password(password)
    set_secret("backup", "password", password)


def make_dumper(db: Database, config: DbConfig, app_password: str, tools_dir: str = "") -> Dumper:
    if db.is_sqlite:
        return SqliteDumper(db.url.database)
    return MariaDbDumper(config, app_password, tools_dir or None)


def make_scheduled_handler(db: Database, config: DbConfig, app_password: str, settings):
    """Handler for TaskKind.BACKUP tasks (runs as the system, audited with no user)."""
    from caspian.core.settings import default_backup_dir

    async def handler(_db: Database, _params: dict) -> str:
        password = backup_password()  # none: an unencrypted backup beats no backup (#33)
        info = await create_backup(
            db, None, make_dumper(db, config, app_password, settings.mariadb_tools_dir), password,
            settings.backup_dir or default_backup_dir(), "زمان‌بندی‌شده", keep=settings.backup_keep)
        warning = "" if info.encrypted else " — بدون رمز! رمز پشتیبان را در تنظیمات تعیین کنید"
        return f"{info.name} ({info.size / 1_048_576:.1f} MB){warning}"

    return handler
