"""Проверка XTTS-v2 без обучения: голос и эмоции Эрика по референсам из игры.

Для каждой эмоции берёт несколько реплик с самой высокой силой эмоции из
datasets/<voice>/metadata.csv (tools/extract_dialogue.py), строит по ним
условие голоса и озвучивает тестовые фразы. Замеряет время и видеопамять.

    python tools/xtts_zeroshot.py
    python tools/xtts_zeroshot.py --emotions neutral anger happy --refs 2

Результат: recordings/xtts_zeroshot/<emotion>.wav и refs_<emotion>.wav (склейка референсов)
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

ROOT = Path(__file__).resolve().parent.parent
EXCLUDE = re.compile(r"combat|attack|bleedout|bardsong|dragon|flee|death|hit_|shout|yell|scream")

PHRASES = {
    "neutral": "Дорога до Вайтрана займёт пару дней. Можем заночевать в Ривервуде, там есть таверна.",
    "anger": "Да как ты смеешь! Ещё одно слово, и я за себя не ручаюсь!",
    "fear": "Ты это слышал? Там, в темноте, что-то шевелится... Может, лучше повернём назад?",
    "happy": "Ха! Вот это была драка! Отец ни за что не поверит, когда я ему расскажу!",
    "sad": "Знаешь... иногда я скучаю по дому. По отцу, по нашей старой ферме.",
    "surprise": "Что? Ты серьёзно? Вот это да, никогда бы не подумал!",
    "puzzled": "Хм, странно. Я был уверен, что пещера где-то здесь. Может, мы свернули не туда?",
    "disgust": "Фу, ну и вонь. Похоже, тут давно никто не убирал трупы скиверов.",
}


def load_audio_sf(audiopath, sampling_rate):
    """Замена TTS.tts.models.xtts.load_audio: torchaudio в новых версиях читает wav
    через torchcodec, которому нужен системный FFmpeg. soundfile + soxr обходятся без него."""
    import soxr
    audio, sr = sf.read(audiopath, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if sr != sampling_rate:
        audio = soxr.resample(audio, sr, sampling_rate)
    return torch.from_numpy(np.clip(audio, -1, 1)).unsqueeze(0)


def pick_refs(metadata: Path, wav_dir: Path, emotion: str, n: int) -> list[Path]:
    rows = [r for r in csv.DictReader(open(metadata, encoding="utf-8"), delimiter=";")
            if r["emotion"] == emotion and r["text"] and 4 <= float(r["duration"]) <= 10
            and not EXCLUDE.search(r["file"])]
    rows.sort(key=lambda r: (-int(r["emotion_value"]), -float(r["duration"])))
    return [wav_dir / r["file"] for r in rows[:n]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--voice", default="maleyoungeager")
    ap.add_argument("--model", default="XTTS-v2", help="папка в models/xtts/")
    ap.add_argument("--emotions", nargs="*", default=list(PHRASES))
    ap.add_argument("--refs", type=int, default=3, help="референсов на эмоцию")
    args = ap.parse_args()

    import TTS.tts.models.xtts as xtts_module
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts

    xtts_module.load_audio = load_audio_sf

    model_dir = ROOT / "models" / "xtts" / args.model
    data_dir = ROOT / "datasets" / args.voice
    out = ROOT / "recordings" / "xtts_zeroshot"
    out.mkdir(parents=True, exist_ok=True)

    t = time.perf_counter()
    config = XttsConfig()
    config.load_json(str(model_dir / "config.json"))
    model = Xtts.init_from_config(config)
    model.load_checkpoint(config, checkpoint_dir=str(model_dir), use_deepspeed=False)
    model.cuda().eval()
    torch.cuda.synchronize()
    print(f"Загрузка модели: {time.perf_counter() - t:.1f} с, VRAM {torch.cuda.memory_allocated() / 2**30:.2f} ГБ")
    torch.cuda.reset_peak_memory_stats()

    sr = config.audio.output_sample_rate
    for emotion in args.emotions:
        refs = pick_refs(data_dir / "metadata.csv", data_dir / "wav", emotion, args.refs)
        if not refs:
            print(f"{emotion}: нет подходящих референсов")
            continue
        ref_audio = [sf.read(p, dtype="float32")[0] for p in refs]
        sf.write(out / f"refs_{emotion}.wav", np.concatenate(ref_audio), sf.info(refs[0]).samplerate)

        t = time.perf_counter()
        gpt_latent, speaker_emb = model.get_conditioning_latents(audio_path=[str(p) for p in refs])
        t_cond = time.perf_counter() - t

        t = time.perf_counter()
        result = model.inference(PHRASES[emotion], "ru", gpt_latent, speaker_emb, temperature=0.7)
        torch.cuda.synchronize()
        t_gen = time.perf_counter() - t
        wav = np.asarray(result["wav"], dtype=np.float32)
        sf.write(out / f"{emotion}.wav", wav, sr)
        print(f"{emotion:9s} условие {t_cond:.2f} с | синтез {t_gen:.2f} с на {len(wav) / sr:.1f} с аудио "
              f"(RTF {t_gen / (len(wav) / sr):.2f})")

    print(f"Пик VRAM при синтезе: {torch.cuda.max_memory_allocated() / 2**30:.2f} ГБ")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
