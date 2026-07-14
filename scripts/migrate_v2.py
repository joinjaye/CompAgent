"""schema v1 -> v2 迁移：

- announcements 新增 raw_category TEXT 列（NULL 填充，历史数据本来就没有这份信息）
- announcements.category / insights.category 的 CHECK 约束新增 delisting

背景：本地 data/competitor_intel.db 是 gitignored 的开发态产物，`init_db` 是
`CREATE TABLE IF NOT EXISTS`，不会回溯迁移已存在的旧表结构（列、CHECK 约束都不会变）。
开发态直接 `rm data/competitor_intel.db` 重建更干净（Bitunix/Weex 已入库的数据无论如何
都要重刷才能拿到 raw_category 和 Phase 2.5 清洗后的纯文本 content）。这个脚本主要是把
标准的 SQLite 表重建流程（建新表 -> 复制数据 -> drop 旧表 -> rename）沉淀下来，供 Phase 8
上线前的正式 migration 需求起步用；也是本项目第一次需要"改列/改约束"的场景，之前的
crawl_state 加 category 列（Phase 2 批次 2）没有配套 migration，只是记了"删库重建"。

用法：
    python scripts/migrate_v2.py [db_path]   # 不传参默认 data/competitor_intel.db

幂等：已是 v2 结构（announcements 已有 raw_category 列）时直接跳过，可重复执行。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db.connection import DEFAULT_DB_PATH, connect  # noqa: E402

_ANNOUNCEMENTS_V2_DDL = """
CREATE TABLE announcements_v2 (
    uid                  TEXT PRIMARY KEY,
    group_id             TEXT,
    source               TEXT NOT NULL,
    locale               TEXT NOT NULL,
    article_id           TEXT NOT NULL,
    url                  TEXT,
    title                TEXT,
    content              TEXT,
    raw_category         TEXT,
    content_hash         TEXT,
    post_time            TEXT,
    update_time          TEXT,
    fetched_at           TEXT,
    status               TEXT NOT NULL DEFAULT 'new'
                         CHECK (status IN ('new', 'changed', 'unchanged')),
    category             TEXT
                         CHECK (category IS NULL OR category IN ('campaign', 'product', 'listing', 'delisting', 'other')),
    is_region_exclusive  BOOLEAN NOT NULL DEFAULT 0,
    push_status          TEXT NOT NULL DEFAULT 'pending'
                         CHECK (push_status IN ('pending', 'pushed', 'skipped')),
    source_endpoint      TEXT
)
"""

_INSIGHTS_V2_DDL = """
CREATE TABLE insights_v2 (
    id             TEXT PRIMARY KEY,
    related_uids   TEXT,
    source         TEXT,
    category       TEXT
                  CHECK (category IS NULL OR category IN ('campaign', 'product', 'listing', 'delisting', 'other')),
    summary        TEXT,
    zmx_diff       TEXT,
    diff_type      TEXT
                  CHECK (diff_type IS NULL OR diff_type IN ('ZMX已有', 'ZMX缺失', 'ZMX玩法不同', '不适用')),
    priority       TEXT
                  CHECK (priority IS NULL OR priority IN ('高', '中', '低')),
    created_at     TEXT
)
"""

_ANNOUNCEMENTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_announcements_source_locale ON announcements (source, locale)",
    "CREATE INDEX IF NOT EXISTS idx_announcements_group_id ON announcements (group_id)",
    "CREATE INDEX IF NOT EXISTS idx_announcements_status ON announcements (status)",
    "CREATE INDEX IF NOT EXISTS idx_announcements_push_status ON announcements (push_status)",
    "CREATE INDEX IF NOT EXISTS idx_announcements_category ON announcements (category)",
]

_INSIGHTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_insights_source_category ON insights (source, category)",
    "CREATE INDEX IF NOT EXISTS idx_insights_diff_type ON insights (diff_type)",
]


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _rebuild_table(conn: sqlite3.Connection, table: str, new_ddl: str, extra_new_columns: list[str]) -> None:
    """标准 SQLite 表重建流程：建新表 -> 复制数据（新增列填 NULL）-> drop 旧表 -> rename。

    重建后原表名重新指回新表，其它表（如 content_history）里 `REFERENCES announcements`
    这类外键声明本来就没引用过 `announcements_v2` 这个中间表名，不会被 SQLite 的
    "rename 自动重写引用" 机制误伤。
    """
    old_columns = _table_columns(conn, table)
    new_table = f"{table}_v2"

    conn.execute(new_ddl)

    insert_cols = ", ".join(old_columns + extra_new_columns)
    select_clause = ", ".join(old_columns) + (
        ", " + ", ".join(["NULL"] * len(extra_new_columns)) if extra_new_columns else ""
    )
    conn.execute(f"INSERT INTO {new_table} ({insert_cols}) SELECT {select_clause} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {new_table} RENAME TO {table}")


def migrate(db_path: Path | str = DEFAULT_DB_PATH) -> bool:
    """返回 True 表示实际执行了迁移；False 表示无需迁移（库/表不存在，或已是 v2）。"""
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"{db_path} 不存在，无需迁移（直接用 `python -m src.db init` 建新库）")
        return False

    conn = connect(db_path)
    conn.isolation_level = None  # 自己管理事务边界，避免 sqlite3 模块隐式事务跟显式 BEGIN 打架
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "announcements" not in tables:
            print("announcements 表不存在，无需迁移")
            return False

        if "raw_category" in _table_columns(conn, "announcements"):
            print("announcements 已是 v2 结构（raw_category 列已存在），无需迁移")
            return False

        print("检测到 v1 结构，开始迁移 -> v2 ...")
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        try:
            _rebuild_table(conn, "announcements", _ANNOUNCEMENTS_V2_DDL, ["raw_category"])
            for stmt in _ANNOUNCEMENTS_INDEXES:
                conn.execute(stmt)
            if "insights" in tables:
                _rebuild_table(conn, "insights", _INSIGHTS_V2_DDL, [])
                for stmt in _INSIGHTS_INDEXES:
                    conn.execute(stmt)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        print("迁移完成")
        return True
    finally:
        conn.close()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    migrate(target)
