import sqlite3

from src.db.connection import init_db
from src.db.operations import upsert_announcement
from src.sinks.feishu_business_tables import (
    BusinessTableCredentials,
    LISTING_FIELD_SPECS,
    build_business_rows,
    sync_business_tables,
)


def _conn(tmp_path):
    path = tmp_path / "business.db"
    init_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_builds_three_tables_from_only_selected_day_new_changed(tmp_path):
    conn = _conn(tmp_path)
    for article_id, category, date in [
        ("c1", "campaign", "2026-07-21T01:00:00Z"),
        ("p1", "product", "2026-07-21T02:00:00Z"),
        ("l1", "listing", "2026-07-21T03:00:00Z"),
        ("old", "campaign", "2026-07-20T03:00:00Z"),
    ]:
        upsert_announcement(
            conn, source="Bitunix", locale="EN", article_id=article_id,
            title=article_id, content=article_id, category=category,
            post_time=date, fetched_at=date, group_id=f"g-{article_id}",
        )
    conn.commit()

    rows = build_business_rows(conn, "2026-07-21")

    assert [r["uid"] for r in rows["campaign"]]
    assert len(rows["campaign"]) == len(rows["product"]) == len(rows["listing"]) == 1
    assert rows["product"][0]["status"] == "new"
    assert "ai_summary" not in rows["listing"][0]
    assert "zmx_comparison" not in rows["listing"][0]
    assert all(name not in {"ai_summary", "zmx_comparison"} for name, _ in LISTING_FIELD_SPECS)
    conn.close()


def test_product_table_status_uses_business_update_kind(tmp_path):
    conn = _conn(tmp_path)
    upsert_announcement(
        conn, source="Bitunix", locale="EN", article_id="p-update",
        title="Adjustment to Funding Rate Settlement Frequency", content="body",
        category="product", post_time="2026-07-21T01:00:00Z",
        fetched_at="2026-07-21T01:00:00Z", group_id="g-p-update",
    )
    conn.commit()

    row = build_business_rows(conn, "2026-07-21")["product"][0]

    assert row["change_type"] == "Rule Updated"
    assert row["status"] == "updated"
    conn.close()


def test_three_table_dry_run_never_requires_remote_ids(tmp_path):
    conn = _conn(tmp_path)
    upsert_announcement(
        conn, source="Bitunix", locale="EN", article_id="c1", title="Campaign",
        content="body", category="campaign", post_time="2026-07-21T01:00:00Z",
        fetched_at="2026-07-21T01:00:00Z", group_id="g-c1",
    )
    conn.commit()
    creds = BusinessTableCredentials(None, None, None, None, None, None, None, None)

    reports = sync_business_tables(conn, creds, "2026-07-21", dry_run=True)

    assert set(reports) == {"campaign", "product", "listing"}
    assert reports["campaign"].dry_run_rows == 1
    conn.close()
