from caspian.core.settings import Settings


def test_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    Settings(theme_mode="dark", sidebar_collapsed=True).save(path)
    loaded = Settings.load(path)
    assert loaded.theme_mode == "dark"
    assert loaded.sidebar_collapsed is True


def test_missing_file_gives_defaults(tmp_path):
    assert Settings.load(tmp_path / "nope.json") == Settings()


def test_corrupt_or_unknown_values(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"theme_mode": "neon", "future_key": 1}', encoding="utf-8")
    assert Settings.load(path).theme_mode == "system"
    path.write_text("{not json", encoding="utf-8")
    assert Settings.load(path) == Settings()
