import json

import httpx
import pytest

from caspian.db.models import ProviderKind
from caspian.services.ai import config
from caspian.services.ai.gateway import DEFAULT_COOLDOWN, AIUnavailable, Gateway
from caspian.services.errors import PermissionDenied, ValidationError


@pytest.fixture
def keys(monkeypatch):
    """In-memory stand-in for Windows Credential Manager."""
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(config, "get_secret", lambda k, n: store.get((k, n)))
    monkeypatch.setattr(config, "set_secret", lambda k, n, v: store.__setitem__((k, n), v))
    monkeypatch.setattr(config, "delete_secret", lambda k, n: store.pop((k, n), None))
    return store


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _answer(text="سلام"):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                                              "content": text}}]})


async def _setup(db, admin, keys):
    groq = await config.save_provider(db, admin, "Groq", ProviderKind.GROQ,
                                      "https://api.groq.com/openai/v1", "llama", api_key="k1")
    hf = await config.save_provider(db, admin, "HF", ProviderKind.HUGGINGFACE,
                                    "https://router.huggingface.co/v1", "m", api_key="k2")
    local = await config.save_provider(db, admin, "Ollama", ProviderKind.OLLAMA,
                                       "http://localhost:11434/v1", "qwen")
    return groq, hf, local


async def test_provider_crud_and_ordering(db, admin, keys):
    groq, hf, local = await _setup(db, admin, keys)
    rows = await config.list_providers(db)
    assert [p.name for p in rows] == ["Groq", "HF", "Ollama"]
    assert rows[0].has_key and not rows[2].needs_key and rows[2].is_local
    await config.move_provider(db, admin, local, -1)
    await config.move_provider(db, admin, local, -1)
    assert [p.name for p in await config.list_providers(db)] == ["Ollama", "Groq", "HF"]
    await config.save_provider(db, admin, "Groq", ProviderKind.GROQ, "https://api.groq.com/openai/v1",
                               "llama", api_key="", provider_id=groq)
    assert not next(p for p in await config.list_providers(db) if p.id == groq).usable
    await config.delete_provider(db, admin, hf)
    assert ("ai", f"provider:{hf}") not in keys


async def test_validation(db, admin, keys):
    for url in ("ftp://x", "api.groq.com", "http://api.groq.com/v1"):
        with pytest.raises(ValidationError):
            await config.save_provider(db, admin, "x", ProviderKind.GROQ, url, "m")
    with pytest.raises(PermissionDenied):
        viewer = admin.__class__(99, "v", "", "viewer", frozenset())
        await config.save_provider(db, viewer, "x", ProviderKind.OLLAMA, "http://localhost:1/v1", "m")


async def test_fallback_on_rate_limit_then_cooldown(db, admin, keys):
    await _setup(db, admin, keys)
    calls = []

    def handler(request: httpx.Request):
        host = request.url.host
        calls.append(host)
        if host == "api.groq.com":
            return httpx.Response(429, headers={"retry-after": "30"})
        assert request.headers["authorization"] == "Bearer k2"
        body = json.loads(request.content)
        assert body["model"] == "m" and body["messages"][0]["content"] == "hi"
        return _answer("از HF")

    clock = Clock()
    gw = Gateway(db, httpx.MockTransport(handler), online_check=lambda: True, clock=clock)
    result = await gw.chat([{"role": "user", "content": "hi"}])
    assert result.content == "از HF" and result.provider == "HF"
    assert [a.outcome for a in gw.last_attempts] == ["rate_limited", "ok"]
    calls.clear()
    await gw.chat([{"role": "user", "content": "hi"}])
    assert calls == ["router.huggingface.co"]  # Groq skipped while cooling down
    clock.now += 31
    calls.clear()
    await gw.chat([{"role": "user", "content": "hi"}])
    assert calls[0] == "api.groq.com"


async def test_timeout_and_server_error_fall_through_to_local(db, admin, keys):
    await _setup(db, admin, keys)

    def handler(request: httpx.Request):
        if request.url.host == "api.groq.com":
            raise httpx.ReadTimeout("slow", request=request)
        if request.url.host == "router.huggingface.co":
            return httpx.Response(503)
        assert "authorization" not in request.headers  # local model: no key sent
        return _answer("محلی")

    gw = Gateway(db, httpx.MockTransport(handler), online_check=lambda: True)
    result = await gw.chat([{"role": "user", "content": "hi"}])
    assert result.provider == "Ollama"
    assert [a.outcome for a in gw.last_attempts] == ["timeout", "server_error", "ok"]


