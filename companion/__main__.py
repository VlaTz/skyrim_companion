"""Запуск голосового сервиса:  python -m companion [--config config.yaml]"""

import argparse
import logging
from pathlib import Path

import uvicorn

from companion import trace
from companion.config import load_config
from companion.server import create_app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config(args.config)
    if cfg.overlay.enabled:  # окно с логом поверх игры — до сервиса, чтобы видеть и загрузку моделей
        from companion.overlay import Overlay
        Overlay(cfg.overlay).start()
    if cfg.trace:
        logging.getLogger(__name__).info("Трасса Гослинга: %s", trace.setup(cfg.logs_path / "trace"))
    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port, log_level="warning")


if __name__ == "__main__":
    main()
