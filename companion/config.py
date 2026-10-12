"""Загрузка config.yaml в типизированные настройки."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class PushToTalkConfig(BaseModel):
    enabled: bool = True
    key: str = "f8"
    min_duration: float = 0.3


class AudioConfig(BaseModel):
    input_device: int | str | None = None
    output_device: int | str | None = None
    sample_rate: int = 16000


class STTConfig(BaseModel):
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    language: str | None = "ru"
    beam_size: int = 5
    vad_filter: bool = True
    initial_prompt: str | None = None


class TTSConfig(BaseModel):
    model: str = "XTTS-v2"
    device: str = "cuda"
    language: str = "ru"
    temperature: float = 0.7
    speed: float = 1.0
    stream: bool = True
    stream_chunk_size: int = 20
    stream_prebuffer: float = 1.0


class LLMConfig(BaseModel):
    base_url: str = "http://localhost:1234/v1"
    model: str = "local-model"
    api_key: str = ""                 # сам ключ — только в config.local.yaml (он не в git)
    api_key_env: str = "LLM_API_KEY"  # или имя переменной окружения с ключом
    temperature: float = 0.8
    max_tokens: int = 300
    history_turns: int = 10
    json_mode: bool = False
    timeout: float = 60
    tools: bool = True           # агент может вызывать инструменты-справки (companion/tools.py)
    max_tool_rounds: int = 3     # сколько раз за ответ
    extra_body: dict = {}  # дополнительные поля запроса, свои у каждого сервера (OpenRouter: reasoning)


class GameConfig(BaseModel):
    # Папка Data игры: оттуда сервис читает строки игры (тексты журнала квестов).
    data_dir: Path = Path(r"D:\Steam\steamapps\common\Skyrim Special Edition\Data")

    @property
    def interface_bsa(self) -> Path:
        return self.data_dir / "Skyrim - Interface.bsa"

    @property
    def lip_generator(self) -> Path:
        # Генератор .lip (губы) из комплекта Creation Kit.
        return self.data_dir.parent / "Tools" / "LipGen" / "LipGenerator" / "LipGenerator.exe"


class NpcVoiceConfig(BaseModel):
    # Голос из уст Гослинга-NPC (companion/voice/npc_voice.py); иначе — колонки.
    enabled: bool = True
    mod_dir: Path = Path(r"D:\Games\Modding\MO2\mods\Gosling")  # мод в MO2 (tools/build_gosling_esp.py)
    max_distance: float = 30   # метров: дальше — говорим через колонки
    max_chars: int = 120       # длина фразы: короче — раньше звучит первая
    gap: float = 0.3           # пауза между фразами, с
    result_timeout: float = 12  # сколько ждать подтверждения от плагина (он ждёт, пока звук пойдёт, до 10 с), с
    lip: bool = True           # губы: .lip к каждой фразе генератором CK (+~0,3 с к первой фразе)

    @property
    def voice_dir(self) -> Path:
        from companion import gosling_mod as mod
        return self.mod_dir / "Sound" / "Voice" / mod.PLUGIN / mod.VOICE_TYPE


class MapConfig(BaseModel):
    known_count: int = 5          # сколько ближайших открытых мест показывать
    unknown_radius: float = 1000  # метров: неоткрытое ближе — видно (без названия)
    unknown_count: int = 3


class BrainConfig(BaseModel):
    enabled: bool = True
    min_gap: float = 60          # секунд между репликами по своей инициативе
    urgent_gap: float = 20       # то же для срочного (игрок тяжело ранен)
    settle: float = 3            # подождать, пока соберутся связанные события
    repeat_after: float = 600    # одно и то же событие повторно комментировать не раньше
    idle_after: float = 180      # столько тишины — и может заговорить сам
    idle_chance: float = 0.5
    chances: dict[str, float] = {  # шанс прокомментировать событие по его типу
        "loaded": 1.0, "location": 1.0, "wilderness": 0.5, "interior": 0.6, "combat_ended": 0.7,
        "low_health": 1.0, "item": 0.3, "discovered": 0.8, "overheard": 0.8,
    }
    # Подслушанное (реплики NPC рядом): решаем, когда разговор стих и игрок вышел из меню разговора.
    # Встревать ли — решает модель (обычно молчит), chances.overheard — сколько разговоров ей вообще показывать.
    overheard_quiet: float = 4   # секунд без новых реплик — разговор закончился
    overheard_gap: float = 20    # секунд между такими решениями (в городе болтают без умолку)


class Config(BaseModel):
    models_dir: Path = Path("models")
    character: str = "gosling"
    server: ServerConfig = ServerConfig()
    push_to_talk: PushToTalkConfig = PushToTalkConfig()
    echo: bool = True
    audio: AudioConfig = AudioConfig()
    stt: STTConfig = STTConfig()
    tts: TTSConfig = TTSConfig()
    llm: LLMConfig = LLMConfig()
    game: GameConfig = GameConfig()
    npc_voice: NpcVoiceConfig = NpcVoiceConfig()
    map: MapConfig = MapConfig()
    brain: BrainConfig = BrainConfig()

    @property
    def models_path(self) -> Path:
        return self.models_dir if self.models_dir.is_absolute() else ROOT / self.models_dir

    @property
    def character_path(self) -> Path:
        return ROOT / "characters" / self.character


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load_config(path: Path | None = None) -> Config:
    """config.yaml + config.local.yaml поверх него (секреты и личные настройки, не в git)."""
    path = path or ROOT / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    local = path.with_name(path.stem + ".local.yaml")
    if local.is_file():
        data = _merge(data or {}, yaml.safe_load(local.read_text(encoding="utf-8")) or {})
    return Config.model_validate(data or {})
