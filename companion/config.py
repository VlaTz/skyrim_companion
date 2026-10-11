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


class LLMConfig(BaseModel):
    base_url: str = "http://localhost:1234/v1"
    model: str = "local-model"
    api_key_env: str = "LLM_API_KEY"
    temperature: float = 0.8
    max_tokens: int = 300
    history_turns: int = 10
    json_mode: bool = False
    timeout: float = 60


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

    @property
    def models_path(self) -> Path:
        return self.models_dir if self.models_dir.is_absolute() else ROOT / self.models_dir

    @property
    def character_path(self) -> Path:
        return ROOT / "characters" / self.character


def load_config(path: Path | None = None) -> Config:
    path = path or ROOT / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return Config.model_validate(data or {})
