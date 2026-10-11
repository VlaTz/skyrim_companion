"""Достаёт реплики одного типа голоса из BSA Скайрима и конвертирует их в wav.

Реплики лежат в архиве как .fuz (заголовок + .lip + аудио .xwm). Из .fuz берём
только аудио, декодируем xWMA через PyAV и пишем моно wav с исходной частотой.

    python tools/extract_voices.py maleyoungeager
    python tools/extract_voices.py femaleeventoned --bsa "D:/.../Skyrim - Voices_ru0.bsa"
    python tools/extract_voices.py --list            # типы голосов и число реплик

Результат: datasets/<voice>/wav/<plugin>__<file>.wav
"""

from __future__ import annotations

import argparse
import io
import struct
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import av
import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BSA = Path(r"D:\Games\The Elder Scrolls V Skyrim\Data\Skyrim - Voices_ru0.bsa")

FLAG_COMPRESSED = 0x4
FLAG_EMBED_NAMES = 0x100
SIZE_COMPRESS_TOGGLE = 0x40000000


@dataclass
class Entry:
    folder: str
    name: str
    size: int
    offset: int
    compressed: bool


def read_bsa_index(f) -> tuple[list[Entry], int]:
    magic, ver, folder_off, flags, nfolders, nfiles, _, names_len, _ = struct.unpack(
        "<4sIIIIIIII", f.read(36))
    if magic != b"BSA\0" or ver not in (104, 105):
        raise ValueError(f"Не BSA Скайрима (magic={magic!r}, version={ver})")

    f.seek(folder_off)
    rec_size = 24 if ver == 105 else 16
    counts = [struct.unpack_from("<I", f.read(rec_size), 8)[0] for _ in range(nfolders)]

    raw: list[tuple[str, int, int]] = []
    for count in counts:
        n = f.read(1)[0]
        folder = f.read(n).rstrip(b"\0").decode("cp1252")
        for _ in range(count):
            _, size, offset = struct.unpack("<QII", f.read(16))
            raw.append((folder, size, offset))

    names = f.read(names_len).split(b"\0")
    default_compressed = bool(flags & FLAG_COMPRESSED)
    entries = [
        Entry(folder, names[i].decode("cp1252"), size & ~SIZE_COMPRESS_TOGGLE, offset,
              default_compressed != bool(size & SIZE_COMPRESS_TOGGLE))
        for i, (folder, size, offset) in enumerate(raw)
    ]
    return entries, flags


def read_file(f, e: Entry, flags: int) -> bytes:
    f.seek(e.offset)
    size = e.size
    if flags & FLAG_EMBED_NAMES:
        n = f.read(1)[0]
        f.seek(n, 1)
        size -= n + 1
    data = f.read(size)
    if e.compressed:
        import zlib
        data = zlib.decompress(data[4:])  # первые 4 байта — исходный размер
    return data


def fuz_audio(data: bytes) -> bytes:
    """.fuz = 'FUZE' + version + размер lip + lip + xwm. Возвращает xwm."""
    if data[:4] != b"FUZE":
        return data  # уже чистый .xwm/.wav
    lip_size = struct.unpack_from("<I", data, 8)[0]
    return data[12 + lip_size:]


def decode(audio: bytes) -> tuple[np.ndarray, int]:
    with av.open(io.BytesIO(audio)) as container:
        stream = container.streams.audio[0]
        frames = [fr.to_ndarray() for fr in container.decode(stream)]
        sr = stream.rate
    pcm = np.concatenate(frames, axis=1).astype(np.float32)  # (каналы, сэмплы) для planar
    if pcm.ndim == 2:
        pcm = pcm.mean(axis=0) if pcm.shape[0] <= 2 else pcm.reshape(-1)
    return pcm, sr


def voice_type(folder: str) -> str:
    return folder.rsplit("\\", 1)[-1].lower()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("voice", nargs="?", help="тип голоса, напр. maleyoungeager")
    ap.add_argument("--bsa", type=Path, default=DEFAULT_BSA)
    ap.add_argument("--out", type=Path, help="папка для wav (по умолчанию datasets/<voice>/wav)")
    ap.add_argument("--list", action="store_true", help="показать типы голосов и выйти")
    args = ap.parse_args()

    with open(args.bsa, "rb") as f:
        entries, flags = read_bsa_index(f)

        if args.list or not args.voice:
            counts = Counter(voice_type(e.folder) for e in entries if e.folder.startswith("sound\\voice\\"))
            for name, n in counts.most_common():
                print(f"{n:6d}  {name}")
            return 0

        voice = args.voice.lower()
        selected = [e for e in entries
                    if voice_type(e.folder) == voice and e.name.lower().endswith((".fuz", ".xwm", ".wav"))]
        if not selected:
            print(f"Голос '{voice}' не найден. Список: --list")
            return 1

        out = args.out or ROOT / "datasets" / voice / "wav"
        out.mkdir(parents=True, exist_ok=True)
        print(f"{len(selected)} реплик -> {out}")

        total, failed = 0.0, 0
        for i, e in enumerate(selected, 1):
            plugin = e.folder.split("\\")[2].rsplit(".", 1)[0]
            dst = out / f"{plugin}__{Path(e.name).stem}.wav"
            try:
                pcm, sr = decode(fuz_audio(read_file(f, e, flags)))
                sf.write(dst, pcm, sr, subtype="PCM_16")
                total += len(pcm) / sr
            except Exception as exc:  # битые/пустые реплики встречаются, не роняем весь прогон
                failed += 1
                print(f"  ! {e.name}: {exc}")
            if i % 250 == 0:
                print(f"  {i}/{len(selected)}")

    print(f"Готово: {len(selected) - failed} файлов, {total / 60:.1f} мин аудио, ошибок: {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
