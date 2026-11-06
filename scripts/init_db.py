"""建表 + 写入意图字典（幂等）。用法：python scripts/init_db.py"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import configure_logging, get_config  # noqa: E402
from app.db.connection import init_db  # noqa: E402


def main() -> None:
    configure_logging("INFO")
    cfg = get_config()
    counts = init_db(verbose=True)
    print(f"\nSQLite: {cfg.sqlite_path}")
    print(f"Chroma: {cfg.chroma_path}")
    print("表行数:", counts)
    print("\n完成。下一步可选：python scripts/seed_kb.py")


if __name__ == "__main__":
    main()
