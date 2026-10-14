"""Синтез речи: XTTS-v2 (Coqui) с голосом и эмоциями по референсам персонажа.

Референсы лежат в characters/<персонаж>/voice/<эмоция>/*.wav. При запуске для каждой
эмоции один раз считается условие голоса (GPT-латент + эмбеддинг диктора), дальше
синтез только подставляет нужное.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Iterator

import numpy as np
import soundfile as sf
import torch

from companion.config import TTSConfig
from companion.emotions import DEFAULT_EMOTION, EMOTIONS

log = logging.getLogger(__name__)


def _load_audio(audiopath, sampling_rate):
    """Замена TTS.tts.models.xtts.load_audio: torchaudio в новых версиях читает wav
    через torchcodec, которому нужен системный FFmpeg. soundfile + soxr обходятся без него."""
    import soxr

    audio, sr = sf.read(audiopath, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if sr != sampling_rate:
        audio = soxr.resample(audio, sr, sampling_rate)
    return torch.from_numpy(np.clip(audio, -1, 1)).unsqueeze(0)


FIRST_MIN = 15  # первую фразу по запятой режем, только если до запятой не меньше стольких знаков


def split_text(text: str, limit: int, first_limit: int | None = None) -> list[str]:
    """Режет текст на куски не длиннее limit символов: по предложениям, длинное предложение —
    по запятой или пробелу. Своя разбивка вместо встроенной в XTTS: та требует spacy.
    first_limit — первый кусок короче (синтез — примерно 0,3 с на 10 знаков, а звучать фраза начинает только
    синтезированной целиком): к нему не подклеиваем соседние предложения, длинное режем по запятой или тире."""
    pieces: list[str] = []
    for sentence in re.split(r"(?<=[.!?…])\s+", text.strip()):
        while len(sentence) > limit:
            cut = max(sentence.rfind(",", 0, limit), sentence.rfind(" ", 0, limit))
            if cut <= 0:
                cut = limit - 1
            pieces.append(sentence[:cut + 1].strip())
            sentence = sentence[cut + 1:].strip()
        if sentence:
            pieces.append(sentence)
    if first_limit and pieces and len(pieces[0]) > first_limit:
        head = pieces[0]
        cut = max(head.rfind(",", 0, first_limit), head.rfind(" —", 0, first_limit))
        if cut >= FIRST_MIN:
            pieces[0:1] = [head[:cut + 1].strip(), head[cut + 1:].strip()]
    merged: list[str] = []  # короткие соседние предложения — вместе, пока влезают
    for piece in pieces:
        room = first_limit if first_limit and len(merged) == 1 else limit
        if merged and len(merged[-1]) + 1 + len(piece) <= room:
            merged[-1] += " " + piece
        else:
            merged.append(piece)
    return merged


class XttsTTS:
    def __init__(self, cfg: TTSConfig, models_path: Path, voice_dir: Path):
        import TTS.tts.models.xtts as xtts_module
        from TTS.tts.configs.xtts_config import XttsConfig
        from TTS.tts.models.xtts import Xtts

        xtts_module.load_audio = _load_audio

        model_dir = models_path / "xtts" / cfg.model
        if not (model_dir / "model.pth").is_file():
            raise FileNotFoundError(
                f"Нет модели XTTS в {model_dir}. Запусти: python tools/download_models.py --only xtts"
            )
        self.cfg = cfg
        t = time.perf_counter()
        config = XttsConfig()
        config.load_json(str(model_dir / "config.json"))
        self.model = Xtts.init_from_config(config)
        self.model.load_checkpoint(config, checkpoint_dir=str(model_dir), use_deepspeed=False)
        self.model.to(cfg.device).eval()
        self.sample_rate = config.audio.output_sample_rate
        self.char_limit = self.model.tokenizer.char_limits.get(cfg.language, 200)

        self.presets: dict[str, tuple[torch.Tensor, torch.Tensor]] = {}
        for emotion in EMOTIONS:
            refs = sorted((voice_dir / emotion).glob("*.wav"))
            if refs:
                self.presets[emotion] = self.model.get_conditioning_latents(audio_path=[str(p) for p in refs])
        if DEFAULT_EMOTION not in self.presets:
            raise FileNotFoundError(
                f"Нет референсов {voice_dir / DEFAULT_EMOTION}/*.wav. Собери: python tools/make_voice_refs.py"
            )
        missing = [e for e in EMOTIONS if e not in self.presets]
        if missing:
            log.warning("Нет референсов для эмоций %s — будет %s", missing, DEFAULT_EMOTION)

        # Прогрев: первая генерация на GPU заметно медленнее.
        self.synthesize("Привет.", DEFAULT_EMOTION)
        log.info("XTTS '%s' загружен на %s за %.1f с, эмоции: %s",
                 cfg.model, cfg.device, time.perf_counter() - t, ", ".join(self.presets))

    def _preset(self, emotion: str | None) -> tuple[torch.Tensor, torch.Tensor]:
        return self.presets.get(emotion or DEFAULT_EMOTION, self.presets[DEFAULT_EMOTION])

    def _params(self) -> dict:
        return dict(temperature=self.cfg.temperature, speed=self.cfg.speed, enable_text_splitting=False)

    @torch.inference_mode()
    def synthesize(self, text: str, emotion: str | None = None) -> tuple[np.ndarray, int]:
        """Возвращает float32 моно и частоту дискретизации."""
        t = time.perf_counter()
        gpt_latent, speaker = self._preset(emotion)
        audio = np.concatenate([
            np.asarray(self.model.inference(piece, self.cfg.language, gpt_latent, speaker, **self._params())["wav"],
                       dtype=np.float32)
            for piece in split_text(text, self.char_limit)])
        log.info("TTS %.2f с: %.1f с аудио [%s]", time.perf_counter() - t, len(audio) / self.sample_rate,
                 emotion or DEFAULT_EMOTION)
        return audio, self.sample_rate

    @torch.inference_mode()
    def stream(self, text: str, emotion: str | None = None) -> Iterator[np.ndarray]:
        """Отдаёт аудио кусками по мере генерации — воспроизведение начинается раньше."""
        gpt_latent, speaker = self._preset(emotion)
        for piece in split_text(text, self.char_limit):
            for chunk in self.model.inference_stream(piece, self.cfg.language, gpt_latent, speaker,
                                                     stream_chunk_size=self.cfg.stream_chunk_size,
                                                     **self._params()):
                yield chunk.detach().float().cpu().numpy().reshape(-1)
