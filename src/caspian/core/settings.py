"""Per-machine user preferences stored as JSON in the user's config directory.

Only UI preferences live here. Business data and secrets belong in the database
or the OS credential store.
"""

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from platformdirs import user_config_dir, user_documents_dir, user_log_dir

from caspian import APP_NAME

log = logging.getLogger(__name__)

THEME_MODES = ("system", "light", "dark")


@dataclass
class Settings:
    theme_mode: str = "system"
    window_geometry: str = ""
    sidebar_collapsed: bool = False
    # Non-secret DB connection fields (host, port, name, user); empty until configured.
    database: dict = field(default_factory=dict)
    # Backups are written by this PC (paths are local to it).
    backup_dir: str = ""
    backup_keep: int = 30
    mariadb_tools_dir: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or settings_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError):
            log.warning("Settings file unreadable, using defaults: %s", path)
            return cls()
        known = {f.name for f in fields(cls)}
        settings = cls(**{k: v for k, v in raw.items() if k in known})
        if settings.theme_mode not in THEME_MODES:
            settings.theme_mode = "system"
        return settings

    def save(self, path: Path | None = None) -> None:
        path = path or settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def config_dir() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False))


def log_dir() -> Path:
    return Path(user_log_dir(APP_NAME, appauthor=False))


def default_backup_dir() -> Path:
    return Path(user_documents_dir()) / "Caspian Backups"


def settings_path() -> Path:
    return config_dir() / "settings.json"
