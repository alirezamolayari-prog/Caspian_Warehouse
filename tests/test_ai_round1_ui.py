"""QA round 1, Phase 1 UI: voice confirm, model list, status, busy text import, setting."""

import asyncio
import io
import math
import struct
import wave

import httpx
import pytest

from caspian.core import recorder
from caspian.db.models import ProviderKind
from caspian.services import master
from caspian.services.ai import config
from caspian.ui import assistant_page
from caspian.ui.assistant_page import AssistantPage
from helpers import settle, wait_until
from test_assistant_ui import make_ctx


@pytest.fixture
def no_keys(monkeypatch):
    monkeypatch.setattr(config, "get_secret", lambda k, n: None)


def _wav(seconds: float, amplitude: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        n = int(16000 * seconds)
        w.writeframes(b"".join(struct.pack("<h", int(amplitude * math.sin(i / 8))) for i in range(n)))
    return buf.getvalue()


def test_silence_and_short_clips_are_detected():
    assert recorder.is_silent(_wav(2, 20))  # room noise
    assert recorder.is_silent(_wav(0.3, 8000))  # a click
    assert not recorder.is_silent(_wav(1.5, 6000))  # speech-like
    assert recorder.is_silent(b"not a wav")


class FakeRecorder:
    def __init__(self, audio):
        self.audio, self.recording = audio, False

    def start(self):
        self.recording = True

    def stop(self):
        self.recording = False
        return self.audio


async def _page(qtbot, ctx, audio, monkeypatch):
    monkeypatch.setattr(assistant_page, "has_microphone", lambda: True)
    page = AssistantPage(ctx)
    qtbot.addWidget(page)
    page._recorder = FakeRecorder(audio)
    return page


async def test_voice_transcript_is_put_in_the_box_not_sent(qtbot, themes, db, admin, no_keys, monkeypatch):
    """Whisper invented «از اینجا باید بردارید.» from silence and it was sent (#6)."""
    ctx = make_ctx(db, admin, themes, lambda r: httpx.Response(500))
    heard = []

    async def transcribe(audio):
        heard.append(audio)
        return "حواله بزن برای آقای مولایاری"

    ctx.ai.transcribe = transcribe
    page = await _page(qtbot, ctx, _wav(1.5, 6000), monkeypatch)
    sent = []
    monkeypatch.setattr(page, "send", lambda text: sent.append(text))
    await page.on_mic()  # start
    await page.on_mic()  # stop -> transcribe
    assert page.input.text() == "حواله بزن برای آقای مولایاری" and sent == []

    page.input.clear()
    page._recorder = FakeRecorder(_wav(2, 10))
    await page.on_mic()
    await page.on_mic()
    assert len(heard) == 1 and page.input.text() == "" and "شنیده نشد" in page.status.text()


async def test_status_says_configured_not_ready(qtbot, themes, db, admin, no_keys):
    await config.save_provider(db, admin, "Local", ProviderKind.OLLAMA, "http://localhost:11434/v1", "q")
    ctx = make_ctx(db, admin, themes, lambda r: httpx.Response(404))
    page = AssistantPage(ctx)
    qtbot.addWidget(page)
    await page.update_status()
    assert "تنظیم شده" in page.status.text() and "آماده" not in page.status.text()
    await page.send("سلام")
    assert await wait_until(lambda: "پاسخ نگرفت" in page.status.text())
    assert "Local" in page.transcript.toPlainText() and "404" in page.transcript.toPlainText()


async def test_fetching_models_keeps_the_selected_one(qtbot, themes, db, admin, no_keys):
    """«دریافت لیست مدل‌ها» replaced the model with the first in the list (allam-2-7b) (#7)."""
    from caspian.ui.settings_page import ProviderDialog

    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "allam-2-7b"}, {"id": "llama-3.3-70b-versatile"}]})

    ctx = make_ctx(db, admin, themes, handler)
    dlg = ProviderDialog(ctx)
    qtbot.addWidget(dlg)
    dlg.kind.setCurrentIndex(dlg.kind.findData(ProviderKind.GROQ))
    assert dlg.model.currentText() == "llama-3.3-70b-versatile"
    await dlg.on_fetch_models()
    assert dlg.model.currentText() == "llama-3.3-70b-versatile" and dlg.model.count() == 2
    dlg.model.setEditText("my-own-model")
    await dlg.on_fetch_models()
    assert dlg.model.currentText() == "my-own-model"
    assert "در فهرست این سرویس نیست" in dlg.status.text()


async def test_text_import_is_busy_then_falls_back_when_ai_hangs(qtbot, themes, db, admin, no_keys,
                                                                 monkeypatch):
    """A slow AI froze «از متن…» for ~20 s with no feedback (#8)."""
    from caspian.services.ai import assistant
    from caspian.ui.imports_page import TextImportDialog

    await config.save_provider(db, admin, "Local", ProviderKind.OLLAMA, "http://localhost:11434/v1", "q")
    monkeypatch.setattr(assistant, "AI_TOTAL_TIMEOUT", 0.3)
    ctx = make_ctx(db, admin, themes, lambda r: httpx.Response(500))

    async def slow_chat(*a, **k):
        await asyncio.sleep(10)

    ctx.ai.chat = slow_chat
    dlg = TextImportDialog(ctx, await master.list_warehouses(db), [])
    qtbot.addWidget(dlg)
    dlg.text.setPlainText("۵ عدد دریل")
    dlg.submit_button.click()
    assert await wait_until(lambda: not dlg.text.isEnabled())
    assert "در حال پردازش" in dlg.status.text()
    await settle(dlg)
    assert dlg.batch_id is not None and not dlg.used_ai


async def test_ai_post_setting_in_settings(qtbot, themes, db, admin, no_keys):
    from caspian.ui.settings_page import AITab

    ctx = make_ctx(db, admin, themes, lambda r: httpx.Response(500))
    tab = AITab(ctx)
    qtbot.addWidget(tab)
    await tab.refresh()
    assert tab.allow_post.isChecked()  # default on
    tab.allow_post.setChecked(False)
    for _ in range(100):
        if not await config.ai_may_post(db):
            break
        await asyncio.sleep(0.02)
    assert not await config.ai_may_post(db)


async def test_created_document_link_opens_it(qtbot, themes, db, admin, no_keys):
    from PySide6.QtCore import QUrl

    opened = []

    async def open_document(doc_id):
        opened.append(doc_id)

    page = AssistantPage(make_ctx(db, admin, themes, lambda r: httpx.Response(500)),
                         open_document=open_document)
    qtbot.addWidget(page)
    page.on_link(QUrl("doc:7"))
    assert await wait_until(lambda: opened == [7])

