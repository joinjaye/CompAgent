"""schema v2 -> v3 迁移（Phase 4）：

- insights 表从「逐条公告一行」改为「批次级一行」，废弃旧列（related_uids 的语义/
  category 的 NOT NULL 约束/summary 等字段全部改变），新增 batch_date/locale/
  article_count/is_locale_derived/derived_from_id/articles_analysis/
  zmx_evidence_uids/prompt_version/llm_tokens_used/updated_at。
- 新增 llm_cache 表（批次级 LLM 响应缓存）。

背景：本地 data/competitor_intel.db 是 gitignored 的开发态产物，`init_db` 是
`CREATE TABLE IF NOT EXISTS`，不会回溯迁移已存在的旧表结构。旧 insights 数据量极少
（只有开发态数据，Phase 4 之前 insights 表从未真正产出过数据——LLM 分析层本次才
实现），迁移只保留 id/source/category/created_at 这几列还能对上语义的字段，其余
新列全部 NULL/默认值，后续重跑 `python -m src.analysis` 会自动补齐。

沿用 scripts/migrate_v2.py 的标准流程：建 insights_v3 -> INSERT SELECT 能对上的
旧列 -> DROP insights -> RENAME insights_v3 -> insights。

用法：
    python scripts/migrate_v3.py [db_path]   # 不传参默认 data/competitor_intel.db

幂等：已是 v3 结构（insights 已有 batch_date 列）时直接跳过，可重复执行。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db.connection import DEFAULT_DB_PATH, connect  # noqa: E402
from src.db.operations import utcnow_iso  # noqa: E402

_INSIGHTS_V3_DDL = """
CREATE TABLE insights_v3 (
    id                 TEXT PRIMARY KEY,
    batch_date         TEXT NOT NULL,
    source             TEXT NOT NULL,
    category           TEXT NOT NULL
                      CHECK (category IN ('campaign', 'product', 'listing', 'delisting', 'other')),
    locale             TEXT NOT NULL,
    article_count      INTEGER NOT NULL DEFAULT 0,
    related_uids       TEXT NOT NULL DEFAULT '[]',
    is_locale_derived  BOOLEAN NOT NULL DEFAULT 0,
    derived_from_id    TEXT REFERENCES insights (id),
    summary            TEXT,
    articles_analysis  TEXT,
    zmx_diff           TEXT,
    diff_type          TEXT
                      CHECK (diff_type IS NULL OR diff_type IN ('ZMX已有', 'ZMX缺失', 'ZMX玩法不同', '混合', '不适用')),
    priority           TEXT
                      CHECK (priority IS NULL OR priority IN ('高', '中', '低')),
    zmx_evidence_uids  TEXT NOT NULL DEFAULT '[]',
    prompt_version     TEXT NOT NULL,
    llm_tokens_used    INTEGER,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
)
"""

_INSIGHTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_insights_batch ON insights (batch_date, source, category, locale)",
    "CREATE INDEX IF NOT EXISTS idx_insights_source_cat ON insights (source, category)",
]

_LLM_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key    TEXT PRIMARY KEY,
    response     TEXT NOT NULL,
    created_at   TEXT NOT NULL
)
"""


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _migrate_insights(conn: sqlite3.Connection) -> None:
    """旧 insights 行大多数字段跟新结构语义不兼容（category 可为 NULL、没有
    batch_date/locale 等新维度），只保留 id/source/category/created_at 几列还能
    直接照抄的字段；category 为 NULL 的旧行（v2 允许 NULL）灌不进 NOT NULL 的新列，
    直接跳过不搬（反正是极少量开发态数据，Phase 4 重跑会产出全新的批次行）。
    """
    old_columns = set(_table_columns(conn, "insights"))
    conn.execute(_INSIGHTS_V3_DDL)

    if {"id", "source", "category", "created_at"} <= old_columns:
        now = utcnow_iso()
        conn.execute(
            """
            INSERT INTO insights_v3 (
                id, batch_date, source, category, locale, related_uids,
                prompt_version, created_at, updated_at
            )
            SELECT id, '1970-01-01', source, category, 'EN', '[]',
                   'migrated-from-v2', created_at, ?
            FROM insights
            WHERE source IS NOT NULL AND category IS NOT NULL
            """,
            (now,),
        )

    conn.execute("DROP TABLE insights")
    conn.execute("ALTER TABLE insights_v3 RENAME TO insights")
    for stmt in _INSIGHTS_INDEXES:
        conn.execute(stmt)


def migrate(db_path: Path | str = DEFAULT_DB_PATH) -> bool:
    """返回 True 表示实际执行了迁移；False 表示无需迁移（库/表不存在，或已是 v3）。"""
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"{db_path} 不存在，无需迁移（直接用 `python -m src.db init` 建新库）")
        return False

    conn = connect(db_path)
    conn.isolation_level = None
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "insights" not in tables:
            print("insights 表不存在，无需迁移")
            return False

        if "batch_date" in _table_columns(conn, "insights"):
            print("insights 已是 v3 结构（batch_date 列已存在），无需迁移")
            return False

        print("检测到 v2 结构，开始迁移 insights -> v3 ...")
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        try:
            _migrate_insights(conn)
            conn.execute(_LLM_CACHE_DDL)
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
