"""Реплики NPC из файлов игры — сырьё для базы шуток Гослинга (characters/gosling/jokes.yaml).

У каждой реплики (запись INFO) — кто её говорит, по условиям записи (CTDA): именной NPC (GetIsID), фракция
(GetInFaction — стража, Соратники), тип голоса (GetIsVoiceType); и тип темы (SNAM: HELO — приветствие, IDLE —
болтовня, ...). Тексты — русские строки из Skyrim - Interface.bsa. Только чтение файлов игры.

    python tools/extract_quotes.py                       # все реплики -> datasets/game_quotes.tsv
    python tools/extract_quotes.py --grep "колено|рулет"  # только с этими словами (регулярное выражение)
    python tools/extract_quotes.py --speaker "М'айк|Шеогорат|Цицерон"

Выход: TSV — plugin;info;topic_type;speaker;factions;voice;emotion;text (по реплике на строку).
"""

from __future__ import annotations

import argparse
import csv
import re
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from companion.config import ROOT, load_config  # noqa: E402
from companion.game.dialogue import EMOTIONS, FLAG_COMPRESSED, PLUGINS, iter_subrecords  # noqa: E402
from companion.game.gamefiles import load_strings  # noqa: E402

GET_IN_FACTION, GET_IS_ID, GET_IS_VOICE_TYPE = 71, 72, 426  # номера функций условий CTDA
TOP_GROUPS = {b"NPC_", b"FACT", b"VTYP", b"DIAL"}  # что читаем; остальное (ячейки, миры) пропускаем
TAG = re.compile(r"<[^>]+>")


def read_plugin(path: Path) -> dict:
    """NPC, фракции, типы голоса (имя/EditorID) и реплики с условиями из одного плагина."""
    data = path.read_bytes()
    out = {"masters": [], "names": {}, "edids": {}, "dials": {}, "infos": []}
    header_size = 24 + struct.unpack_from("<I", data, 4)[0]
    for sub, payload in iter_subrecords(data[24:header_size]):
        if sub == b"MAST":
            out["masters"].append(payload.rstrip(b"\0").decode("ascii").lower().removesuffix(".esm"))

    def walk(start: int, end: int, topic: int | None) -> None:
        pos = start
        while pos < end:
            typ = data[pos:pos + 4]
            size = struct.unpack_from("<I", data, pos + 4)[0]
            if typ == b"GRUP":
                label, gtype = data[pos + 8:pos + 12], struct.unpack_from("<i", data, pos + 12)[0]
                if (gtype == 0 and label in TOP_GROUPS) or gtype == 7:
                    walk(pos + 24, pos + size, struct.unpack_from("<I", label)[0] if gtype == 7 else None)
                pos += size
                continue
            flags, form = struct.unpack_from("<II", data, pos + 8)
            body = data[pos + 24:pos + 24 + size]
            pos += 24 + size
            if flags & FLAG_COMPRESSED:
                body = zlib.decompress(body[4:])
            subs = list(iter_subrecords(body))
            if typ in (b"NPC_", b"FACT", b"VTYP"):
                for sub, payload in subs:
                    if sub == b"FULL" and len(payload) >= 4:
                        out["names"][form] = struct.unpack_from("<I", payload)[0]
                    elif sub == b"EDID":
                        out["edids"][form] = payload.rstrip(b"\0").decode("cp1252", "replace")
            elif typ == b"DIAL":
                snam = next((p for s, p in subs if s == b"SNAM"), b"")
                out["dials"][form] = snam.decode("ascii", "replace")
            elif typ == b"INFO" and topic is not None:
                responses, conditions, number = [], [], None
                for sub, payload in subs:
                    if sub == b"TRDT":
                        emotion, number = struct.unpack_from("<I", payload)[0], payload[12]
                    elif sub == b"NAM1" and number is not None:
                        responses.append((number, emotion, struct.unpack_from("<I", payload)[0]))
                        number = None
                    elif sub == b"CTDA" and len(payload) >= 16:
                        # op (флаги), значение, номер функции, параметр 1 (форма); «!= 1» и т.п. — не «это он»
                        op, value = payload[0] >> 5, struct.unpack_from("<f", payload, 4)[0]
                        func, param = struct.unpack_from("<H", payload, 8)[0], struct.unpack_from("<I", payload, 12)[0]
                        if op == 0 and value == 1.0:
                            conditions.append((func, param))
                out["infos"].append((form, topic, responses, conditions))
    walk(header_size, len(data), None)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grep", help="регулярное выражение по тексту реплики (без учёта регистра)")
    ap.add_argument("--speaker", help="регулярное выражение по говорящему (имя NPC, фракция, тип голоса)")
    ap.add_argument("--out", type=Path, default=ROOT / "datasets" / "game_quotes.tsv")
    args = ap.parse_args()

    data_dir = load_config().game.data_dir
    ilstrings = load_strings(data_dir / "Skyrim - Interface.bsa", "ilstrings")
    strings = load_strings(data_dir / "Skyrim - Interface.bsa", "strings")
    plugins = {name: read_plugin(data_dir / f"{name}.esm") for name in PLUGINS if (data_dir / f"{name}.esm").is_file()}

    def resolve(plugin: str, form: int) -> tuple[str, int]:
        """Форма из условия плагина plugin -> (плагин-владелец, номер без индекса мода)."""
        masters = plugins[plugin]["masters"]
        index = form >> 24
        return (masters[index] if index < len(masters) else plugin), form & 0xFFFFFF

    names: dict[tuple[str, int], str] = {}  # (плагин-владелец, номер) -> русское имя
    edids: dict[tuple[str, int], str] = {}  # -> EditorID (у типов голоса и части фракций имени нет)
    for name, p in plugins.items():
        table = {**strings.get("skyrim", {}), **strings.get(name, {})}
        for form, sid in p["names"].items():
            if text := table.get(sid):
                names[resolve(name, form)] = text
        for form, edid in p["edids"].items():
            edids[resolve(name, form)] = edid

    def title(plugin: str, form: int) -> str:
        key = resolve(plugin, form)
        return names.get(key) or edids.get(key) or f"{key[0]}:{key[1]:06X}"

    grep = re.compile(args.grep, re.I) if args.grep else None
    speaker_re = re.compile(args.speaker, re.I) if args.speaker else None
    args.out.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        out = csv.writer(f, delimiter="\t")
        out.writerow(["plugin", "info", "topic_type", "speaker", "factions", "voice", "emotion", "text"])
        for name, p in plugins.items():
            table = {**ilstrings.get("skyrim", {}), **ilstrings.get(name, {})}
            for form, topic, responses, conditions in p["infos"]:
                text = " ".join(TAG.sub("", table.get(sid, "")).strip() for _n, _e, sid in sorted(responses)).strip()
                if not text or (grep and not grep.search(text)):
                    continue
                who = [title(name, param) for func, param in conditions if func == GET_IS_ID]
                factions = [title(name, param) for func, param in conditions if func == GET_IN_FACTION]
                voices = [title(name, param) for func, param in conditions if func == GET_IS_VOICE_TYPE]
                if speaker_re and not any(speaker_re.search(s) for s in who + factions + voices):
                    continue
                emotion = EMOTIONS[responses[0][1]] if responses and responses[0][1] < len(EMOTIONS) else ""
                topic_type = p["dials"].get(topic, "")
                out.writerow([name, f"{form & 0xFFFFFF:06X}", topic_type, ", ".join(who), ", ".join(factions),
                              ", ".join(voices), emotion, text])
                rows += 1
    print(f"Реплик: {rows} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
