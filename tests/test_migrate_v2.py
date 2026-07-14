"""scripts/migrate_v2.py 单测：对一个手工构造的 v1 结构临时库跑 migration，
验证数据不丢、新列存在、CHECK 约束放开、幂等（对已是 v2 的库重跑是 no-op）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from migrate_v2 import migrate  # noqa: E402

_V1_SCHEMA = """
CREATE TABLE announcements (
    uid                  TEXT PRIMARY KEY,
    group_id             TEXT,
    source               TEXT NOT NULL,
    locale               TEXT NOT NULL,
    article_id           TEXT NOT NULL,
    url                  TEXT,
    title                TEXT,
    content              TEXT,
    content_hash         TEXT,
    post_time            TEXT,
    update_time          TEXT,
    fetched_at            TEXT,
    status               TEXT NOT NULL DEFAULT 'new'
                         CHECK (status IN ('new', 'changed', 'unchanged')),
    category             TEXT
                         CHECK (category IS NULL OR category IN ('campaign', 'product', 'listing', 'other')),
    is_region_exclusive  BOOLEAN NOT NULL DEFAULT 0,
    push_status          TEXT NOT NULL DEFAULT 'pending'
                         CHECK (push_status IN ('pending', 'pushed', 'skipped')),
    source_endpoint      TEXT
);
CREATE TABLE content_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uid             TEXT NOT NULL REFERENCES announcements (uid) ON DELETE CASCADE,
    content_hash    TEXT,
    content         TEXT,
    captured_at     TEXT
);
CREATE TABLE insights (
    id             TEXT PRIMARY KEY,
    related_uids   TEXT,
    source         TEXT,
    category       TEXT
                  CHECK (category IS NULL OR category IN ('campaign', 'product', 'listing', 'other')),
    summary        TEXT,
    zmx_diff       TEXT,
    diff_type      TEXT
                  CHECK (diff_type IS NULL OR diff_type IN ('ZMX已有', 'ZMX缺失', 'ZMX玩法不同', '不适用')),
    priority       TEXT
                  CHECK (priority IS NULL OR priority IN ('高', '中', '低')),
    created_at     TEXT
);
CREATE TABLE crawl_state (
    source           TEXT NOT NULL,
    locale           TEXT NOT NULL,
    category         TEXT NOT NULL DEFAULT '',
    high_watermark   TEXT,
    strategy         TEXT NOT NULL DEFAULT 'watermark'
                    CHECK (strategy IN ('watermark', 'full_scan')),
    updated_at       TEXT,
    PRIMARY KEY (source, locale, category)
);
"""


@pytest.fixture()
def v1_db_path(tmp_path):
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_V1_SCHEMA)
    conn.execute(
        "INSERT INTO announcements (uid, group_id, source, locale, article_id, url, title, "
        "content, content_hash, post_time, update_time, fetched_at, status, category, "
        "is_region_exclusive, push_status, source_endpoint) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("u1", "g1", "Bitunix", "EN", "1", "https://x", "title", "content", "hash1",
         "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z",
         "new", "listing", 0, "pending", "https://endpoint"),
    )
    conn.execute(
        "INSERT INTO content_history (uid, content_hash, content, captured_at) VALUES (?,?,?,?)",
        ("u1", "old-hash", "old content", "2025-12-31T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO crawl_state (source, locale, category, high_watermark, strategy, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("Bitunix", "EN", "", "2026-01-02T00:00:00Z", "watermark", "2026-01-02T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    return path


def test_migrate_returns_false_when_db_missing(tmp_path):
    assert migrate(tmp_path / "does_not_exist.db") is False


def test_migrate_adds_raw_category_column_and_preserves_data(v1_db_path):
    assert migrate(v1_db_path) is True

    conn = sqlite3.connect(v1_db_path)
    conn.row_factory = sqlite3.Row
    columns = [row[1] for row in conn.execute("PRAGMA table_info(announcements)")]
    assert "raw_category" in columns

    row = conn.execute("SELECT * FROM announcements WHERE uid = 'u1'").fetchone()
    assert row["source"] == "Bitunix"
    assert row["content"] == "content"
    assert row["raw_category"] is None  # 历史数据本来就没有这份信息，填 NULL

    history = conn.execute("SELECT * FROM content_history WHERE uid = 'u1'").fetchall()
    assert len(history) == 1

    crawl = conn.execute("SELECT * FROM crawl_state").fetchall()
    assert len(crawl) == 1
    conn.close()


def test_migrate_widens_category_check_to_allow_delisting(v1_db_path):
    migrate(v1_db_path)

    conn = sqlite3.connect(v1_db_path)
    conn.execute("UPDATE announcements SET category = 'delisting' WHERE uid = 'u1'")
    conn.commit()
    row = conn.execute("SELECT category FROM announcements WHERE uid = 'u1'").fetchone()
    assert row[0] == "delisting"
    conn.close()


def test_migrate_preserves_foreign_key_cascade_delete(v1_db_path):
    migrate(v1_db_path)

    conn = sqlite3.connect(v1_db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM announcements WHERE uid = 'u1'")
    conn.commit()
    remaining = conn.execute("SELECT COUNT(*) FROM content_history").fetchone()[0]
    assert remaining == 0
    conn.close()


def test_migrate_is_idempotent_on_already_v2_db(v1_db_path):
    assert migrate(v1_db_path) is True
    assert migrate(v1_db_path) is False  # 已是 v2，第二次调用是 no-op
