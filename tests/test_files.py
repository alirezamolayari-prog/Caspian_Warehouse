from pathlib import Path

import pytest

from caspian.core import files
from caspian.core.files import default_export_dir, export_path, remember_export_dir, safe_filename
from caspian.core.settings import Settings

WINDOWS_INVALID = set('\\/:*?"<>|')


def _is_valid_windows_name(name: str) -> bool:
    return (bool(name) and not (set(name) & WINDOWS_INVALID) and name == name.rstrip(" .")
            and all(ord(c) >= 32 for c in name)
            and name.split(".")[0].upper() not in files.RESERVED_NAMES)


def test_report_title_with_colon_becomes_valid():
    """«کاردکس کالا: جارو.xlsx» was rejected by Windows ("file name is not valid") (#6)."""
    name = safe_filename("کاردکس کالا: جارو.xlsx")
    assert name == "کاردکس کالا جارو.xlsx"
    assert _is_valid_windows_name(name)
    assert safe_filename("کاردکس کالا: جارو") == "کاردکس کالا جارو"


@pytest.mark.parametrize("raw", ['a\\b/c:d*e?f"g<h>i|j.pdf', "report. . .", "  ", "***", "tab\there\n.pdf",
                                 "CON.pdf", "nul", "lpt1.txt", "x" * 400 + ".xlsx"])
def test_any_input_gives_a_valid_name(raw):
    name = safe_filename(raw)
    assert _is_valid_windows_name(name), name
    assert len(name) <= files.MAX_NAME_LENGTH


def test_extension_survives_truncation():
    assert safe_filename("x" * 400 + ".xlsx").endswith(".xlsx")


def test_export_folder_defaults_to_documents_and_is_remembered(tmp_path, monkeypatch):
    documents = tmp_path / "Documents"
    documents.mkdir()
    monkeypatch.setattr(files, "user_documents_dir", lambda: str(documents))
    settings = Settings()
    assert default_export_dir() == documents
    assert Path(export_path(settings, "کاردکس: جارو.pdf")) == documents / "کاردکس جارو.pdf"

    chosen = tmp_path / "exports"
    chosen.mkdir()
    remember_export_dir(settings, str(chosen / "a.xlsx"))
    assert Settings.load().last_export_dir == str(chosen)  # saved (to the isolated settings file)
    assert Path(export_path(settings, "b.pdf")) == chosen / "b.pdf"

    chosen.rmdir()  # a remembered folder that no longer exists falls back to Documents
    assert Path(export_path(settings, "b.pdf")) == documents / "b.pdf"
