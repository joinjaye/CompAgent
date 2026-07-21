"""schema v3 -> v4 迁移：announcements 新增 duplicate_of 列。

背景（2026-07-21）：核查发现同源同 locale 下存在标题+正文完全一致、但 article_id 不同的
公告（如源站对同一条通知重新发布出新 ID），旧 schema 没有字段记录这种关系，下游
分析/看板会把它们当成两条独立事件重复计入。只按 content_hash 判重会误伤——源站的
CMS 存在不同事件（如不同代币的合约上线公告）复用同一段模板正文、只有标题不同的情况，
这些不能被当成重复合并掉。判重口径固定为「同 source + locale + title + content」，
见 src/pipeline/dedup.py。

用法：
    python scripts/migrate_v4.py [db_path]   # 不传参默认 data/competitor_intel.db

幂等：announcements 已有 duplicate_of 列时直接跳过，可重复执行。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db.connection import DEFAULT_DB_PATH, connect  # noqa: E402


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def migrate(db_path: Path | str = DEFAULT_DB_PATH) -> bool:
    """返回 True 表示实际执行了迁移；False 表示无需迁移（库/表不存在，或已是 v4）。"""
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"{db_path} 不存在，无需迁移（直接用 `python -m src.db init` 建新库）")
        return False

    conn = connect(db_path)
    conn.isolation_level = None
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "announcements" not in tables:
            print("announcements 表不存在，无需迁移")
            return False

        if "duplicate_of" in _table_columns(conn, "announcements"):
            print("announcements 已有 duplicate_of 列，无需迁移")
            return False

        print("检测到 v3 结构，开始迁移 announcements -> v4（新增 duplicate_of 列）...")
        conn.execute("BEGIN")
        try:
            conn.execute(
                "ALTER TABLE announcements ADD COLUMN duplicate_of TEXT REFERENCES announcements (uid)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_announcements_duplicate_of ON announcements (duplicate_of)"
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        print("迁移完成")
        return True
    finally:
        conn.close()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    migrate(target)
