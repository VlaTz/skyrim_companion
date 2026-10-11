"""Собирает референсы голоса персонажа по эмоциям из реплик игры.

Для каждой эмоции берёт реплики с самой высокой силой эмоции (метки из
Skyrim.esm, см. tools/extract_dialogue.py) длиной 4–10 с, без боевых криков
и песен, и копирует их в characters/<персонаж>/voice/<эмоция>/.
Неудачный референс можно просто удалить или заменить своим wav.

    python tools/make_voice_refs.py --voice maleyoungeager --character gosling
    python tools/make_voice_refs.py --voice maleyoungeager --character gosling --refs 2
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion.config import ROOT  # noqa: E402
from companion.emotions import EMOTIONS  # noqa: E402

EXCLUDE = re.compile(r"combat|attack|bleedout|bardsong|dragon|flee|death|hit_|shout|yell|scream")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--voice", required=True, help="тип голоса из игры, напр. maleyoungeager")
    ap.add_argument("--character", required=True, help="папка в characters/")
    ap.add_argument("--refs", type=int, default=3, help="референсов на эмоцию")
    args = ap.parse_args()

    data = ROOT / "datasets" / args.voice
    rows = [r for r in csv.DictReader(open(data / "metadata.csv", encoding="utf-8"), delimiter=";")
            if r["text"] and 4 <= float(r["duration"]) <= 10 and not EXCLUDE.search(r["file"])]
    dst_root = ROOT / "characters" / args.character / "voice"

    for emotion in EMOTIONS:
        picked = sorted((r for r in rows if r["emotion"] == emotion),
                        key=lambda r: (-int(r["emotion_value"]), -float(r["duration"])))[:args.refs]
        dst = dst_root / emotion
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True)
        for r in picked:
            shutil.copy2(data / "wav" / r["file"], dst / r["file"])
        print(f"{emotion:9s} {len(picked)} реф.: " + " | ".join(r["text"][:40] for r in picked))
    print(f"-> {dst_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
