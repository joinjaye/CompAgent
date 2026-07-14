"""scripts/migrate_v3.py 单测：对手工构造的 v2 结构临时库跑迁移，验证新列存在、
旧数据里能对上语义的部分被保留、llm_cache 表建好、幂等。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from migrate_v3 import migrate  # noqa: E402

_V2_SCHEMA = """
CREATE TABLE announcements (
    uid TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    locale TEXT NOT NULL,
    article_id TEXT NOT NULL
);
CREATE TABLE insights (
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
);
"""


@pytest.fixture()
def v2_db_path(tmp_path):
    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.executescript(_V2_SCHEMA)
    conn.execute(
        "INSERT INTO insights (id, related_uids, source, category, summary, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("i1", '["u1"]', "Bitunix", "listing", "old summary", "2026-01-01T00:00:00Z"),
    )
    # category=NULL 的旧行：新 schema category 是 NOT NULL，不应该被搬过去
    conn.execute(
        "INSERT INTO insights (id, related_uids, source, category, created_at) VALUES (?,?,?,?,?)",
        ("i2", '["u2"]', "Weex", None, "2026-01-02T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    return path


def test_migrate_returns_false_when_db_missing(tmp_path):
    assert migrate(tmp_path / "does_not_exist.db") is False


def test_migrate_returns_false_when_insights_table_missing(tmp_path):
    path = tmp_path / "no_insights.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE announcements (uid TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    assert migrate(path) is False


def test_migrate_adds_v3_columns_and_llm_cache_table(v2_db_path):
    assert migrate(v2_db_path) is True

    conn = sqlite3.connect(v2_db_path)
    conn.row_factory = sqlite3.Row
    columns = [row[1] for row in conn.execute("PRAGMA table_info(insights)")]
    for col in ("batch_date", "locale", "article_count", "is_locale_derived",
                "derived_from_id", "articles_analysis", "zmx_evidence_uids",
                "prompt_version", "llm_tokens_used", "updated_at"):
        assert col in columns

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "llm_cache" in tables
    conn.close()


def test_migrate_preserves_rows_with_non_null_category(v2_db_path):
    migrate(v2_db_path)
    conn = sqlite3.connect(v2_db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM insights WHERE id = 'i1'").fetchone()
    assert row["source"] == "Bitunix"
    assert row["category"] == "listing"
    assert row["prompt_version"] == "migrated-from-v2"
    conn.close()


def test_migrate_drops_rows_with_null_category(v2_db_path):
    migrate(v2_db_path)
    conn = sqlite3.connect(v2_db_path)
    row = conn.execute("SELECT * FROM insights WHERE id = 'i2'").fetchone()
    assert row is None
    conn.close()


def test_migrate_is_idempotent(v2_db_path):
    assert migrate(v2_db_path) is True
    assert migrate(v2_db_path) is False
