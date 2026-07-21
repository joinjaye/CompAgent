"""重复公告检测单测：判重口径是「同 source + locale + title + content」，不是只看
content_hash——覆盖两个方向：真重复要被合并，标题不同的"假重复"（源站复用模板正文）
不能被合并。"""

from __future__ import annotations

import pytest

from src.db.connection import get_connection, init_db
from src.db.operations import upsert_announcement
from src.pipeline.dedup import apply_dedup, find_duplicate_clusters


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _seed(conn, source, locale, article_id, title, content, post_time):
    upsert_announcement(
        conn,
        source=source,
        locale=locale,
        article_id=article_id,
        title=title,
        content=content,
        post_time=post_time,
    )


def test_same_title_and_content_is_flagged_as_duplicate(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, "BingX", "EN", "1", "Maintenance margin update", "same body", "2026-07-20T00:44:00Z")
        _seed(conn, "BingX", "EN", "2", "Maintenance margin update", "same body", "2026-07-20T02:41:00Z")

        clusters = find_duplicate_clusters(conn)
        assert len(clusters) == 1
        cluster = clusters[0]
        # 更早的 post_time（article_id=1）是规范行
        assert cluster.duplicate_uids == [conn.execute(
            "SELECT uid FROM announcements WHERE article_id='2'"
        ).fetchone()["uid"]]


def test_same_content_different_title_is_not_flagged(db_path):
    """源站自己的 CMS 复用模板正文、标题不同（真实不同事件）时不能被合并——
    这是 2026-07-21 真实数据核查发现的假重复模式，见 src/pipeline/dedup.py 顶部说明。"""
    with get_connection(db_path) as conn:
        _seed(conn, "Zoomex", "EN", "1", "APT and 1000LUNC contracts available", "same templated body", "2022-10-24T00:00:00Z")
        _seed(conn, "Zoomex", "EN", "2", "LDO and CEEK contracts available", "same templated body", "2022-08-01T00:00:00Z")

        clusters = find_duplicate_clusters(conn)
        assert clusters == []


def test_empty_content_rows_are_never_clustered(db_path):
    """多篇正文均为空字符串的文章不能互相"撞车"成一个假聚类。"""
    with get_connection(db_path) as conn:
        _seed(conn, "Zoomex", "EN", "1", "Title A", "", "2026-01-01T00:00:00Z")
        _seed(conn, "Zoomex", "EN", "2", "Title B", "", "2026-01-02T00:00:00Z")

        clusters = find_duplicate_clusters(conn)
        assert clusters == []


def test_apply_dedup_writes_duplicate_of_and_is_idempotent(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, "BingX", "EN", "1", "Same notice", "same body", "2026-07-20T00:44:00Z")
        _seed(conn, "BingX", "EN", "2", "Same notice", "same body", "2026-07-20T02:41:00Z")

        report = apply_dedup(conn)
        conn.commit()
        assert report.clusters_found == 1
        assert report.rows_marked == 1

        canonical_uid = conn.execute("SELECT uid FROM announcements WHERE article_id='1'").fetchone()["uid"]
        dup_row = conn.execute("SELECT duplicate_of FROM announcements WHERE article_id='2'").fetchone()
        assert dup_row["duplicate_of"] == canonical_uid

        # 幂等：再跑一次不应该再产出新聚类（已标记的行不参与新一轮分组）
        second = apply_dedup(conn)
        conn.commit()
        assert second.clusters_found == 0
        assert second.rows_marked == 0


def test_third_duplicate_arriving_later_points_to_same_canonical(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, "BingX", "EN", "1", "Same notice", "same body", "2026-07-20T00:00:00Z")
        _seed(conn, "BingX", "EN", "2", "Same notice", "same body", "2026-07-20T01:00:00Z")
        apply_dedup(conn)
        conn.commit()
        canonical_uid = conn.execute("SELECT uid FROM announcements WHERE article_id='1'").fetchone()["uid"]

        # 第三条重复稍后才出现
        _seed(conn, "BingX", "EN", "3", "Same notice", "same body", "2026-07-20T02:00:00Z")
        report = apply_dedup(conn)
        conn.commit()

        assert report.clusters_found == 1
        dup_row = conn.execute("SELECT duplicate_of FROM announcements WHERE article_id='3'").fetchone()
        assert dup_row["duplicate_of"] == canonical_uid
