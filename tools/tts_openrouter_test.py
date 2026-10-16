"""Проба TTS-моделей OpenRouter: одна фраза — разные модели, звук в recordings/openrouter_tts/.

Для каждой модели: первый байт и полный ответ (с), длина звука (с), цена запроса и цена 1000 таких реплик.
Эмоция задаётся так, как умеет модель: instructions, тег в тексте или никак. Модели, которые умеют
клонировать голос, получают референс голоса Гослинга (characters/gosling/voice/<эмоция>/).

Цена — по прайсу OpenRouter (Models API): статистика генераций (/generation) TTS-запросов не видит.
За знак текста — у большинства; Seed — за секунду звука; Gemini — за токены (звук ≈ 25 токенов/с), это оценка.
Итог сверяется с расходом ключа (/key) — он обновляется с задержкой, поэтому в конце ждём.

    python tools/tts_openrouter_test.py                 # все модели
    python tools/tts_openrouter_test.py --only gemini   # только те, где в метке или id есть «gemini»
    python tools/tts_openrouter_test.py --list          # список моделей пробы
"""

import argparse
import base64
import io
import json
import sys
import time
from pathlib import Path

import requests
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion.config import ROOT, load_config  # noqa: E402

API = "https://openrouter.ai/api/v1"
OUT_DIR = ROOT / "recordings" / "openrouter_tts"
VOICE_DIR = ROOT / "characters" / "gosling" / "voice"

TEXT = ("Владос, гляди — двемерские руины! Ну что, заглянем? "
        "Только чур, в этот раз ловушки первым обходишь ты.")
MOOD = "Весело и с азартом: молодой искатель приключений увидел что-то интересное и подначивает друга."
EMOTION = "happy"  # папка референсов для клонирования

PCM_RATE = 24000  # Gemini TTS отдаёт только сырой PCM: 24 кГц, 16 бит, моно
GEMINI_AUDIO_TOKENS_PER_SEC = 25

# (метка, модель, голос, как передать эмоцию: instructions | tag:<префикс> | none, клонирование)
MODELS = [
    ("gemini-lite", "google/gemini-3.8-flash-lite-tts", "Fenrir", "instructions", False),
    ("gemini", "google/gemini-3.8-flash-tts", "Fenrir", "instructions", False),
    ("mai-flash", "microsoft/mai-voice-2.1-flash", "ru-RU-Lev:MAI-Voice-2.1-Flash", "none", False),
    ("mai", "microsoft/mai-voice-2.1", "ru-RU-Lev:MAI-Voice-2.1", "none", False),
    ("grok", "x-ai/grok-voice-tts-1.0", "rex", "none", False),
    ("eleven-flash", "elevenlabs/eleven-flash-v2.5", "liam", "none", False),
    ("eleven-v3-conv", "elevenlabs/eleven-v3-conversational", "liam", "tag:[excited] ", False),
    ("eleven-v4-turbo", "elevenlabs/eleven-v4-turbo", "liam", "tag:[excited] ", False),
    ("fish-drama3", "fish-audio/drama-3", None, "tag:[весело, с азартом] ", False),
    ("fish-s2.1-clone", "fish-audio/s2.1-pro", None, "instructions", True),
    ("seed-clone", "bytedance-seed/seed-audio-1-0", None, "tag:@Audio1 ", True),
]


def prices() -> dict[str, dict]:
    data = requests.get(f"{API}/models", params={"output_modalities": "speech"}, timeout=30).json()["data"]
    return {m["id"]: {k: float(v or 0) for k, v in m["pricing"].items() if k in ("prompt", "completion")}
            for m in data}


def references(emotion: str, limit: int) -> list[dict]:
    refs = sorted((VOICE_DIR / emotion).glob("*.wav"))[:limit]
    return [{"type": "input_audio",
             "input_audio": {"data": "data:audio/wav;base64," + base64.b64encode(p.read_bytes()).decode()}}
            for p in refs]


def body(model: str, voice: str | None, emotion_mode: str, clone: bool) -> dict:
    text = TEXT
    req: dict = {"model": model, "response_format": "pcm" if model.startswith("google/") else "mp3"}
    if emotion_mode == "instructions":
        req["instructions"] = MOOD
    elif emotion_mode.startswith("tag:"):
        text = emotion_mode[4:] + text
    req["input"] = text
    if voice:
        req["voice"] = voice
    if clone:
        req["input_references"] = references(EMOTION, 1)
    return req


