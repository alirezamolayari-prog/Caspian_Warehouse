"""4.1: the assistant is «دستیار کاسپین» everywhere on screen; the old name never comes back."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD = "دستیار هوشمند"


def test_old_assistant_name_is_gone_from_the_app_and_the_guide():
    files = [*ROOT.joinpath("src").rglob("*.py"), ROOT / "docs" / "USER_GUIDE.md"]
    offenders = [str(f.relative_to(ROOT)) for f in files if OLD in f.read_text(encoding="utf-8")]
    assert offenders == []
