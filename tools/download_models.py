"""Скачивает модели с Hugging Face в папку models/ по манифесту models.yaml.

Если модель Whisper лежит на HF в формате transformers, она автоматически
конвертируется в формат CTranslate2 (его использует faster-whisper).

Структура результата:
    models/whisper/<name>/            model.bin, config.json, tokenizer.json ...
    models/xtts/<name>/               config.json, model.pth, vocab.json, ...

Примеры:
    python tools/download_models.py
    python tools/download_models.py --only whisper:large-v3-ru --only xtts
    python tools/download_models.py --force
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import yaml
from huggingface_hub import hf_hub_download, snapshot_download

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
RAW_CACHE = MODELS / ".raw"

# Файлы, нужные для конвертации Whisper из transformers (без весов TF/Flax).
WHISPER_HF_PATTERNS = ["*.json", "*.txt", "*.safetensors", "merges.txt", "vocab.json"]
WHISPER_CT2_FILES = ["model.bin", "config.json", "tokenizer.json", "vocabulary.txt",
                     "vocabulary.json", "preprocessor_config.json"]


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch(repo: str, filename: str, dst: Path) -> None:
    """Скачивает один файл сразу в dst, минуя общий кэш HF на диске C:."""
    tmp = RAW_CACHE / "files"
    shutil.move(hf_hub_download(repo, filename, local_dir=tmp), dst)


def selected(section: str, name: str | None, only: list[str]) -> bool:
    if not only:
        return True
    for item in only:
        sec, _, nm = item.partition(":")
        if sec == section and (not nm or nm == name):
            return True
    return False


# ---------------------------------------------------------------- whisper

def is_ct2_model(path: Path) -> bool:
    return (path / "model.bin").is_file()


def is_hf_whisper(path: Path) -> bool:
    cfg = path / "config.json"
    if not cfg.is_file():
        return False
    model_type = json.loads(cfg.read_text(encoding="utf-8")).get("model_type")
    has_weights = any(path.glob("*.safetensors")) or (path / "pytorch_model.bin").is_file()
    return model_type == "whisper" and has_weights


def convert_whisper(src: Path, dst: Path, quantization: str) -> None:
    from ctranslate2.converters import TransformersConverter

    copy_files = [f for f in ("tokenizer.json", "preprocessor_config.json") if (src / f).is_file()]
    log(f"  конвертация transformers -> CTranslate2 ({quantization}) ...")
    TransformersConverter(str(src), copy_files=copy_files, load_as_float16=True).convert(
        str(dst), quantization=quantization, force=True
    )
    # faster-whisper берёт токенайзер из tokenizer.json; если его нет в репо — соберём.
    if not (dst / "tokenizer.json").is_file():
        from transformers import WhisperTokenizerFast

        WhisperTokenizerFast.from_pretrained(str(src)).backend_tokenizer.save(str(dst / "tokenizer.json"))


def download_whisper(entry: dict, force: bool, keep_raw: bool) -> None:
    name, repo = entry["name"], entry["repo"]
    dst = MODELS / "whisper" / name
    if is_ct2_model(dst) and not force:
        log(f"[whisper] {name}: уже есть, пропуск")
        return

    log(f"[whisper] {name}: скачивание {repo}")
    raw = RAW_CACHE / "whisper" / name
    snapshot_download(repo, local_dir=raw,
                      allow_patterns=WHISPER_CT2_FILES + WHISPER_HF_PATTERNS)

    if dst.exists():
        shutil.rmtree(dst)
    if is_ct2_model(raw):
        log("  формат CTranslate2, конвертация не нужна")
        dst.mkdir(parents=True)
        for f in WHISPER_CT2_FILES:
            if (raw / f).is_file():
                shutil.copy2(raw / f, dst / f)
    elif is_hf_whisper(raw):
        convert_whisper(raw, dst, entry.get("quantization", "int8_float16"))
    else:
        raise RuntimeError(f"{repo}: не похоже ни на CTranslate2, ни на transformers-Whisper")

    if not keep_raw:
        shutil.rmtree(raw, ignore_errors=True)
    log(f"  готово: {dst.relative_to(ROOT)}")


# ---------------------------------------------------------------- xtts

XTTS_FILES = ["config.json", "model.pth", "vocab.json", "speakers_xtts.pth", "dvae.pth", "mel_stats.pth",
              "LICENSE.txt", "README.md"]


def download_xtts(entry: dict, force: bool) -> None:
    name, repo = entry["name"], entry["repo"]
    dst = MODELS / "xtts" / name
    if (dst / "model.pth").is_file() and (dst / "config.json").is_file() and not force:
        log(f"[xtts] {name}: уже есть, пропуск")
        return

    log(f"[xtts] {name}: скачивание {repo}")
    snapshot_download(repo, local_dir=dst, allow_patterns=entry.get("files", XTTS_FILES))
    log(f"  готово: {dst.relative_to(ROOT)}")


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, default=ROOT / "models.yaml")
    ap.add_argument("--only", action="append", default=[],
                    help="секция или секция:имя (whisper, xtts); "
                         "явно указанные модели качаются даже при enabled: false")
    ap.add_argument("--force", action="store_true", help="перекачать, даже если уже есть")
    ap.add_argument("--keep-raw", action="store_true", help="не удалять исходники после конвертации")
    args = ap.parse_args()

    manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))

    def wanted(section: str, entry: dict) -> bool:
        explicit = f"{section}:{entry['name']}" in args.only
        return explicit or (entry.get("enabled", True) and selected(section, entry["name"], args.only))

    errors = []
    jobs = (
        [(e, lambda e: download_whisper(e, args.force, args.keep_raw)) for e in manifest.get("whisper", []) if wanted("whisper", e)]
        + [(e, lambda e: download_xtts(e, args.force)) for e in manifest.get("xtts", []) if wanted("xtts", e)]
    )
    for entry, job in jobs:
        try:
            job(entry)
        except Exception as e:  # одна битая модель не должна останавливать остальные
            errors.append(f"{entry['name']}: {e}")
            log(f"  ОШИБКА: {e}")


    shutil.rmtree(RAW_CACHE / "files", ignore_errors=True)
    if errors:
        log("\nНе удалось:\n  " + "\n  ".join(errors))
        return 1
    log("\nВсе модели на месте.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
