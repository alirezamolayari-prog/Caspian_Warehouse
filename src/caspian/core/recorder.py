"""Microphone recording via the Windows MCI API (winmm) — no extra dependencies.

The recording is written to a temporary WAV file only because MCI can't save to memory;
the bytes are read back and the file is deleted immediately.
"""

import contextlib
import ctypes
import os
import sys
import tempfile
from pathlib import Path

ALIAS = "caspian_voice"


class RecorderError(Exception):
    pass


def _mci(command: str) -> str:
    if sys.platform != "win32":
        raise RecorderError("ضبط صدا فقط در ویندوز پشتیبانی می‌شود.")
    buffer = ctypes.create_unicode_buffer(256)
    code = ctypes.windll.winmm.mciSendStringW(command, buffer, 255, 0)
    if code != 0:
        raise RecorderError(f"MCI error {code}")
    return buffer.value


def has_microphone() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return ctypes.windll.winmm.waveInGetNumDevs() > 0
    except OSError:
        return False


class Recorder:
    def __init__(self) -> None:
        self.recording = False

    def start(self) -> None:
        if not has_microphone():
            raise RecorderError("میکروفونی پیدا نشد. از جعبه گفتگو استفاده کنید.")
        try:
            _mci(f"open new type waveaudio alias {ALIAS}")
            # 16 kHz mono 16-bit: what speech-to-text services expect, and small to upload.
            _mci(f"set {ALIAS} bitspersample 16 samplespersec 16000 channels 1 "
                 "bytespersec 32000 alignment 2")
            _mci(f"record {ALIAS}")
        except RecorderError as exc:
            self._close()
            raise RecorderError("شروع ضبط صدا ممکن نشد. میکروفون را بررسی کنید.") from exc
        self.recording = True

    def stop(self) -> bytes:
        if not self.recording:
            return b""
        self.recording = False
        fd, name = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        path = Path(name)
        try:
            _mci(f"stop {ALIAS}")
            _mci(f'save {ALIAS} "{path}"')
            return path.read_bytes()
        except RecorderError as exc:
            raise RecorderError("ذخیره صدای ضبط‌شده ممکن نشد.") from exc
        finally:
            self._close()
            path.unlink(missing_ok=True)

    def cancel(self) -> None:
        self.recording = False
        self._close()

    @staticmethod
    def _close() -> None:
        with contextlib.suppress(RecorderError):
            _mci(f"close {ALIAS}")
