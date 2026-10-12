"""Сопоставляет извлечённые реплики с их текстом и эмоцией из файлов игры.

Имя файла реплики: <quest>_<topic>_<INFO formID>_<номер ответа>.fuz. По formID
находим запись INFO в плагине (Skyrim.esm, Dawnguard.esm ...), а в ней — ответ
с этим номером: TRDT (эмоция и её сила) и NAM1 (id строки в *_russian.ilstrings).

    python tools/extract_dialogue.py maleyoungeager

Вход:  datasets/<voice>/wav/*.wav (tools/extract_voices.py), Data/*.esm, Skyrim - Interface.bsa
Выход: datasets/<voice>/metadata.csv  — file;plugin;emotion;emotion_value;duration;text
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from companion.game.dialogue import PLUGINS, load_responses  # noqa: E402
from companion.game.gamefiles import load_strings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(r"D:\Games\The Elder Scrolls V Skyrim\Data")
NAME_RE = re.compile(r"^(?P<plugin>[^_]+)__.*_(?P<form>[0-9a-f]{8})_(?P<resp>\d+)$")


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("voice")
    args = ap.parse_args()

    wav_dir = ROOT / "datasets" / args.voice / "wav"
    files = sorted(wav_dir.glob("*.wav"))
    if not files:
        print(f"Нет реплик в {wav_dir}. Сначала: python tools/extract_voices.py {args.voice}")
        return 1

    print("Строки: Skyrim - Interface.bsa")
    strings = load_strings(DATA / "Skyrim - Interface.bsa", "ilstrings")
    responses = {}
    for name in PLUGINS:
        print(f"Диалоги: {name}.esm")
        responses[name] = load_responses(DATA / f"{name}.esm")

    rows, missing = [], 0
    for p in files:
        m = NAME_RE.match(p.stem)
        if not m:
            missing += 1
            continue
        plugin, key = m["plugin"], (int(m["form"], 16), int(m["resp"]))
        # Update.esm правит записи Skyrim.esm — его версия приоритетнее
        candidates = (["update", "skyrim"] if plugin == "skyrim" else [plugin])
        hit = next(((c, responses[c][key]) for c in candidates if key in responses[c]), None)
        if not hit:
            missing += 1
            continue
        src, (emo, value, sid) = hit
        text = strings.get(src, {}).get(sid) or strings.get(plugin, {}).get(sid, "")
        rows.append([p.name, plugin, emo, value, f"{sf.info(p).duration:.2f}", text.strip()])

    out = ROOT / "datasets" / args.voice / "metadata.csv"
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["file", "plugin", "emotion", "emotion_value", "duration", "text"])
        w.writerows(rows)

    from collections import Counter
    print(f"\nСопоставлено {len(rows)} из {len(files)} (не найдено {missing}), без текста: "
          f"{sum(1 for r in rows if not r[5])}")
    for emo, n in Counter(r[2] for r in rows).most_common():
        print(f"  {emo:9s} {n}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
