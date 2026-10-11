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
import struct
import sys
import zlib
from pathlib import Path

import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_voices import read_bsa_index, read_file  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(r"D:\Games\The Elder Scrolls V Skyrim\Data")
PLUGINS = ["skyrim", "update", "dawnguard", "hearthfires", "dragonborn"]

EMOTIONS = ["neutral", "anger", "disgust", "fear", "sad", "happy", "surprise", "puzzled"]
FLAG_COMPRESSED = 0x00040000
NAME_RE = re.compile(r"^(?P<plugin>[^_]+)__.*_(?P<form>[0-9a-f]{8})_(?P<resp>\d+)$")


# ------------------------------------------------------------------ strings

def parse_strings(data: bytes, length_prefixed: bool) -> dict[int, str]:
    count, _ = struct.unpack_from("<II", data, 0)
    base = 8 + count * 8
    out = {}
    for i in range(count):
        sid, off = struct.unpack_from("<II", data, 8 + i * 8)
        pos = base + off
        if length_prefixed:
            pos += 4
        end = data.index(b"\0", pos)
        raw = data[pos:end]
        try:
            out[sid] = raw.decode("utf-8")
        except UnicodeDecodeError:
            out[sid] = raw.decode("cp1251")
    return out


def load_ilstrings(bsa: Path) -> dict[str, dict[int, str]]:
    tables = {}
    with open(bsa, "rb") as f:
        entries, flags = read_bsa_index(f)
        for e in entries:
            if e.folder == "strings" and e.name.endswith("_russian.ilstrings"):
                tables[e.name.split("_")[0]] = parse_strings(read_file(f, e, flags), True)
    return tables


# ------------------------------------------------------------------ plugin

def iter_records(data: bytes, start: int, end: int):
    """Обходит записи, раскрывая только группы диалогов (DIAL и их INFO)."""
    pos = start
    while pos < end:
        typ = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        if typ == b"GRUP":
            label = data[pos + 8:pos + 12]
            gtype = struct.unpack_from("<i", data, pos + 12)[0]
            if (gtype == 0 and label == b"DIAL") or gtype == 7:
                yield from iter_records(data, pos + 24, pos + size)
            pos += size
        else:
            yield typ, struct.unpack_from("<I", data, pos + 8)[0], \
                struct.unpack_from("<I", data, pos + 12)[0], data[pos + 24:pos + 24 + size]
            pos += 24 + size


def iter_subrecords(body: bytes):
    pos, big = 0, None
    while pos < len(body):
        typ = body[pos:pos + 4]
        size = struct.unpack_from("<H", body, pos + 4)[0]
        pos += 6
        if big is not None:
            size, big = big, None
        if typ == b"XXXX":
            big = struct.unpack_from("<I", body, pos)[0]
            pos += size
            continue
        yield typ, body[pos:pos + size]
        pos += size


def load_responses(plugin: Path) -> dict[tuple[int, int], tuple[str, int, int]]:
    """(formID & 0xFFFFFF, номер ответа) -> (эмоция, сила, id строки)."""
    data = plugin.read_bytes()
    out = {}
    header_size = 24 + struct.unpack_from("<I", data, 4)[0]
    for typ, flags, form, body in iter_records(data, header_size, len(data)):
        if typ != b"INFO":
            continue
        if flags & FLAG_COMPRESSED:
            body = zlib.decompress(body[4:])
        key = None
        for sub, payload in iter_subrecords(body):
            if sub == b"TRDT":
                emo, value = struct.unpack_from("<Ii", payload, 0)
                key = (form & 0xFFFFFF, payload[12])
                # у части ответов NAM1 нет (пустой текст) — сохраняем эмоцию и без него
                out[key] = (EMOTIONS[emo] if emo < len(EMOTIONS) else str(emo), value, 0)
            elif sub == b"NAM1" and key:
                emo, value, _ = out[key]
                out[key] = (emo, value, struct.unpack_from("<I", payload)[0])
                key = None
    return out


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
    strings = load_ilstrings(DATA / "Skyrim - Interface.bsa")
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
