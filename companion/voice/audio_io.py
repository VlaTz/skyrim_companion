"""Запись с микрофона по push-to-talk и воспроизведение."""

from __future__ import annotations

import logging
import threading
from typing import Callable, Iterable

import numpy as np
import sounddevice as sd
from pynput import keyboard

from companion.config import AudioConfig, PushToTalkConfig

log = logging.getLogger(__name__)


def resolve_device(device: int | str | None, kind: str) -> int | None:
    """Номер устройства или часть его имени -> номер для sounddevice."""
    if device is None or isinstance(device, int):
        return device
    for i, d in enumerate(sd.query_devices()):
        if device.lower() in d["name"].lower() and d[f"max_{kind}_channels"] > 0:
            return i
    raise ValueError(f"Аудиоустройство не найдено: {device!r}")


def parse_key(name: str) -> keyboard.Key | keyboard.KeyCode:
    name = name.strip().lower()
    if hasattr(keyboard.Key, name):
        return getattr(keyboard.Key, name)
    if len(name) == 1:
        return keyboard.KeyCode.from_char(name)
    raise ValueError(f"Неизвестная клавиша push-to-talk: {name!r}")


def play(audio: np.ndarray, sample_rate: int, device: int | str | None = None) -> None:
    """Блокирующее воспроизведение."""
    sd.play(audio, sample_rate, device=resolve_device(device, "output"))
    sd.wait()


def play_stream(chunks: Iterable[np.ndarray], sample_rate: int, device: int | str | None = None,
                on_first: Callable[[], None] | None = None) -> np.ndarray:
    """Играет куски по мере поступления (блокирует до конца), возвращает всё аудио целиком."""
    played: list[np.ndarray] = []
    with sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32", latency="high",
                         device=resolve_device(device, "output")) as out:
        for chunk in chunks:
            chunk = np.clip(np.asarray(chunk, np.float32).reshape(-1), -1, 1)
            if not played and on_first:
                on_first()
            out.write(chunk.reshape(-1, 1))
            played.append(chunk)
    # выход из with ждёт, пока доиграет буфер
    return np.concatenate(played) if played else np.zeros(0, np.float32)


class PushToTalkRecorder:
    """Пока клавиша зажата — пишет микрофон; при отпускании отдаёт запись в on_utterance."""

    def __init__(self, ptt: PushToTalkConfig, audio: AudioConfig,
                 on_utterance: Callable[[np.ndarray], None]):
        self.key = parse_key(ptt.key)
        self.min_samples = int(ptt.min_duration * audio.sample_rate)
        self.sample_rate = audio.sample_rate
        self.device = resolve_device(audio.input_device, "input")
        self.on_utterance = on_utterance

        self._chunks: list[np.ndarray] = []
        self._recording = False
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None
        self._listener: keyboard.Listener | None = None

    def start(self) -> None:
        self._stream = sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="float32",
            device=self.device, callback=self._on_audio,
        )
        self._stream.start()
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()
        log.info("Push-to-talk: держи %s и говори", self.key)

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
        if self._stream:
            self._stream.close()

    def _matches(self, key) -> bool:
        if isinstance(self.key, keyboard.KeyCode):
            return isinstance(key, keyboard.KeyCode) and (key.char or "").lower() == self.key.char
        return key == self.key

    def _on_audio(self, indata, frames, time_info, status) -> None:
        if self._recording:
            with self._lock:
                self._chunks.append(indata[:, 0].copy())

    def _on_press(self, key) -> None:
        # Windows шлёт повторные press, пока клавиша зажата — реагируем только на первый.
        if self._matches(key) and not self._recording:
            with self._lock:
                self._chunks = []
            self._recording = True
            log.info("● запись...")

    def _on_release(self, key) -> None:
        if not (self._matches(key) and self._recording):
            return
        self._recording = False
        with self._lock:
            audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)
            self._chunks = []
        log.info("■ стоп, %.1f с", len(audio) / self.sample_rate)
        if len(audio) < self.min_samples:
            log.info("слишком коротко, пропуск")
            return
        # Обработка в отдельном потоке, чтобы не блокировать перехват клавиатуры.
        threading.Thread(target=self.on_utterance, args=(audio,), daemon=True).start()
