"""将当日业务结果同步到 Campaign / Product / Listing & Delisting 三张飞书表。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Optional

from src.dashboard.export_data import (
    _load_article_index,
    _load_zmx_catalog_index,
    _load_zmx_counterpart_index,
    _merge_localized_rows,
    build_category_section,
)
from src.db.connection import DEFAULT_DB_PATH, connect
from src.sinks.feishu_bitable import (
    BATCH_CREATE_SIZE,
    FIELD_TYPE_TEXT,
    SyncReport,
    _chunks,
    _index_existing_records,
    _request,
    _sync_table,
    ensure_fields,
    get_table_fields,
    load_env,
    rename_field,
)


COMMON_FIELDS = [
    ("uid", FIELD_TYPE_TEXT), ("group_id", FIELD_TYPE_TEXT),
    ("source", FIELD_TYPE_TEXT), ("markets", FIELD_TYPE_TEXT),
    ("category", FIELD_TYPE_TEXT), ("status", FIELD_TYPE_TEXT),
    ("title", FIELD_TYPE_TEXT), ("url", FIELD_TYPE_TEXT),
    ("fetched_at", FIELD_TYPE_TEXT), ("update_time", FIELD_TYPE_TEXT),
    ("post_time", FIELD_TYPE_TEXT),
]
CAMPAIGN_FIELD_SPECS = COMMON_FIELDS + [
    ("activity_type", FIELD_TYPE_TEXT), ("reward", FIELD_TYPE_TEXT),
    ("target_users", FIELD_TYPE_TEXT), ("start_date", FIELD_TYPE_TEXT),
    ("end_date", FIELD_TYPE_TEXT), ("ai_summary", FIELD_TYPE_TEXT),
    ("zmx_comparison", FIELD_TYPE_TEXT), ("zmx_detail", FIELD_TYPE_TEXT),
]
PRODUCT_FIELD_SPECS = COMMON_FIELDS + [
    ("product_category", FIELD_TYPE_TEXT), ("feature", FIELD_TYPE_TEXT),
    ("change_type", FIELD_TYPE_TEXT), ("ai_summary", FIELD_TYPE_TEXT),
    ("zmx_comparison", FIELD_TYPE_TEXT), ("zmx_detail", FIELD_TYPE_TEXT),
]
LISTING_FIELD_SPECS = COMMON_FIELDS + [
    ("token", FIELD_TYPE_TEXT), ("trading_pair", FIELD_TYPE_TEXT),
    ("listing_type", FIELD_TYPE_TEXT), ("listing_status", FIELD_TYPE_TEXT),
    ("token_category", FIELD_TYPE_TEXT), ("effective_time_from_article", FIELD_TYPE_TEXT),
]

DIFF_LABELS = {
    "missing": "未检索到同类", "diff": "已有同类 · 机制不同",
    "broad": "已有同类型 · 粗粒度", "na": "未进行对比",
}


@dataclass
class BusinessTableCredentials:
    app_id: Optional[str]
    app_secret: Optional[str]
    campaign_app_token: Optional[str]
    campaign_table_id: Optional[str]
    product_app_token: Optional[str]
    product_table_id: Optional[str]
    listing_app_token: Optional[str]
    listing_table_id: Optional[str]

    def validate(self) -> None:
        values = {
            "FEISHU_APP_ID": self.app_id,
            "FEISHU_APP_SECRET": self.app_secret,
            "FEISHU_CAMPAIGN_APP_TOKEN": self.campaign_app_token,
            "FEISHU_CAMPAIGN_TABLE_ID": self.campaign_table_id,
            "FEISHU_PRODUCT_APP_TOKEN": self.product_app_token,
            "FEISHU_PRODUCT_TABLE_ID": self.product_table_id,
            "FEISHU_LISTING_APP_TOKEN": self.listing_app_token,
            "FEISHU_LISTING_TABLE_ID": self.listing_table_id,
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError("缺少飞书三表配置：" + ", ".join(missing))


def load_business_credentials() -> BusinessTableCredentials:
    env = load_env()
    return BusinessTableCredentials(
        app_id=env.get("FEISHU_APP_ID"), app_secret=env.get("FEISHU_APP_SECRET"),
        campaign_app_token=env.get("FEISHU_CAMPAIGN_APP_TOKEN"),
        campaign_table_id=env.get("FEISHU_CAMPAIGN_TABLE_ID"),
        product_app_token=env.get("FEISHU_PRODUCT_APP_TOKEN"),
        product_table_id=env.get("FEISHU_PRODUCT_TABLE_ID"),
        listing_app_token=env.get("FEISHU_LISTING_APP_TOKEN"),
        listing_table_id=env.get("FEISHU_LISTING_TABLE_ID"),
    )


def _source_rows(
    conn: sqlite3.Connection, category: str, date: str, *, latest_only: bool = True,
) -> list[dict[str, Any]]:
    article_index = _load_article_index(conn)
    catalog = _load_zmx_catalog_index(conn)
    counterpart_uids = {
        item["zmx_counterpart_uids"][0] for item in article_index.values()
        if item.get("zmx_counterpart_uids")
    }
    counterparts = _load_zmx_counterpart_index(conn, counterpart_uids)
    return _merge_localized_rows(build_category_section(
        conn, category, date, article_index,
        zmx_catalog_index=catalog, zmx_counterpart_index=counterparts,
        latest_only=latest_only,
    ))


def build_business_rows(
    conn: sqlite3.Connection, date: str, *, full_history: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    latest_only = not full_history
    campaign = _source_rows(conn, "campaign", date, latest_only=latest_only)
    product = _source_rows(conn, "product", date, latest_only=latest_only)
    listing = _source_rows(conn, "listing", date, latest_only=latest_only) + _source_rows(
        conn, "delisting", date, latest_only=latest_only,
    )

    def common(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "uid": row.get("uid"), "group_id": row.get("group_id"),
            "source": row.get("source"), "markets": " / ".join(row.get("markets") or []),
            "category": row.get("category"), "status": row.get("status"),
            "title": row.get("title"), "url": row.get("url"),
            "fetched_at": row.get("fetched_at"), "update_time": row.get("update_time"),
            "post_time": row.get("post_time"),
        }

    return {
        "campaign": [{**common(r), "activity_type": r.get("mechanism_type"),
            "reward": r.get("reward_range"), "target_users": r.get("target_users"),
            "start_date": r.get("start_date"), "end_date": r.get("end_date"),
            "ai_summary": r.get("description"), "zmx_comparison": DIFF_LABELS.get(r.get("diff_tag"), "未进行对比"),
            "zmx_detail": r.get("diff_detail")} for r in campaign],
        "product": [{**common(r),
            # Product 表对业务用户展示产品语义，不暴露“首次被采集到”的底层
            # status=new。调整/升级类公告即使首次抓到，也应标为 updated。
            "status": "new" if r.get("update_kind") == "New Product" else "updated",
            "product_category": r.get("product_category"),
            "feature": r.get("feature"), "change_type": r.get("update_kind") or r.get("change_kind"),
            "ai_summary": r.get("description"), "zmx_comparison": DIFF_LABELS.get(r.get("diff_tag"), "未进行对比"),
            "zmx_detail": r.get("diff_detail")} for r in product],
        "listing": [{**common(r), "token": r.get("token_symbol"),
            "trading_pair": r.get("trading_pair"), "listing_type": r.get("listing_type"),
            "listing_status": r.get("listing_status"), "token_category": r.get("token_category"),
            "effective_time_from_article": r.get("launch_time")} for r in listing],
    }


def _table_config(creds: BusinessTableCredentials) -> dict[str, tuple[str, str, list[tuple[str, int]]]]:
    return {
        "campaign": (creds.campaign_app_token or "", creds.campaign_table_id or "", CAMPAIGN_FIELD_SPECS),
        "product": (creds.product_app_token or "", creds.product_table_id or "", PRODUCT_FIELD_SPECS),
        "listing": (creds.listing_app_token or "", creds.listing_table_id or "", LISTING_FIELD_SPECS),
    }


def sync_business_tables(conn: sqlite3.Connection, creds: BusinessTableCredentials, date: str,
                         table: str = "all", dry_run: bool = False,
                         full_history: bool = False, prune: bool = False) -> dict[str, SyncReport]:
    rows_by_table = build_business_rows(conn, date, full_history=full_history)
    reports: dict[str, SyncReport] = {}
    for name, (app_token, table_id, specs) in _table_config(creds).items():
        if table not in ("all", name):
            continue
        # 旧字段名 launch_time 容易被理解成平台记录时间。它实际是从公告正文
        # 确定性提取的交易开放/生效时间；原地改名可保留历史单元格数据。
        if name == "listing" and not dry_run:
            fields = get_table_fields(app_token, table_id, creds)
            by_name = {field["field_name"]: field for field in fields}
            if "launch_time" in by_name and "effective_time_from_article" not in by_name:
                old = by_name["launch_time"]
                rename_field(
                    app_token, table_id, old["field_id"],
                    "effective_time_from_article", old["type"], creds,
                )
        reports[name] = _sync_table(
            conn, rows_by_table[name], key_column="uid", field_specs=specs,
            app_token=app_token, table_id=table_id,
            target=f"bitable_{name}", creds=creds, dry_run=dry_run,
            # 三表同步是外部幂等写；不因另一个分析进程暂时持有 SQLite 写锁而中断。
            log_actions=False,
            clear_missing_text=True,
        )
        if prune and not dry_run:
            existing = _index_existing_records(app_token, table_id, "uid", creds)
            desired_uids = {row["uid"] for row in rows_by_table[name]}
            stale_ids = [
                item["record_id"] for uid, item in existing.items() if uid not in desired_uids
            ]
            for batch in _chunks(stale_ids, BATCH_CREATE_SIZE):
                _request(
                    "POST",
                    f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete",
                    app_id=creds.app_id, app_secret=creds.app_secret,
                    json_body={"records": batch},
                )
                time.sleep(0.6)
            reports[name].deleted = len(stale_ids)
    return reports


def _all_record_ids(app_token: str, table_id: str, creds: BusinessTableCredentials) -> list[str]:
    ids: list[str] = []
    page_token = None
    while True:
        params: dict[str, Any] = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        data = _request("GET", f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            app_id=creds.app_id, app_secret=creds.app_secret, params=params)["data"]
        ids.extend(item["record_id"] for item in data.get("items", []))
        if not data.get("has_more"):
            return ids
        page_token = data.get("page_token")


def reset_business_tables(creds: BusinessTableCredentials) -> None:
    """清空三表记录并删除非主字段，然后按新业务 schema 重建。"""
    display_names = {"campaign": "Campaign", "product": "Product", "listing": "Listing & Delisting"}
    for name, (app_token, table_id, specs) in _table_config(creds).items():
        for batch in _chunks(_all_record_ids(app_token, table_id, creds), BATCH_CREATE_SIZE):
            _request("POST", f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete",
                app_id=creds.app_id, app_secret=creds.app_secret, json_body={"records": batch})
            time.sleep(0.6)
        fields = get_table_fields(app_token, table_id, creds)
        for field in fields[1:]:
            _request("DELETE", f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field['field_id']}",
                app_id=creds.app_id, app_secret=creds.app_secret)
            time.sleep(0.6)
        # 删除后仅剩主字段，ensure_fields 会安全改名并补齐其余字段。
        ensure_fields(app_token, table_id, specs, creds)
        _request("PATCH", f"/bitable/v1/apps/{app_token}/tables/{table_id}",
            app_id=creds.app_id, app_secret=creds.app_secret,
            json_body={"name": display_names[name]})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--date", required=True)
    parser.add_argument("--table", choices=["campaign", "product", "listing", "all"], default="all")
    parser.add_argument("--scope", choices=["date", "all"], default="date",
                        help="date=只同步指定批次；all=回写全部历史业务记录")
    parser.add_argument("--prune", action="store_true",
                        help="删除不在本次同步范围内的远端记录（daily 表仅保留当天）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reset-schema", action="store_true")
    parser.add_argument("--confirm-reset", default="")
    args = parser.parse_args()
    creds = load_business_credentials()
    if not args.dry_run:
        creds.validate()
    if args.reset_schema:
        if args.confirm_reset != "RESET_THREE_BUSINESS_TABLES":
            raise SystemExit("重置被拒绝：必须传 --confirm-reset RESET_THREE_BUSINESS_TABLES")
        reset_business_tables(creds)
    conn = connect(args.db_path)
    try:
        reports = sync_business_tables(
            conn, creds, args.date, args.table, args.dry_run,
            full_history=args.scope == "all",
            prune=args.prune,
        )
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()
    for name, report in reports.items():
        print(f"{name}: created={report.created} updated={report.updated} skipped={report.skipped} deleted={report.deleted} failed={report.failed} dry_run_rows={report.dry_run_rows}")
        if report.failed:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
