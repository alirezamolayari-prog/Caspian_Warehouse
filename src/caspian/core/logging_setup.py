import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from caspian.core.settings import log_dir


def log_file() -> Path:
    return log_dir() / "caspian.log"


def setup_logging(level: int = logging.INFO) -> None:
    log_dir().mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_file(), maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers[:] = [file_handler, console]
