"""Branding of this installation: the only place to change for another mall or shop.

Swap `resources/branding/logo.png` and the names below. The company name saved in the database
(app_settings «company_name») wins when present, so an existing installation keeps its name and an
admin can change it without touching code.
"""

from pathlib import Path

from caspian.resources import RESOURCES_DIR

COMPANY_NAME = "بازار مبلمان کاسپین"
LOGO_PATH: Path = RESOURCES_DIR / "branding" / "logo.png"


def company_name(saved: object = None) -> str:
    """The name to show: the saved setting, else the default above."""
    text = str(saved).strip() if saved is not None else ""
    return text or COMPANY_NAME


def logo_path() -> Path | None:
    """The logo file, or None when it is missing (callers then show no logo / a plain icon)."""
    return LOGO_PATH if LOGO_PATH.is_file() else None
