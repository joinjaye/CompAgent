"""db 层单测：建库、插入、去重、变更检测、水位线读写。

全部基于临时 SQLite 文件跑，不依赖网络、不依赖任何真实数据源。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.connection import get_connection, init_db
from src.db.operations import (
    compute_content_hash,
    compute_uid,
    get_announcement,
    get_content_history,
    get_crawl_state,
    set_crawl_state,
    upsert_announcement,
)


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


@pytest.fixture()
def conn(db_path):
    with get_connection(db_path) as connection:
        yield connection


# ---------------------------------------------------------------- 建库 ----

def test_init_db_creates_all_tables(db_path):
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    table_names = {row["name"] for row in rows}
    expected = {
        "announcements",
        "content_history",
        "insights",
        "crawl_state",
        "sync_log",
    }
    assert expected.issubset(table_names)


def test_init_db_is_idempotent(db_path):
    # 再跑一次 init 不应报错(CREATE TABLE IF NOT EXISTS)
    init_db(db_path)
    init_db(db_path)


# ---------------------------------------------------------------- 插入 ----

def test_insert_new_announcement(conn):
    result = upsert_announcement(
        conn,
        source="Bitunix",
        locale="EN",
        article_id="1001",
        url="https://bitunix.example/a/1001",
        title="Trading Competition",
        content="Join our trading competition, prize pool 100000 USDT.",
        post_time="2026-07-10T00:00:00Z",
        update_time="2026-07-10T00:00:00Z",
    )

    assert result.status == "new"
    assert result.uid == compute_uid("Bitunix", "EN", "1001")

    row = get_announcement(conn, result.uid)
    assert row is not None
    assert row["status"] == "new"
    assert row["push_status"] == "pending"
    assert row["content_hash"] == compute_content_hash(
        "Join our trading competition, prize pool 100000 USDT."
    )


def test_uid_is_deterministic_sha256_of_source_locale_article_id():
    import hashlib

    uid = compute_uid("Bitunix", "EN", "1001")
    assert uid == hashlib.sha256(b"Bitunix_EN_1001").hexdigest()


# ---------------------------------------------------------------- 去重 ----

def test_reinsert_same_content_is_unchanged_and_does_not_duplicate(conn):
    kwargs = dict(
        source="Weex",
        locale="FR",
        article_id="2002",
        title="Listing",
        content="XYZ is now listed.",
        post_time="2026-07-10T00:00:00Z",
    )

    first = upsert_announcement(conn, **kwargs, fetched_at="2026-07-10T01:00:00Z")
    second = upsert_announcement(conn, **kwargs, fetched_at="2026-07-11T01:00:00Z")

    assert first.status == "new"
    assert second.status == "unchanged"
    assert first.uid == second.uid

    count = conn.execute(
        "SELECT COUNT(*) AS c FROM announcements WHERE uid = ?", (first.uid,)
    ).fetchone()["c"]
    assert count == 1

    row = get_announcement(conn, first.uid)
    assert row["status"] == "unchanged"
    # fetched_at 应更新为最新一次抓取时间
    assert row["fetched_at"] == "2026-07-11T01:00:00Z"

    # 内容未变，不应产生历史记录
    assert get_content_history(conn, first.uid) == []


def test_unchanged_content_but_raw_category_moved_updates_raw_category_only(conn):
    """正文没变但源端分类归属变了（如 Zendesk 后台把文章挪到另一个 section）：
    status 仍是 unchanged，不进 content_history、不重置 push_status，只补正
    raw_category，否则会一直停在第一次抓到的旧分类上（Phase 2.6 订正）。"""
    kwargs = dict(source="Bitunix", locale="EN", article_id="4004", content="same content")

    first = upsert_announcement(
        conn, **kwargs, raw_category="111", fetched_at="2026-07-10T00:00:00Z"
    )
    row = get_announcement(conn, first.uid)
    conn.execute("UPDATE announcements SET push_status = 'pushed' WHERE uid = ?", (first.uid,))

    second = upsert_announcement(
        conn, **kwargs, raw_category="222", fetched_at="2026-07-11T00:00:00Z"
    )

    assert second.status == "unchanged"
    row = get_announcement(conn, first.uid)
    assert row["raw_category"] == "222"
    assert row["push_status"] == "pushed"  # 分区变动不是内容变更，不应重新触发推送
    assert get_content_history(conn, first.uid) == []  # 也不该产生历史记录


# ---------------------------------------------------------------- 变更检测 ----

def test_content_change_is_detected_and_archived_to_history(conn):
    base_kwargs = dict(source="BingX", locale="VN", article_id="3003", title="Promo")

    first = upsert_announcement(
        conn,
        **base_kwargs,
        content="Prize pool: 100000 USDT",
        fetched_at="2026-07-10T00:00:00Z",
    )
    second = upsert_announcement(
        conn,
        **base_kwargs,
        content="Prize pool: 500000 USDT",
        fetched_at="2026-07-12T00:00:00Z",
    )

    assert first.status == "new"
    assert second.status == "changed"
    assert first.uid == second.uid

    row = get_announcement(conn, first.uid)
    assert row["content"] == "Prize pool: 500000 USDT"
    assert row["content_hash"] == compute_content_hash("Prize pool: 500000 USDT")
    # 变更后应重新进入待推送状态
    assert row["push_status"] == "pending"

    history = get_content_history(conn, first.uid)
    assert len(history) == 1
    assert history[0]["content"] == "Prize pool: 100000 USDT"
    assert history[0]["content_hash"] == compute_content_hash("Prize pool: 100000 USDT")
    assert history[0]["captured_at"] == "2026-07-10T00:00:00Z"


def test_content_hash_manual_tamper_is_detected_as_changed(conn):
    """模拟手动改库里的 content_hash 后重跑，应识别为 changed（Phase 2 幂等验收场景的前置保证）。"""
    result = upsert_announcement(
        conn,
        source="Phemex",
        locale="EN",
        article_id="4004",
        content="original content",
        fetched_at="2026-07-10T00:00:00Z",
    )

    conn.execute(
        "UPDATE announcements SET content_hash = 'tampered-hash' WHERE uid = ?",
        (result.uid,),
    )

    second = upsert_announcement(
        conn,
        source="Phemex",
        locale="EN",
        article_id="4004",
        content="original content",
        fetched_at="2026-07-11T00:00:00Z",
    )

    assert second.status == "changed"


# ---------------------------------------------------------------- 水位线 ----

def test_crawl_state_write_then_read(conn):
    assert get_crawl_state(conn, "Lbank", "VN") is None

    set_crawl_state(
        conn,
        source="Lbank",
        locale="VN",
        high_watermark="2026-07-10T00:00:00Z",
        strategy="watermark",
        updated_at="2026-07-10T00:05:00Z",
    )

    row = get_crawl_state(conn, "Lbank", "VN")
    assert row["high_watermark"] == "2026-07-10T00:00:00Z"
    assert row["strategy"] == "watermark"
    assert row["updated_at"] == "2026-07-10T00:05:00Z"


def test_crawl_state_upsert_overwrites_existing_row(conn):
    set_crawl_state(
        conn, source="Lbank", locale="ID", high_watermark="2026-07-10T00:00:00Z"
    )
    set_crawl_state(
        conn, source="Lbank", locale="ID", high_watermark="2026-07-12T00:00:00Z"
    )

    row = get_crawl_state(conn, "Lbank", "ID")
    assert row["high_watermark"] == "2026-07-12T00:00:00Z"

    count = conn.execute(
        "SELECT COUNT(*) AS c FROM crawl_state WHERE source = ? AND locale = ?",
        ("Lbank", "ID"),
    ).fetchone()["c"]
    assert count == 1


def test_crawl_state_full_scan_strategy_has_no_watermark_requirement(conn):
    set_crawl_state(
        conn, source="Zoomex", locale="EN-Asia", high_watermark=None, strategy="full_scan"
    )
    row = get_crawl_state(conn, "Zoomex", "EN-Asia")
    assert row["high_watermark"] is None
    assert row["strategy"] == "full_scan"


# ---------------------------------------------------------------- 约束 ----

def test_status_check_constraint_rejects_invalid_value(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO announcements (uid, source, locale, article_id, status)
            VALUES ('bad-uid', 'Bitunix', 'EN', '9999', 'not-a-real-status')
            """
        )
