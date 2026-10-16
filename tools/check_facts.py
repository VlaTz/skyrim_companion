"""Сверить уже записанные факты памяти с данными игры: что противоречит — показать (и с --apply убрать).

Летописец до 2026-10-16 записывал фактами и выдумки из разговора («Лукан в лавке Белетора»). Новые факты он теперь
сверяет сам (companion/memory/diarist.py, verify_facts); этот скрипт — для накопленных раньше. Убранный факт не
стирается: archived = 2 («неверное»), его видно в базе.

    python tools/check_facts.py                       # все персонажи из memory/, только показать
    python tools/check_facts.py --apply               # убрать неверные
    python tools/check_facts.py --db memory/gosling/02CCEDA2.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from companion.config import load_config  # noqa: E402
from companion.game.places import PlaceBook  # noqa: E402
from companion.knowledge import Knowledge  # noqa: E402
from companion.llm import CompanionLLM  # noqa: E402
from companion.memory.diarist import verify_facts  # noqa: E402

BATCH = 12  # фактов за один запрос


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, action="append", help="файл памяти (по умолчанию — все в memory/)")
    ap.add_argument("--apply", action="store_true", help="убрать неверные (archived = 2)")
    args = ap.parse_args()

    cfg = load_config()
    dbs = args.db or sorted(cfg.memory_path.glob("*.db"))
    knowledge = Knowledge(cfg.game.data_dir, PlaceBook(cfg.game.data_dir))
    llm = CompanionLLM(cfg.llm, cfg.character_path / "prompt.md")
    for path in dbs:
        db = sqlite3.connect(path)
        facts = db.execute("SELECT id, subject, text, source FROM facts WHERE archived = 0 ORDER BY id").fetchall()
        print(f"\n== {path.name}: фактов {len(facts)}")
        wrong_ids = []
        for start in range(0, len(facts), BATCH):
            part = facts[start:start + BATCH]
            for i, why in verify_facts([(s, t) for _id, s, t, _src in part], knowledge, llm.complete_json).items():
                fid, subject, text, source = part[i]
                wrong_ids.append(fid)
                print(f"  ✗ [{fid}] {subject}: {text}" + (f" ({source})" if source else "") + f"\n      — {why}")
        if args.apply and wrong_ids:
            db.executemany("UPDATE facts SET archived = 2 WHERE id = ?", [(i,) for i in wrong_ids])
            db.commit()
        print(f"  неверных: {len(wrong_ids)}" + (" — убраны" if args.apply and wrong_ids else ""))
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
