"""Проба тегов эмоций ElevenLabs v3 Conversational (через OpenRouter) на русской фразе и нескольких голосах.

Одна фраза × теги × голоса → recordings/eleven_tags/, страница прослушивания tags.html. Whisper проверяет,
не прочитала ли модель тег вслух (латиница в распознанном тексте).

    python tools/tts_eleven_tags.py
    python tools/tts_eleven_tags.py --voices liam will   # свои голоса
"""

import argparse
import html
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion.config import ROOT, load_config  # noqa: E402

API = "https://openrouter.ai/api/v1/audio/speech"
MODEL = "elevenlabs/eleven-v3-conversational"
PRICE_PER_CHAR = 0.00002
OUT_DIR = ROOT / "recordings" / "eleven_tags"

FIRST = "Владос, ты это слышал?"
REST = "Кажется, там кто-то есть."

# (метка, наша эмоция или назначение, тег в начале, тег посреди фразы)
TAGS = [
    ("без тега", "neutral", "", ""),
    ("[happily]", "happy", "[happily] ", ""),
    ("[excited]", "happy", "[excited] ", ""),
    ("[sad]", "sad", "[sad] ", ""),
    ("[angry]", "anger", "[angry] ", ""),
    ("[annoyed]", "anger", "[annoyed] ", ""),
    ("[worried]", "fear", "[worried] ", ""),
    ("[panicking]", "fear", "[panicking] ", ""),
    ("[surprised]", "surprise", "[surprised] ", ""),
    ("[curious]", "puzzled", "[curious] ", ""),
    ("[dismissive]", "disgust", "[dismissive] ", ""),
    ("[sarcastically]", "подколка", "[sarcastically] ", ""),
    ("[cheeky]", "подколка", "[cheeky] ", ""),
    ("[whispers]", "подземелье", "[whispers] ", ""),
    ("[shouts]", "бой", "[shouts] ", ""),
    ("… [laughs] …", "смех посреди", "", "[laughs] "),
    ("… [sighs] …", "вздох посреди", "", "[sighs] "),
]
VOICES = ["liam", "will", "charlie"]


def text_for(start: str, middle: str) -> str:
    return f"{start}{FIRST} {middle}{REST}"


def synth(session: requests.Session, voice: str, text: str) -> bytes:
    r = session.post(API, json={"model": MODEL, "voice": voice, "input": text, "response_format": "mp3"}, timeout=120)
    r.raise_for_status()
    return r.content


def transcribe(files: list[Path]) -> dict[str, str]:
    import torch  # noqa: F401 — подтягивает CUDA-библиотеки (cublas) для faster-whisper
    from faster_whisper import WhisperModel
    model = WhisperModel(str(load_config().models_path / "whisper" / "large-v3-turbo"),
                         device="cuda", compute_type="int8_float16")
    out = {}
    for f in files:
        segs, _ = model.transcribe(str(f), language="ru", beam_size=5)
        out[f.name] = " ".join(s.text.strip() for s in segs)
    return out


def page(rows: list[dict], voices: list[str], total: float) -> str:
    head = "".join(f"<th>{html.escape(v)}</th>" for v in voices)
    body = []
    for tag, purpose, _, _ in TAGS:
        cells = []
        for v in voices:
            r = next(x for x in rows if x["tag"] == tag and x["voice"] == v)
            warn = ' <b class="warn">тег вслух?</b>' if r["latin"] else ""
            cells.append(f'<td><audio controls preload="none" src="{html.escape(r["file"])}"></audio>'
                         f'<div class="heard">{html.escape(r["heard"])}{warn}</div></td>')
        body.append(f"<tr><th class='tag'>{html.escape(tag)}<br><small>{html.escape(purpose)}</small></th>{''.join(cells)}</tr>")
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Теги ElevenLabs</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{ --bg:#fafaf8; --fg:#1d1d1b; --mute:#6b6b66; --line:#e2e1dc; --warn:#b4471a; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#171716; --fg:#ecebe6; --mute:#9a9992; --line:#2f2e2b; --warn:#f08a5d; }} }}
body {{ background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; margin:0; padding:24px 16px; }}
main {{ max-width:1200px; margin:auto; }} h1 {{ font-size:22px; margin:0 0 6px; }} p {{ color:var(--mute); margin:4px 0; }}
.wrap {{ overflow-x:auto; margin-top:16px; }} table {{ border-collapse:collapse; width:100%; }}
th, td {{ border-bottom:1px solid var(--line); padding:8px 10px; text-align:left; vertical-align:top; }}
thead th {{ font-size:13px; }} th.tag {{ white-space:nowrap; font-family:ui-monospace, monospace; font-weight:600; }}
small {{ color:var(--mute); font-family:system-ui, sans-serif; font-weight:400; }}
audio {{ height:32px; width:230px; }} .heard {{ font-size:12px; color:var(--mute); margin-top:4px; max-width:260px; }}
.warn {{ color:var(--warn); }}
</style></head><body><main>
<h1>ElevenLabs v3 Conversational — теги эмоций</h1>
<p>Фраза: «{html.escape(FIRST)} {html.escape(REST)}» — тег в начале (или посреди, где «…»).</p>
<p>Под плеером — как разобрал Whisper; «тег вслух?» — в тексте латиница: модель могла прочитать тег. Вся проба ≈ ${total:.3f}.</p>
<div class="wrap"><table><thead><tr><th>Тег</th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>
</main></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--voices", nargs="+", default=VOICES)
    args = ap.parse_args()
    key = load_config().llm.api_key
    if not key:
        sys.exit("Нет llm.api_key в config.local.yaml")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows, chars = [], 0
    with requests.Session() as s:
        s.headers["Authorization"] = f"Bearer {key}"
        for i, (tag, purpose, start, middle) in enumerate(TAGS):
            text = text_for(start, middle)
            for v in args.voices:
                path = OUT_DIR / f"{i:02d}_{v}.mp3"
                try:
                    path.write_bytes(synth(s, v, text))
                except requests.HTTPError as e:
                    print(f"{tag:16} {v:8} ОШИБКА {e.response.status_code}: {e.response.text[:200]}")
                    continue
                chars += len(text)
                rows.append({"tag": tag, "purpose": purpose, "voice": v, "text": text, "file": path.name})
                print(f"{tag:16} {v:8} ok")
    heard = transcribe([OUT_DIR / r["file"] for r in rows])
    for r in rows:
        r["heard"] = heard[r["file"]]
        r["latin"] = any("a" <= ch.lower() <= "z" for ch in r["heard"])
    total = chars * PRICE_PER_CHAR
    (OUT_DIR / "results.json").write_text(json.dumps({"model": MODEL, "total": total, "rows": rows},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "tags.html").write_text(page(rows, args.voices, total), encoding="utf-8")
    flagged = [f'{r["tag"]}/{r["voice"]}: {r["heard"]}' for r in rows if r["latin"]]
    print(f"\n{len(rows)} записей, ≈ ${total:.3f}. Тег вслух (латиница): {len(flagged)}")
    for f in flagged:
        print("  ", f)
    print(f"Страница: {OUT_DIR / 'tags.html'}")


if __name__ == "__main__":
    main()