def estimate(price: dict, req: dict, audio_sec: float | None) -> float | None:
    p, c = price.get("prompt", 0), price.get("completion", 0)
    if req["model"].startswith("google/"):  # за токены: текст (≈3 знака на токен) + звук
        if audio_sec is None:
            return None
        text_tokens = (len(req["input"]) + len(req.get("instructions", ""))) / 3
        return p * text_tokens + c * audio_sec * GEMINI_AUDIO_TOKENS_PER_SEC
    if c and not p:  # за секунду звука
        return c * audio_sec if audio_sec is not None else None
    return p * len(req["input"])


def save(audio: bytes, model: str, path: Path) -> tuple[Path, float | None]:
    if model.startswith("google/"):
        path = path.with_suffix(".wav")
        with sf.SoundFile(path, "w", samplerate=PCM_RATE, channels=1, subtype="PCM_16") as f:
            f.buffer_write(audio[: len(audio) // 2 * 2], dtype="int16")
        return path, len(audio) / 2 / PCM_RATE
    path.write_bytes(audio)
    try:
        info = sf.info(io.BytesIO(audio))
        return path, info.frames / info.samplerate
    except Exception:
        return path, None


def run(client: requests.Session, n: int, label: str, model: str, voice, mode: str, clone: bool,
        price: dict) -> dict:
    req = body(model, voice, mode, clone)
    t0 = time.perf_counter()
    first = None
    chunks = []
    with client.post(f"{API}/audio/speech", json=req, stream=True, timeout=120) as r:
        if r.status_code != 200:
            return {"label": label, "model": model, "error": f"{r.status_code}: {r.text[:300]}"}
        for chunk in r.iter_content(chunk_size=4096):
            if first is None:
                first = time.perf_counter() - t0
            chunks.append(chunk)
    total = time.perf_counter() - t0
    path, sec = save(b"".join(chunks), model, OUT_DIR / f"{n:02d}_{label}.mp3")
    cost = estimate(price, req, sec)
    return {"label": label, "model": model, "voice": voice or ("клон: " + EMOTION if clone else "по умолчанию"),
            "emotion": mode.split(":")[0], "chars": len(req["input"]), "first_byte": round(first or total, 2),
            "total": round(total, 2), "audio_sec": round(sec, 1) if sec else None, "cost": cost,
            "per_1000": round(cost * 1000, 2) if cost is not None else None, "file": path.name}


def usage(client: requests.Session) -> float:
    return float(client.get(f"{API}/key", timeout=30).json()["data"]["usage"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="подстрока метки или id модели")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    models = [m for m in MODELS if not args.only or args.only in m[0] or args.only in m[1]]
    if args.list:
        for m in models:
            print(f"{m[0]:16} {m[1]}")
        return
    key = load_config().llm.api_key
    if not key:
        sys.exit("Нет llm.api_key в config.local.yaml")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    price = prices()
    print(f"Текст ({len(TEXT)} знаков): {TEXT}\n")
    results = []
    with requests.Session() as client:
        client.headers["Authorization"] = f"Bearer {key}"
        before = usage(client)
        for n, (label, model, voice, mode, clone) in enumerate(models, 1):
            res = run(client, n, label, model, voice, mode, clone, price.get(model, {}))
            results.append(res)
            if "error" in res:
                print(f"{label:16} ОШИБКА {res['error']}")
                continue
            sec = f"{res['audio_sec']:.1f}" if res["audio_sec"] else "?"
            cost = f"${res['cost']:.5f}" if res["cost"] is not None else "$?"
            print(f"{label:16} звук {sec:>4} с  первый байт {res['first_byte']:5.2f} с  всего {res['total']:5.2f} с  "
                  f"{cost}  (1000 реплик ≈ ${res['per_1000']})")
        estimated = sum(r.get("cost") or 0 for r in results)
        print(f"\nПо прайсу: ${estimated:.4f}. Ждём, пока обновится расход ключа…")
        spent = None
        for _ in range(20):
            time.sleep(15)
            spent = usage(client) - before
            if spent > 0:
                break
        print(f"Расход ключа за пробу: ${spent:.4f}" if spent else "Расход ключа пока не обновился")
    (OUT_DIR / "results.json").write_text(json.dumps(
        {"text": TEXT, "mood": MOOD, "estimated_total": estimated, "key_spent": spent, "results": results},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nФайлы: {OUT_DIR}")


if __name__ == "__main__":
    main()
