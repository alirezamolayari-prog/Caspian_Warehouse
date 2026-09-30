"""Export file names and folders (report titles often contain characters Windows rejects)."""

import re
from pathlib import Path

from platformdirs import user_documents_dir

from caspian.core.settings import Settings

_INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                  *(f"LPT{i}" for i in range(1, 10))}
MAX_NAME_LENGTH = 150


def safe_filename(name: str, fallback: str = "export") -> str:
    """A valid Windows file name: invalid characters removed, no trailing dots/spaces,
    no reserved device names, and a sane length (the extension is kept)."""
    text = " ".join(_INVALID.sub(" ", name).split())
    stem, dot, ext = text.rpartition(".")
    if not dot or not ext or " " in ext or len(ext) > 10:
        stem, ext = text, ""
    stem = stem.rstrip(" .") or fallback
    if stem.split(".")[0].upper() in RESERVED_NAMES:
        stem = f"_{stem}"
    suffix = f".{ext}" if ext else ""
    stem = stem[:MAX_NAME_LENGTH - len(suffix)].rstrip(" .") or fallback
    return stem + suffix


def default_export_dir() -> Path:
    return Path(user_documents_dir())


def export_path(settings: Settings, default_name: str) -> str:
    """Suggested path for a save dialog: the last folder used on this PC, else Documents."""
    folder = Path(settings.last_export_dir) if settings.last_export_dir else None
    if folder is None or not folder.is_dir():
        folder = default_export_dir()
    return str(folder / safe_filename(default_name))


def remember_export_dir(settings: Settings, chosen_path: str) -> None:
    folder = str(Path(chosen_path).parent)
    if folder != settings.last_export_dir:
        settings.last_export_dir = folder
        settings.save()