async def test_offline_uses_only_local(db, admin, keys):
    await _setup(db, admin, keys)
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return _answer()

    gw = Gateway(db, httpx.MockTransport(handler), online_check=lambda: False)
    await gw.chat([{"role": "user", "content": "hi"}])
    assert hosts == ["localhost"]


async def test_all_fail_or_none_configured(db, admin, keys):
    gw = Gateway(db, httpx.MockTransport(lambda r: _answer()), online_check=lambda: False)
    with pytest.raises(AIUnavailable, match="تنظیم نشده"):
        await gw.chat([{"role": "user", "content": "hi"}])
    await _setup(db, admin, keys)
    gw = Gateway(db, httpx.MockTransport(lambda r: httpx.Response(429)),
                 online_check=lambda: True, clock=Clock())
    with pytest.raises(AIUnavailable):
        await gw.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(AIUnavailable, match="محدود"):  # everything cooling down
        await gw.chat([{"role": "user", "content": "hi"}])
    assert DEFAULT_COOLDOWN > 0


async def test_tools_passed_and_tool_calls_returned(db, admin, keys):
    await config.save_provider(db, admin, "Ollama", ProviderKind.OLLAMA, "http://localhost:11434/v1", "q")
    tool_call = {"id": "c1", "type": "function",
                 "function": {"name": "search_items", "arguments": "{\"query\": \"دریل\"}"}}

    def handler(request):
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "search_items"
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": None, "tool_calls": [tool_call]}}]})

    gw = Gateway(db, httpx.MockTransport(handler), online_check=lambda: True)
    result = await gw.chat([{"role": "user", "content": "x"}],
                           tools=[{"type": "function", "function": {"name": "search_items"}}])
    assert result.tool_calls == [tool_call] and result.content == ""


async def test_transcribe_and_test_provider(db, admin, keys):
    pid = await config.save_provider(db, admin, "Groq", ProviderKind.GROQ,
                                      "https://api.groq.com/openai/v1", "llama",
                                      stt_model="whisper-large-v3", api_key="k")

    def handler(request):
        if request.url.path.endswith("/audio/transcriptions"):
            assert b"whisper-large-v3" in request.content
            return httpx.Response(200, json={"text": " پنج عدد دریل "})
        return httpx.Response(401)

    gw = Gateway(db, httpx.MockTransport(handler), online_check=lambda: True)
    assert await gw.transcribe(b"RIFF....") == "پنج عدد دریل"
    provider = next(p for p in await config.list_providers(db) if p.id == pid)
    ok, message, _ = await gw.test_provider(provider)
    assert not ok and "کلید" in message


async def test_settings_ai_tab(qtbot, themes, db, admin, keys):
    from caspian.core.settings import Settings
    from caspian.db.database import DbConfig
    from caspian.ui.app_context import AppContext
    from caspian.ui.settings_page import ProviderDialog, SettingsPage
    from helpers import settle

    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    ctx.ai = Gateway(db, httpx.MockTransport(lambda r: _answer()), online_check=lambda: False)
    page = SettingsPage(ctx)
    qtbot.addWidget(page)
    assert page.tabs.isTabVisible(page.ai_index)

    dlg = ProviderDialog(ctx)
    qtbot.addWidget(dlg)
    dlg.kind.setCurrentIndex(dlg.kind.findData(ProviderKind.GROQ))
    assert dlg.base_url.text() == "https://api.groq.com/openai/v1"
    assert dlg.stt_model.text() == "whisper-large-v3"
    dlg.key.setText("gsk_test")
    dlg.submit_button.click()
    await settle(dlg)
    [provider] = await config.list_providers(db)
    assert provider.has_key and keys[("ai", f"provider:{provider.id}")] == "gsk_test"

    await page.ai.refresh()
    assert page.ai.table.rowCount() == 1
    assert "اینترنت: قطع" in page.ai.status.text()


# ----- #16: clearer errors, new presets, model lists -----


async def _one(db, admin, kind=ProviderKind.GEMINI, key="sk-SECRET-123"):
    preset = config.PRESETS[kind]
    pid = await config.save_provider(db, admin, preset.label, kind, preset.base_url or "https://x.example/v1",
                                     preset.model or "m", api_key=key)
    return next(p for p in await config.list_providers(db) if p.id == pid)


@pytest.mark.parametrize(("status", "expected"), [(401, "کلید API نامعتبر"), (403, "VPN")])
async def test_401_and_403_are_reported_separately_with_server_text(db, admin, keys, status, expected):
    """Iranian IPs get 403 (region blocked) from Groq/Gemini/OpenAI: that is not a bad key (#16)."""
    provider = await _one(db, admin)

    def handler(request):
        return httpx.Response(status, json={"error": {"message": "User location is not supported "
                                                                  "(key sk-SECRET-123)"}})

    gw = Gateway(db, httpx.MockTransport(handler), online_check=lambda: True)
    ok, message, _ = await gw.test_provider(provider)
    assert not ok and expected in message
    assert "User location is not supported" in message  # the server's own explanation
    assert "sk-SECRET-123" not in message  # never echo the key
    other = "VPN" if status == 401 else "کلید API نامعتبر"
    assert other not in message


