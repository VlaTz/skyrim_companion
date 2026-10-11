"""Распознавание речи: faster-whisper (CTranslate2)."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np

from companion.config import STTConfig

log = logging.getLogger(__name__)

WHISPER_SR = 16000


class SpeechToText:
    def __init__(self, cfg: STTConfig, models_path: Path):
        if cfg.device == "cuda":
            # torch на Windows регистрирует свои cuBLAS/cuDNN DLL — они нужны и CTranslate2.
            import torch  # noqa: F401
        from faster_whisper import WhisperModel

        model_dir = models_path / "whisper" / cfg.model
        if not (model_dir / "model.bin").is_file():
            raise FileNotFoundError(
                f"Нет модели Whisper в {model_dir}. Запусти: python tools/download_models.py"
            )
        self.cfg = cfg
        t = time.perf_counter()
        self.model = WhisperModel(str(model_dir), device=cfg.device, compute_type=cfg.compute_type)
        # Прогрев: первый вызов на GPU в разы медленнее — пусть это случится при старте, а не на первой фразе.
        list(self.model.transcribe(np.zeros(WHISPER_SR, np.float32), language=cfg.language, vad_filter=False)[0])
        self.transcribe(np.zeros(WHISPER_SR, np.float32))  # подгружает VAD-модель
        log.info("Whisper '%s' загружен на %s за %.1f с", cfg.model, cfg.device, time.perf_counter() - t)

    def transcribe(self, audio: np.ndarray) -> str:
        """audio: float32 моно 16 кГц."""
        t = time.perf_counter()
        segments, _ = self.model.transcribe(
            audio.astype(np.float32),
            language=self.cfg.language,
            beam_size=self.cfg.beam_size,
            vad_filter=self.cfg.vad_filter,
            condition_on_previous_text=False,
            initial_prompt=self.cfg.initial_prompt or None,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        log.info("STT %.2f с: %r", time.perf_counter() - t, text)
        return text
