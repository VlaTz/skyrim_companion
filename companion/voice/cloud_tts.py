"""Синтез речи в облаке: OpenAI-совместимый эндпоинт /audio/speech (OpenRouter), по умолчанию
ElevenLabs v3 Conversational. Тот же интерфейс, что у XttsTTS: synthesize, stream, sample_rate, char_limit.

Эмоция — тег в начале каждого куска текста ([happily], [angry], …; соответствие — tts.cloud.tags в config.yaml):
кусок синтезируется отдельно, и без тега следующая фраза прозвучала бы ровно. Звук — сырой PCM (16 бит, моно),
частота — из заголовка ответа (у ElevenLabs 24 кГц). Проба моделей и тегов — tools/tts_openrouter_test.py,
tools/tts_eleven_tags.py.
"""

from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator

import numpy as np
import requests

from companion.config import CloudTTSConfig
from companion.emotions import DEFAULT_EMOTION
from companion.voice.xtts import split_text

log = logging.getLogger(__name__)

DEFAULT_RATE = 24000
RETRIES = 2  # сеть или сервер споткнулись — повторяем запрос


class CloudTTS:
    def __init__(self, cfg: CloudTTSConfig, fallback_key: str = ""):
        key = cfg.api_key or (os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else "") or fallback_key
        if not key:
            raise RuntimeError("Нет ключа для облачного TTS: tts.cloud.api_key в config.local.yaml "
                               "(или ключ llm, если он от того же сервера)")
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {key}"
        self.char_limit = cfg.char_limit
        self.sample_rate = DEFAULT_RATE
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cloud-tts")
        t = time.perf_counter()
        self.synthesize("Привет.", DEFAULT_EMOTION)  # проверка ключа и частоты звука; заодно греет соединение
        log.info("Облачный TTS: %s, голос %s, %.2f с на пробную фразу", cfg.model, cfg.voice, time.perf_counter() - t)

    def _text(self, piece: str, emotion: str | None) -> str:
        tag = self.cfg.tags.get(emotion or DEFAULT_EMOTION, "")
        return f"{tag} {piece}" if tag else piece

    def _request(self, piece: str, emotion: str | None) -> np.ndarray:
        body = {"model": self.cfg.model, "voice": self.cfg.voice, "input": self._text(piece, emotion),
                "response_format": "pcm"}
        for attempt in range(RETRIES + 1):
            try:
                r = self.session.post(f"{self.cfg.base_url}/audio/speech", json=body, timeout=self.cfg.timeout)
                r.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt == RETRIES:
                    raise
                log.warning("Облачный TTS: %s — повтор", e)
        rate = re.search(r"rate=(\d+)", r.headers.get("content-type", ""))
        if rate:
            self.sample_rate = int(rate.group(1))
        pcm = r.content[: len(r.content) // 2 * 2]
        return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0

    def synthesize(self, text: str, emotion: str | None = None) -> tuple[np.ndarray, int]:
        """Возвращает float32 моно и частоту дискретизации."""
        t = time.perf_counter()
        audio = np.concatenate([self._request(piece, emotion) for piece in split_text(text, self.char_limit)])
        log.info("TTS %.2f с: %.1f с аудио [%s]", time.perf_counter() - t, len(audio) / self.sample_rate,
                 emotion or DEFAULT_EMOTION)
        return audio, self.sample_rate

    def stream(self, text: str, emotion: str | None = None) -> Iterator[np.ndarray]:
        """По фразе: пока звучит одна, следующая уже синтезируется."""
        pieces = split_text(text, self.char_limit, first_limit=self.cfg.first_chars)
        pending = self._pool.submit(self._request, pieces[0], emotion) if pieces else None
        for nxt in pieces[1:] + [None]:
            audio = pending.result()
            pending = self._pool.submit(self._request, nxt, emotion) if nxt else None
            yield audio
