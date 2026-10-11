"""Компаньон целиком: микрофон -> Whisper -> LLM -> XTTS (голос + эмоция) -> динамики.

С echo: true в config.yaml LLM пропускается — распознанная фраза озвучивается обратно
(проверка голосового тракта).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np
import soxr

from companion.config import Config
from companion.emotions import DEFAULT_EMOTION
from companion.voice.audio_io import PushToTalkRecorder, play, play_stream
from companion.voice.stt import WHISPER_SR, SpeechToText
from companion.voice.xtts import XttsTTS

log = logging.getLogger(__name__)

EventSink = Callable[[dict], None]


@dataclass
class Speech:
    audio: np.ndarray
    sample_rate: int
    first_audio: float  # секунд от вызова до начала звука


class VoiceService:
    def __init__(self, cfg: Config, on_event: EventSink | None = None, use_llm: bool | None = None):
        self.cfg = cfg
        self.on_event = on_event or (lambda e: None)
        models = cfg.models_path

        self.stt = SpeechToText(cfg.stt, models)
        self.tts = XttsTTS(cfg.tts, models, cfg.character_path / "voice")

        use_llm = (not cfg.echo) if use_llm is None else use_llm
        self.llm = None
        if use_llm:
            from companion.llm import CompanionLLM
            self.llm = CompanionLLM(cfg.llm, cfg.character_path / "prompt.md")

        # Модели не потокобезопасны, а реплики не должны накладываться друг на друга.
        self._stt_lock = threading.Lock()
        self._tts_lock = threading.Lock()
        self._play_lock = threading.Lock()
        self.recorder: PushToTalkRecorder | None = None

    # ------------------------------------------------------------ API

    def transcribe(self, audio: np.ndarray, sample_rate: int = WHISPER_SR) -> str:
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sample_rate != WHISPER_SR:
            audio = soxr.resample(audio, sample_rate, WHISPER_SR)
        with self._stt_lock:
            return self.stt.transcribe(audio)

    def synthesize(self, text: str, emotion: str | None = None) -> tuple[np.ndarray, int]:
        """Аудио целиком."""
        with self._tts_lock:
            return self.tts.synthesize(text, emotion)

    def speak(self, text: str, emotion: str | None = None) -> Speech:
        """Озвучивает в динамики; с tts.stream — потоково, звук начинается до конца синтеза."""
        t = time.perf_counter()
        first: list[float] = []
        mark = lambda: first.append(time.perf_counter() - t)  # noqa: E731
        device = self.cfg.audio.output_device

        if self.cfg.tts.stream:
            with self._tts_lock, self._play_lock:
                audio = play_stream(self.tts.stream(text, emotion), self.tts.sample_rate, device, mark)
            sr = self.tts.sample_rate
        else:
            audio, sr = self.synthesize(text, emotion)
            with self._play_lock:
                mark()
                play(audio, sr, device)
        return Speech(audio, sr, first[0] if first else 0.0)

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        with self._play_lock:
            play(audio, sample_rate, self.cfg.audio.output_device)

    def respond(self, player_text: str, game_context: str = ""):
        """Ответ компаньона на реплику игрока (нужна LLM, т.е. echo: false)."""
        if self.llm is None:
            raise RuntimeError("LLM не подключена: в config.yaml echo: true")
        return self.llm.reply(player_text, game_context)

    # ------------------------------------------------------------ push-to-talk

    def start_push_to_talk(self) -> None:
        if not self.cfg.push_to_talk.enabled:
            return
        self.recorder = PushToTalkRecorder(self.cfg.push_to_talk, self.cfg.audio, self._on_utterance)
        self.recorder.start()

    def stop(self) -> None:
        if self.recorder:
            self.recorder.stop()

    def _on_utterance(self, audio: np.ndarray) -> None:
        try:
            t = time.perf_counter()
            text = self.transcribe(audio, self.cfg.audio.sample_rate)
            if not text:
                return
            self.on_event({"type": "player_speech", "text": text})
            if self.llm is None:
                reply_text, emotion = text, DEFAULT_EMOTION
            else:
                reply = self.respond(text)
                reply_text, emotion = reply.text, reply.emotion
                self.on_event({"type": "companion_reply", "text": reply_text, "emotion": emotion})
            before_tts = time.perf_counter() - t
            speech = self.speak(reply_text, emotion)
            log.info("от конца фразы до начала ответа: %.2f с", before_tts + speech.first_audio)
        except Exception:
            log.exception("Ошибка обработки фразы")
