"""跨语言归组一致性防御性扫描单测：离线临时库，手工构造异常场景验证能被扫出来，
也验证正常场景不会被误报。"""

from __future__ import annotations

import pytest

from src.db.connection import get_connection, init_db
from src.db.operations import upsert_announcement
from src.pipeline.grouping import scan_group_consistency


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


def test_consistent_groups_pass(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, "Bitunix", "EN", "1", "bitunix_1")
        _seed(conn, "Bitunix", "FR", "1", "bitunix_1")
        _seed(conn, "Bitunix", "ID", "1", "bitunix_1")

        report = scan_group_consistency(conn, {"bitunix": {"EN", "FR", "ID"}}, sources=("Bitunix",))
        assert report.ok
        assert report.duplicate_group_id == []
        assert report.locale_overflow == []


def test_duplicate_group_id_detected(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, "Bitunix", "EN", "1", "bitunix_1")
        _seed(conn, "Bitunix", "FR", "1", "bitunix_1_WRONG")  # 拼接 bug 模拟

        report = scan_group_consistency(conn, {"bitunix": {"EN", "FR", "ID"}}, sources=("Bitunix",))
        assert not report.ok
        assert len(report.duplicate_group_id) == 1
        source, article_id, gids = report.duplicate_group_id[0]
        assert source == "Bitunix"
        assert article_id == "1"
        assert set(gids) == {"bitunix_1", "bitunix_1_WRONG"}


def test_locale_overflow_detected(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, "Bitunix", "EN", "1", "bitunix_1")
        _seed(conn, "Bitunix", "FR", "1", "bitunix_1")
        _seed(conn, "Bitunix", "ID", "1", "bitunix_1")

        # 故意只允许 2 个 locale，制造"超过配置的 locale 数"异常
        report = scan_group_consistency(conn, {"bitunix": {"EN", "FR"}}, sources=("Bitunix",))
        assert not report.ok
        assert len(report.locale_overflow) == 1
        source, group_id, actual_locales, allowed = report.locale_overflow[0]
        assert source == "Bitunix"
        assert group_id == "bitunix_1"
        assert set(actual_locales) == {"EN", "FR", "ID"}
        assert allowed == {"EN", "FR"}
