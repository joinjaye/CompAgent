"""src/analysis/batch.py 单测：批次 PK 幂等、locale 复用判断的满足/不满足场景。
全部离线，临时 SQLite 库。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.analysis.batch import (
    can_derive_from_en,
    compute_batch_id,
    get_batch_uids,
    list_batch_keys,
)


def test_compute_batch_id_is_deterministic_and_idempotent():
    id1 = compute_batch_id("Bitunix", "campaign", "EN", "2026-07-14")
    id2 = compute_batch_id("Bitunix", "campaign", "EN", "2026-07-14")
    assert id1 == id2
    assert len(id1) == 64  # sha256 hex


def test_compute_batch_id_differs_by_any_component():
    base = compute_batch_id("Bitunix", "campaign", "EN", "2026-07-14")
    assert base != compute_batch_id("Weex", "campaign", "EN", "2026-07-14")
    assert base != compute_batch_id("Bitunix", "product", "EN", "2026-07-14")
    assert base != compute_batch_id("Bitunix", "campaign", "FR", "2026-07-14")
    assert base != compute_batch_id("Bitunix", "campaign", "EN", "2026-07-15")


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE announcements (
            uid TEXT PRIMARY KEY, group_id TEXT, source TEXT, locale TEXT,
            category TEXT, status TEXT, fetched_at TEXT, duplicate_of TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE insights (
            id TEXT PRIMARY KEY, batch_date TEXT, source TEXT, category TEXT,
            locale TEXT, related_uids TEXT
        )
        """
    )
    yield c
    c.close()


def _insert_ann(conn, uid, group_id, source, locale, category, status, fetched_at="2026-07-14T00:00:00Z"):
    conn.execute(
        "INSERT INTO announcements (uid, group_id, source, locale, category, status, fetched_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (uid, group_id, source, locale, category, status, fetched_at),
    )


def _insert_insight(conn, source, category, locale, batch_date, related_uids):
    conn.execute(
        "INSERT INTO insights (id, batch_date, source, category, locale, related_uids) VALUES (?,?,?,?,?,?)",
        (compute_batch_id(source, category, locale, batch_date), batch_date, source, category, locale,
         json.dumps(related_uids)),
    )


def test_can_derive_from_en_returns_none_for_en_itself(conn):
    assert can_derive_from_en(conn, "Bitunix", "campaign", "EN", "2026-07-14") is None


def test_can_derive_from_en_returns_none_when_en_insight_missing(conn):
    _insert_ann(conn, "u_fr1", "g1", "Bitunix", "FR", "campaign", "new")
    conn.commit()
    assert can_derive_from_en(conn, "Bitunix", "campaign", "FR", "2026-07-14") is None


def test_can_derive_from_en_satisfied_when_all_groups_covered(conn):
    _insert_ann(conn, "u_en1", "g1", "Bitunix", "EN", "campaign", "new")
    _insert_ann(conn, "u_fr1", "g1", "Bitunix", "FR", "campaign", "new")
    _insert_insight(conn, "Bitunix", "campaign", "EN", "2026-07-14", ["u_en1"])
    conn.commit()

    result = can_derive_from_en(conn, "Bitunix", "campaign", "FR", "2026-07-14")
    assert result == compute_batch_id("Bitunix", "campaign", "EN", "2026-07-14")


def test_can_derive_from_en_not_satisfied_with_region_exclusive_entry(conn):
    # FR 批次里 g2 是地区独占，EN 批次里没有对应条目 -> 不能复用
    _insert_ann(conn, "u_en1", "g1", "Bitunix", "EN", "campaign", "new")
    _insert_ann(conn, "u_fr1", "g1", "Bitunix", "FR", "campaign", "new")
    _insert_ann(conn, "u_fr2", "g2", "Bitunix", "FR", "campaign", "new")
    _insert_insight(conn, "Bitunix", "campaign", "EN", "2026-07-14", ["u_en1"])
    conn.commit()

    result = can_derive_from_en(conn, "Bitunix", "campaign", "FR", "2026-07-14")
    assert result is None


def test_can_derive_from_en_not_satisfied_when_locale_is_only_subset(conn):
    _insert_ann(conn, "u_en1", "g1", "Bitunix", "EN", "campaign", "new")
    _insert_ann(conn, "u_en2", "g2", "Bitunix", "EN", "campaign", "new")
    _insert_ann(conn, "u_fr1", "g1", "Bitunix", "FR", "campaign", "new")
    _insert_insight(conn, "Bitunix", "campaign", "EN", "2026-07-14", ["u_en1", "u_en2"])
    conn.commit()

    assert can_derive_from_en(conn, "Bitunix", "campaign", "FR", "2026-07-14") is None


def test_can_derive_from_en_returns_none_when_no_current_uids(conn):
    _insert_insight(conn, "Bitunix", "campaign", "EN", "2026-07-14", ["u_en1"])
    conn.commit()
    assert can_derive_from_en(conn, "Bitunix", "campaign", "FR", "2026-07-14") is None


def test_get_batch_uids_filters_status_and_date(conn):
    _insert_ann(conn, "u1", "g1", "Bitunix", "EN", "campaign", "new", "2026-07-14T00:00:00Z")
    _insert_ann(conn, "u2", "g2", "Bitunix", "EN", "campaign", "unchanged", "2026-07-14T00:00:00Z")
    _insert_ann(conn, "u3", "g3", "Bitunix", "EN", "campaign", "changed", "2026-07-13T00:00:00Z")
    conn.commit()

    uids = get_batch_uids(conn, "Bitunix", "campaign", "EN", "2026-07-14")
    assert uids == ["u1"]


def test_list_batch_keys_orders_en_first_and_skips_other_category(conn):
    _insert_ann(conn, "u1", "g1", "Bitunix", "FR", "campaign", "new")
    _insert_ann(conn, "u2", "g2", "Bitunix", "EN", "campaign", "new")
    _insert_ann(conn, "u3", "g3", "Bitunix", "EN", "other", "new")
    conn.commit()

    keys = list_batch_keys(conn, ("Bitunix",), "2026-07-14")
    assert len(keys) == 2
    assert keys[0].locale == "EN"
    assert keys[1].locale == "FR"
    assert all(k.category != "other" for k in keys)