def test_presets_cover_the_requested_free_providers():
    kinds = {k.value for k in config.PRESETS}
    assert {"GEMINI", "OPENROUTER", "CEREBRAS", "MISTRAL", "CUSTOM"} <= kinds
    assert config.PRESETS[ProviderKind.GEMINI].base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert config.PRESETS[ProviderKind.OPENROUTER].base_url == "https://openrouter.ai/api/v1"
    assert config.PRESETS[ProviderKind.CEREBRAS].base_url == "https://api.cerebras.ai/v1"
    assert config.PRESETS[ProviderKind.MISTRAL].base_url == "https://api.mistral.ai/v1"
    assert all(len(k.value) <= 20 for k in ProviderKind)  # VARCHAR(20) column
    assert not config.PRESETS[ProviderKind.CUSTOM].needs_key  # key optional for self-hosted servers


async def test_every_preset_can_be_saved_and_listed(db, admin, keys):
    """The provider list must not come back empty after saving (reported in QA)."""
    for kind in config.PRESETS:
        await _one(db, admin, kind)
    assert {p.kind for p in await config.list_providers(db)} == set(config.PRESETS)


async def test_openrouter_sends_attribution_headers(db, admin, keys):
    provider = await _one(db, admin, ProviderKind.OPENROUTER)
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return _answer()

    gw = Gateway(db, httpx.MockTransport(handler), online_check=lambda: True)
    assert (await gw.test_provider(provider))[0]
    assert seen["x-title"] == "Caspian Warehouse" and seen["http-referer"].startswith("https://")


async def test_list_models_and_free_filter(db, admin, keys):
    def handler(request):
        assert request.url.path.endswith("/models")
        assert request.headers["authorization"] == "Bearer typed-key"
        return httpx.Response(200, json={"data": [
            {"id": "openrouter/free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "meta-llama/llama-3.3-70b-instruct:free"},
            {"id": "openai/gpt-5", "pricing": {"prompt": "0.000002", "completion": "0.00001"}},
            {"id": "models/gemini-3.8-flash"},
        ]})

    gw = Gateway(db, httpx.MockTransport(handler), online_check=lambda: True)
    models = await gw.list_models(ProviderKind.OPENROUTER, "https://openrouter.ai/api/v1", "typed-key")
    assert [(m.id, m.free) for m in models] == [
        ("gemini-3.8-flash", False), ("meta-llama/llama-3.3-70b-instruct:free", True),
        ("openai/gpt-5", False), ("openrouter/free", True)]


async def test_list_models_errors_are_explained(db, admin, keys):
    gw = Gateway(db, httpx.MockTransport(lambda r: httpx.Response(403, text="blocked")),
                 online_check=lambda: True)
    with pytest.raises(AIUnavailable, match="VPN"):
        await gw.list_models(ProviderKind.GROQ, "https://api.groq.com/openai/v1", "k")


async def test_provider_dialog_fetches_models_with_free_filter(qtbot, themes, db, admin, keys):
    from caspian.core.settings import Settings
    from caspian.db.database import DbConfig
    from caspian.ui.app_context import AppContext
    from caspian.ui.settings_page import ProviderDialog
    from helpers import wait_until

    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "openrouter/free"}, {"id": "x/paid"},
                                                  {"id": "y/model:free"}]})

    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    ctx.ai = Gateway(db, httpx.MockTransport(handler), online_check=lambda: True)
    dlg = ProviderDialog(ctx)
    qtbot.addWidget(dlg)
    assert dlg.kind.currentData() == ProviderKind.GEMINI  # free-tier providers first
    dlg.kind.setCurrentIndex(dlg.kind.findData(ProviderKind.OPENROUTER))
    assert dlg.model.currentText() == "openrouter/free" and not dlg.free_only.isHidden()
    dlg.key.setText("sk-or")
    await dlg.on_fetch_models()
    assert await wait_until(lambda: dlg.model.count() == 2)  # only the free ones
    dlg.free_only.setChecked(False)
    assert dlg.model.count() == 3
    dlg.model.setEditText("y/model:free")
    dlg.submit_button.click()
    from helpers import settle

    await settle(dlg)
    [provider] = await config.list_providers(db)
    assert provider.kind is ProviderKind.OPENROUTER and provider.model == "y/model:free"
