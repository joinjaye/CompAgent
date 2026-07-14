"""地区独占标记单测：离线临时库，验证判断按"该源实际配置的 locale 集合"来看，
不是全局 locale 集合；EN-only 不标记，非 EN 单一 locale 才标记。"""

from __future__ import annotations

import pytest

from src.db.connection import get_connection, init_db
from src.db.operations import upsert_announcement
from src.pipeline.region import apply_region_exclusive, compute_region_exclusive

SOURCE_LOCALES = {"bitunix": {"EN", "FR", "ID"}, "weex": {"EN", "FR"}}


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _seed(conn, source, locale, article_id, group_id):
    upsert_announcement(
        conn,
        source=source,
        locale=locale,
        article_id=article_id,
        title=f"{source} {locale} {article_id}",
        content="content",
        group_id=group_id,
    )


def test_en_only_group_not_exclusive(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, "Bitunix", "EN", "1", "bitunix_1")
        flags = compute_region_exclusive(conn, SOURCE_LOCALES, sources=("Bitunix",))
        assert flags["bitunix_1"] is False


def test_single_non_en_locale_is_exclusive(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, "Weex", "FR", "1", "weex_1")
        flags = compute_region_exclusive(conn, SOURCE_LOCALES, sources=("Weex",))
        assert flags["weex_1"] is True


def test_multi_locale_group_not_exclusive(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, "Bitunix", "EN", "1", "bitunix_1")
        _seed(conn, "Bitunix", "FR", "1", "bitunix_1")
        flags = compute_region_exclusive(conn, SOURCE_LOCALES, sources=("Bitunix",))
        assert flags["bitunix_1"] is False


def test_apply_writes_is_region_exclusive_column(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, "Bitunix", "EN", "1", "bitunix_1")  # not exclusive
        _seed(conn, "Weex", "FR", "2", "weex_2")  # exclusive
        _seed(conn, "Weex", "EN", "3", "weex_3")
        _seed(conn, "Weex", "FR", "3", "weex_3")  # multi-locale, not exclusive

        report = apply_region_exclusive(conn, SOURCE_LOCALES, sources=("Bitunix", "Weex"))
        conn.commit()

        assert report.exclusive_groups == 1
        assert report.exclusive_rows_updated == 1
        assert report.non_exclusive_rows_updated == 3

        rows = {r["article_id"] + r["source"]: r["is_region_exclusive"] for r in conn.execute(
            "SELECT source, article_id, is_region_exclusive FROM announcements"
        )}
        assert rows["1Bitunix"] == 0
        assert rows["2Weex"] == 1
        assert rows["3Weex"] == 0
