"""Типы голоса игры и образцы голосов NPC — из файлов игры (для разговоров Гослинга с NPC, companion/talk.py).

Тип голоса (VTYP) — папка звуков реплик: Sound\\Voice\\<плагин>\\<EditorID типа голоса>\\. В игре у формы нет
EditorID, поэтому плагин шлёт номер типа голоса NPC, а название папки берём из мастер-файлов: у официальных мастеров
номера в файле совпадают с номерами в игре (Skyrim.esm — 00, Update — 01, Dawnguard — 02, HearthFires — 03,
Dragonborn — 04).

Образцы голоса — реплики этого типа голоса из BSA (как у Гослинга: tools/extract_voices.py): .fuz -> xWMA -> wav,
несколько реплик 3–10 с без криков; кэш — в data/npc_voices/<тип голоса>/. По ним XTTS говорит голосом NPC.
"""

from __future__ import annotations

import io
import logging
import re
import struct
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

from companion.game.books import plugin_records
from companion.game.gamefiles import read_bsa_index, read_file

log = logging.getLogger(__name__)

MASTERS = ["Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm"]
EXCLUDE = re.compile(r"combat|attack|bleedout|bardsong|dragon|flee|death|hit_|shout|yell|scream|grunt|breath",
                     re.IGNORECASE)
MIN_SECONDS, MAX_SECONDS = 3.0, 10.0
CANDIDATES = 30  # реплик разобрать, чтобы выбрать образцы нужной длины
TARGET_BYTES = 45000  # ~6 с реплики в .fuz


def voice_types(data_dir: Path) -> dict[int, str]:
    """Номер типа голоса в игре -> его EditorID (название папки звуков)."""
    found: dict[int, str] = {}
    for name in MASTERS:
        path = data_dir / name
        if not path.is_file():
            continue
        for _typ, form, subs in plugin_records(path, {b"VTYP"}):
            if edid := subs.get(b"EDID", [b""])[0].rstrip(b"\0").decode("ascii", "replace"):
                found[form] = edid
    return found


def fuz_audio(data: bytes) -> bytes:
    """.fuz = 'FUZE' + версия + размер lip + lip + xwm. Возвращает xwm (или сами данные — уже .xwm/.wav)."""
    if data[:4] != b"FUZE":
        return data
    lip_size = struct.unpack_from("<I", data, 8)[0]
    return data[12 + lip_size:]


def decode(audio: bytes) -> tuple[np.ndarray, int]:
    """xWMA / wav -> моно float32 и частота (PyAV)."""
    import av

    with av.open(io.BytesIO(audio)) as container:
        stream = container.streams.audio[0]
        frames = [fr.to_ndarray() for fr in container.decode(stream)]
        sr = stream.rate
    pcm = np.concatenate(frames, axis=1).astype(np.float32)
    if pcm.ndim == 2:
        pcm = pcm.mean(axis=0) if pcm.shape[0] <= 2 else pcm.reshape(-1)
    return pcm, sr


class VoiceSamples:
    """Образцы голоса по типу голоса: из BSA при первом обращении, дальше — из кэша."""

    def __init__(self, data_dir: Path, bsa_name: str, cache_dir: Path, count: int = 3):
        self.data_dir = data_dir
        self.bsa = data_dir / bsa_name
        self.cache_dir = cache_dir
        self.count = count
        self._types: dict[int, str] | None = None
        self._index: dict[str, list] | None = None  # тип голоса (папка, без регистра) -> записи BSA
        self._flags = 0
        self._lock = threading.Lock()

    def edid(self, form: int) -> str:
        """Название папки типа голоса по номеру в игре; "" — не официальный тип голоса (мод) или нет номера."""
        with self._lock:
            if self._types is None:
                self._types = voice_types(self.data_dir)
            return self._types.get(form, "") if form else ""

    def refs(self, edid: str) -> list[Path]:
        """Образцы голоса (wav) для типа голоса; [] — не нашлось."""
        folder = self.cache_dir / edid.lower()
        if cached := sorted(folder.glob("*.wav")):
            return cached
        with self._lock:
            if cached := sorted(folder.glob("*.wav")):
                return cached
            return self._extract(edid, folder)

    def _extract(self, edid: str, folder: Path) -> list[Path]:
        if not self.bsa.is_file():
            log.warning("Голоса NPC: нет %s — образцов не будет", self.bsa)
            return []
        with open(self.bsa, "rb") as f:
            if self._index is None:
                entries, self._flags = read_bsa_index(f)
                self._index = {}
                for e in entries:
                    if e.folder.startswith("sound\\voice\\") and e.name.lower().endswith((".fuz", ".xwm", ".wav")):
                        self._index.setdefault(e.folder.rsplit("\\", 1)[-1].lower(), []).append(e)
            entries = [e for e in self._index.get(edid.lower(), []) if not EXCLUDE.search(e.name)]
            # Ближе всего по размеру к реплике в ~6 с (xWMA ~48 кбит/с + .lip): короткие — «Хм», длинные — монологи.
            entries.sort(key=lambda e: abs(e.size - TARGET_BYTES))
            picked: list[Path] = []
            for e in entries[:CANDIDATES]:
                try:
                    audio, sr = decode(fuz_audio(read_file(f, e, self._flags)))
                except Exception:  # noqa: BLE001 — битая реплика: берём другую
                    continue
                if MIN_SECONDS <= len(audio) / sr <= MAX_SECONDS:
                    folder.mkdir(parents=True, exist_ok=True)
                    path = folder / f"ref{len(picked) + 1}.wav"
                    sf.write(path, audio, sr, subtype="PCM_16")
                    picked.append(path)
                    if len(picked) >= self.count:
                        break
        log.info("Голоса NPC: %s — образцов %d (из %d реплик)", edid, len(picked), len(entries))
        return picked
