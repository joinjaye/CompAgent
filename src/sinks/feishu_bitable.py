"""飞书多维表同步（Phase 5）：把 announcements / insights 两张表同步到飞书 Bitable。

SQLite 是唯一真相源，飞书只是同步出去的业务视图（CLAUDE.md 核心约束 1）——本模块只读
SQLite、只写飞书，不反向回写任何 SQLite 列。

两张表分别位于两个独立的飞书多维表 app（app_token 不同，见 `.env` 里
FEISHU_ANNOUNCEMENTS_APP_TOKEN / FEISHU_INSIGHTS_APP_TOKEN），不是同一个 base 下的
两张子表。domain 固定用 open.larksuite.com（sg.larksuite.com 账号），不是
open.feishu.cn（那个域名只服务中国大陆账号）。

复用 src/collectors/http.py 的 fetch_json()（已有指数退避重试），不引入新的 HTTP 库；
飞书 API 的"业务错误"体现为 HTTP 200 + body.code != 0，不是 HttpError，需要在这一层
单独判断、按错误码决定是刷新 token 重试还是直接失败。

幂等：以 uid（announcements）/ id（insights）作为业务主键，先一次性拉全表已有记录建
本地索引（不用飞书的按条件过滤查询接口，避免依赖其 filter query 语法细节，几千条规模
一次性拉取也不算贵），再逐条比较决定 create / update / skip。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.collectors.http import HttpError, fetch_json
from src.db.connection import DEFAULT_DB_PATH, connect
from src.db.operations import utcnow_iso

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

API_BASE = "https://open.larksuite.com/open-apis"

MAX_RETRIES = 3
BACKOFF_BASE_S = 1.0
RATE_LIMIT_SLEEP_S = 0.6  # 单条请求之间的节流，QPS 上限约 100/分钟见 CLAUDE.md 任务说明
BATCH_CREATE_SIZE = 500
TEXT_FIELD_LIMIT = 50000
TOKEN_EXPIRE_SAFETY_S = 60

# tenant_access_token 失效/过期相关错误码：命中时强制刷新 token 重试一次，不是直接失败
TOKEN_INVALID_CODES = {99991661, 99991663, 99991664}

FIELD_TYPE_TEXT = 1
FIELD_TYPE_NUMBER = 2
FIELD_TYPE_CHECKBOX = 7

# (字段名, 字段类型)，顺序即 CLAUDE.md schema 表格顺序，业务主键排第一
ANNOUNCEMENTS_FIELD_SPECS: list[tuple[str, int]] = [
    ("uid", FIELD_TYPE_TEXT),
    ("group_id", FIELD_TYPE_TEXT),
    ("source", FIELD_TYPE_TEXT),
    ("locale", FIELD_TYPE_TEXT),
    ("article_id", FIELD_TYPE_TEXT),
    ("url", FIELD_TYPE_TEXT),
    ("title", FIELD_TYPE_TEXT),
    ("content", FIELD_TYPE_TEXT),
    ("raw_category", FIELD_TYPE_TEXT),
    ("content_hash", FIELD_TYPE_TEXT),
    ("post_time", FIELD_TYPE_TEXT),
    ("update_time", FIELD_TYPE_TEXT),
    ("fetched_at", FIELD_TYPE_TEXT),
    ("status", FIELD_TYPE_TEXT),
    ("category", FIELD_TYPE_TEXT),
    ("is_region_exclusive", FIELD_TYPE_CHECKBOX),
    ("push_status", FIELD_TYPE_TEXT),
    ("source_endpoint", FIELD_TYPE_TEXT),
]

INSIGHTS_FIELD_SPECS: list[tuple[str, int]] = [
    ("id", FIELD_TYPE_TEXT),
    ("batch_date", FIELD_TYPE_TEXT),
    ("source", FIELD_TYPE_TEXT),
    ("category", FIELD_TYPE_TEXT),
    ("locale", FIELD_TYPE_TEXT),
    ("article_count", FIELD_TYPE_NUMBER),
    ("related_uids", FIELD_TYPE_TEXT),
    ("is_locale_derived", FIELD_TYPE_CHECKBOX),
    ("derived_from_id", FIELD_TYPE_TEXT),
    ("summary", FIELD_TYPE_TEXT),
    ("articles_analysis", FIELD_TYPE_TEXT),
    ("zmx_diff", FIELD_TYPE_TEXT),
    ("diff_type", FIELD_TYPE_TEXT),
    ("priority", FIELD_TYPE_TEXT),
    ("zmx_evidence_uids", FIELD_TYPE_TEXT),
    ("prompt_version", FIELD_TYPE_TEXT),
    ("llm_tokens_used", FIELD_TYPE_NUMBER),
    ("created_at", FIELD_TYPE_TEXT),
    ("updated_at", FIELD_TYPE_TEXT),
]


class FeishuApiError(Exception):
    """飞书 API 请求最终失败（网络重试耗尽，或业务 code != 0 且不是可重试的场景）。"""


# ============================================================
# 凭证加载：.env（不引入 python-dotenv，跟 src/analysis/config.py 同样的理由——
# .env 格式极简单，手写解析足够，见该文件顶部注释）
# ============================================================


def _parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def load_env(path: Path | str = ENV_PATH) -> dict[str, str]:
    """.env 文件内容 + 真实环境变量的合并结果，真实环境变量优先。"""
    file_values = _parse_env_file(Path(path))
    merged = dict(file_values)
    for key in file_values:
        if key in os.environ:
            merged[key] = os.environ[key]
    return merged


@dataclass
class FeishuCredentials:
    app_id: Optional[str]
    app_secret: Optional[str]
    announcements_app_token: Optional[str]
    announcements_table_id: Optional[str]
    insights_app_token: Optional[str]
    insights_table_id: Optional[str]

    def validate(self) -> None:
        missing = [
            name
            for name, val in (
                ("FEISHU_APP_ID", self.app_id),
                ("FEISHU_APP_SECRET", self.app_secret),
                ("FEISHU_ANNOUNCEMENTS_APP_TOKEN", self.announcements_app_token),
                ("FEISHU_ANNOUNCEMENTS_TABLE_ID", self.announcements_table_id),
                ("FEISHU_INSIGHTS_APP_TOKEN", self.insights_app_token),
                ("FEISHU_INSIGHTS_TABLE_ID", self.insights_table_id),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(f"缺少飞书凭证环境变量：{', '.join(missing)}（见 config/.env.example）")


def load_feishu_credentials(env_path: Path | str = ENV_PATH) -> FeishuCredentials:
    env = load_env(env_path)
    return FeishuCredentials(
        app_id=env.get("FEISHU_APP_ID"),
        app_secret=env.get("FEISHU_APP_SECRET"),
        announcements_app_token=env.get("FEISHU_ANNOUNCEMENTS_APP_TOKEN"),
        announcements_table_id=env.get("FEISHU_ANNOUNCEMENTS_TABLE_ID"),
        insights_app_token=env.get("FEISHU_INSIGHTS_APP_TOKEN"),
        insights_table_id=env.get("FEISHU_INSIGHTS_TABLE_ID"),
    )


# ============================================================
# 底层请求：token 获取/缓存/刷新 + 业务错误码处理
# ============================================================

_token_cache: dict[str, tuple[str, float]] = {}


def _get_tenant_access_token(app_id: str, app_secret: str, *, force_refresh: bool = False) -> str:
    now = time.time()
    if not force_refresh:
        cached = _token_cache.get(app_id)
        if cached and cached[1] > now + TOKEN_EXPIRE_SAFETY_S:
            return cached[0]

    resp = fetch_json(
        f"{API_BASE}/auth/v3/tenant_access_token/internal",
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
        body=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
    )
    if resp.get("code") != 0:
        raise FeishuApiError(f"获取 tenant_access_token 失败：code={resp.get('code')} msg={resp.get('msg')}")
    token = resp["tenant_access_token"]
    expire_s = resp.get("expire", 7200)
    _token_cache[app_id] = (token, now + expire_s)
    return token


def _request(
    method: str,
    path: str,
    *,
    app_id: str,
    app_secret: str,
    json_body: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, Any]] = None,
    max_retries: int = MAX_RETRIES,
) -> dict[str, Any]:
    """发一次飞书 API 请求，返回解析后的 JSON body（已确认 code==0）。

    网络错误交给 fetch_json 内部的指数退避处理；这里额外处理飞书自己的业务错误码：
    token 失效/过期（TOKEN_INVALID_CODES）强制刷新后重试，其它非 0 错误码按指数退避
    重试到 max_retries 次仍失败才抛出。
    """
    url = f"{API_BASE}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    body = json.dumps(json_body).encode("utf-8") if json_body is not None else None

    last_error: Optional[str] = None
    force_refresh = False
    for attempt in range(max_retries):
        token = _get_tenant_access_token(app_id, app_secret, force_refresh=force_refresh)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        try:
            resp = fetch_json(url, method=method, headers=headers, body=body)
        except HttpError as e:
            last_error = str(e)
            force_refresh = False
            if attempt < max_retries - 1:
                time.sleep(BACKOFF_BASE_S * (2**attempt))
                continue
            raise FeishuApiError(f"{method} {path} 请求失败：{last_error}") from e

        code = resp.get("code")
        if code == 0:
            return resp

        last_error = f"code={code} msg={resp.get('msg')}"
        if code in TOKEN_INVALID_CODES:
            force_refresh = True
        else:
            force_refresh = False
        if attempt < max_retries - 1:
            time.sleep(BACKOFF_BASE_S * (2**attempt))
            continue
        raise FeishuApiError(f"{method} {path} -> {last_error}")

    raise FeishuApiError(f"{method} {path} 重试耗尽：{last_error}")


# ============================================================
# 字段值转换：飞书文本字段读回来是 [{"type":"text","text":"..."}] 这种分段数组，
# 不是纯字符串；写入时反而直接传字符串即可。截断超长文本，飞书单字段 5 万字符上限。
# ============================================================


def _extract_text_field(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(seg.get("text", "") for seg in value if isinstance(seg, dict))
    return str(value)


def _truncate_text(value: str, limit: int = TEXT_FIELD_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _extract_number_field(value: Any) -> Any:
    """飞书 Number 字段读回来是字符串（实测 `"2"`/`"16732"`，不是 JSON 数字，大概率是为了
    避免 JS 客户端的大数精度丢失），不转换的话跟本地 int 比较永远不相等，导致每次重跑都
    误判成需要 update。"""
    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            f = float(value)
            return int(f) if f.is_integer() else f
        except ValueError:
            return value
    return value


def _build_fields(row: sqlite3.Row, field_specs: list[tuple[str, int]]) -> dict[str, Any]:
    """DB 行 -> 飞书 record 的 fields dict。NULL 列直接不写入这个 key（而不是写 null），
    避免依赖飞书对 null 值的具体处理行为。空字符串同样跳过——实测飞书不会把写入的空
    字符串 Text 字段持久化成一个"空但存在"的值，读回来就是 None，如果这里仍然把它当
    有效值 `''` 写进 desired_fields，会导致 _record_needs_update 永远判定为"变了"
    （'' != None），每次重跑都误判成 update，见 sync_announcements 幂等验证记录。"""
    fields: dict[str, Any] = {}
    for name, ftype in field_specs:
        val = row[name]
        if val is None:
            continue
        if ftype == FIELD_TYPE_CHECKBOX:
            fields[name] = bool(val)
        elif ftype == FIELD_TYPE_NUMBER:
            fields[name] = val
        else:
            text_val = str(val)
            if text_val == "":
                continue
            fields[name] = _truncate_text(text_val)
    return fields


def _record_needs_update(
    existing_fields: dict[str, Any], desired_fields: dict[str, Any], field_specs: list[tuple[str, int]]
) -> bool:
    for name, ftype in field_specs:
        want = desired_fields.get(name)
        have = existing_fields.get(name)
        if ftype == FIELD_TYPE_TEXT:
            have = _extract_text_field(have)
        elif ftype == FIELD_TYPE_CHECKBOX:
            have = bool(have) if have is not None else False
            want = bool(want) if want is not None else False
        elif ftype == FIELD_TYPE_NUMBER:
            have = _extract_number_field(have)
        if have != want:
            return True
    return False


# ============================================================
# 字段（列）管理：查询已有字段、按需新建，幂等
# ============================================================


def get_table_fields(app_token: str, table_id: str, creds: FeishuCredentials) -> list[dict[str, Any]]:
    """按飞书返回顺序，列出该表全部字段（含 field_id/field_name/type）。"""
    items: list[dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        resp = _request(
            "GET",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            app_id=creds.app_id,
            app_secret=creds.app_secret,
            params=params,
        )
        data = resp["data"]
        items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        time.sleep(RATE_LIMIT_SLEEP_S)
    return items


def create_field(app_token: str, table_id: str, field_name: str, field_type: int, creds: FeishuCredentials) -> None:
    _request(
        "POST",
        f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        app_id=creds.app_id,
        app_secret=creds.app_secret,
        json_body={"field_name": field_name, "type": field_type},
    )
    time.sleep(RATE_LIMIT_SLEEP_S)


def rename_field(
    app_token: str, table_id: str, field_id: str, new_name: str, field_type: int, creds: FeishuCredentials
) -> None:
    # 飞书的字段更新接口要求 body 里带上 type，即使只是改名、type 本身没变
    # （实测：不带 type 返回 code=99992402 "field validation failed" / "type is required"）。
    _request(
        "PUT",
        f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field_id}",
        app_id=creds.app_id,
        app_secret=creds.app_secret,
        json_body={"field_name": new_name, "type": field_type},
    )
    time.sleep(RATE_LIMIT_SLEEP_S)


def ensure_fields(
    app_token: str,
    table_id: str,
    field_specs: list[tuple[str, int]],
    creds: FeishuCredentials,
    *,
    dry_run: bool = False,
) -> None:
    """对比期望字段列表，只新建缺少的字段，已有字段不动（幂等，可重复执行）。

    唯一的例外：全新空表（飞书自动生成的默认主字段，且当前只有这一个字段）时，把这个
    默认占位字段改名成业务主键（uid/id），让主键字段真正排在第一列，而不是留一个多余
    的占位列——这个改名只在表里"确实只有这一个自动生成的字段"时才触发，不会动任何已经
    真实使用过的字段。
    """
    primary_field_name = field_specs[0][0]
    existing = get_table_fields(app_token, table_id, creds)
    existing_names = {f["field_name"] for f in existing}

    if len(existing) == 1 and primary_field_name not in existing_names:
        default_field = existing[0]
        if dry_run:
            print(f"[dry-run] 将把默认主字段 '{default_field['field_name']}' 改名为 '{primary_field_name}'")
        else:
            rename_field(
                app_token, table_id, default_field["field_id"], primary_field_name, default_field["type"], creds
            )
        existing_names.discard(default_field["field_name"])
        existing_names.add(primary_field_name)

    for name, ftype in field_specs:
        if name in existing_names:
            continue
        if dry_run:
            print(f"[dry-run] 将新建字段 '{name}'（type={ftype}）")
        else:
            create_field(app_token, table_id, name, ftype, creds)


# ============================================================
# 记录（行）同步
# ============================================================


def _index_existing_records(
    app_token: str, table_id: str, key_column: str, creds: FeishuCredentials
) -> dict[str, dict[str, Any]]:
    """一次性拉全表已有记录，按业务主键建索引：{key: {"record_id":..., "fields":...}}。
    不用飞书按条件过滤查询接口——避免依赖其 filter 语法细节，几千条规模一次性全量拉取
    也不算贵。"""
    index: dict[str, dict[str, Any]] = {}
    page_token: Optional[str] = None
    while True:
        params: dict[str, Any] = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        resp = _request(
            "GET",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            app_id=creds.app_id,
            app_secret=creds.app_secret,
            params=params,
        )
        data = resp["data"]
        for item in data.get("items", []):
            item_fields = item.get("fields", {})
            key = _extract_text_field(item_fields.get(key_column))
            if key:
                index[key] = {"record_id": item["record_id"], "fields": item_fields}
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        time.sleep(RATE_LIMIT_SLEEP_S)
    return index


def _update_record(
    app_token: str, table_id: str, record_id: str, fields: dict[str, Any], creds: FeishuCredentials
) -> tuple[bool, Optional[str]]:
    try:
        _request(
            "PUT",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
            app_id=creds.app_id,
            app_secret=creds.app_secret,
            json_body={"fields": fields},
        )
        time.sleep(RATE_LIMIT_SLEEP_S)
        return True, None
    except FeishuApiError as e:
        return False, str(e)


def _batch_create(
    app_token: str, table_id: str, fields_list: list[dict[str, Any]], creds: FeishuCredentials
) -> list[dict[str, Any]]:
    body = {"records": [{"fields": f} for f in fields_list]}
    resp = _request(
        "POST",
        f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
        app_id=creds.app_id,
        app_secret=creds.app_secret,
        json_body=body,
    )
    return resp["data"]["records"]


def _chunks(seq: list[Any], size: int) -> Any:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _log_sync(
    conn: sqlite3.Connection,
    target: str,
    record_id: str,
    action: str,
    status: str,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO sync_log (target, record_id, action, status, error, synced_at) VALUES (?, ?, ?, ?, ?, ?)",
        (target, record_id, action, status, error, utcnow_iso()),
    )


@dataclass
class SyncReport:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    dry_run_rows: int = 0
    batches: list[str] = field(default_factory=list)


def _sync_table(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    key_column: str,
    field_specs: list[tuple[str, int]],
    app_token: str,
    table_id: str,
    target: str,
    creds: FeishuCredentials,
    dry_run: bool,
) -> SyncReport:
    report = SyncReport()
    if not rows:
        return report

    if dry_run:
        print(f"[dry-run] {target}: 共 {len(rows)} 行，将做字段映射校验（不调用飞书 API）")
        for name, ftype in field_specs:
            print(f"  字段：{name}（type={ftype}）")
        report.dry_run_rows = len(rows)
        return report

    ensure_fields(app_token, table_id, field_specs, creds)
    existing_by_key = _index_existing_records(app_token, table_id, key_column, creds)

    to_create: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        key = row[key_column]
        desired = _build_fields(row, field_specs)
        existing = existing_by_key.get(key)
        if existing is None:
            to_create.append((key, desired))
            continue
        if _record_needs_update(existing["fields"], desired, field_specs):
            ok, err = _update_record(app_token, table_id, existing["record_id"], desired, creds)
            _log_sync(conn, target, key, "update", "success" if ok else "failed", err)
            if ok:
                report.updated += 1
            else:
                report.failed += 1
        else:
            _log_sync(conn, target, key, "skip", "success")
            report.skipped += 1

    for batch in _chunks(to_create, BATCH_CREATE_SIZE):
        keys = [k for k, _ in batch]
        fields_list = [f for _, f in batch]
        try:
            _batch_create(app_token, table_id, fields_list, creds)
            for key in keys:
                _log_sync(conn, target, key, "create", "success")
            report.created += len(keys)
        except FeishuApiError as e:
            for key in keys:
                _log_sync(conn, target, key, "create", "failed", str(e))
            report.failed += len(keys)

    return report


def sync_announcements(
    conn: sqlite3.Connection, creds: FeishuCredentials, *, dry_run: bool = False
) -> SyncReport:
    rows = conn.execute("SELECT * FROM announcements ORDER BY uid").fetchall()
    return _sync_table(
        conn,
        rows,
        key_column="uid",
        field_specs=ANNOUNCEMENTS_FIELD_SPECS,
        app_token=creds.announcements_app_token,
        table_id=creds.announcements_table_id,
        target="bitable_announcements",
        creds=creds,
        dry_run=dry_run,
    )


def sync_insights(
    conn: sqlite3.Connection, creds: FeishuCredentials, *, dry_run: bool = False
) -> SyncReport:
    rows = conn.execute("SELECT * FROM insights ORDER BY id").fetchall()
    return _sync_table(
        conn,
        rows,
        key_column="id",
        field_specs=INSIGHTS_FIELD_SPECS,
        app_token=creds.insights_app_token,
        table_id=creds.insights_table_id,
        target="bitable_insights",
        creds=creds,
        dry_run=dry_run,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--table", choices=["announcements", "insights", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    creds = load_feishu_credentials()
    if not args.dry_run:
        creds.validate()

    conn = connect(args.db_path)
    try:
        if args.table in ("announcements", "all"):
            report = sync_announcements(conn, creds, dry_run=args.dry_run)
            print(
                f"announcements: created={report.created} updated={report.updated} "
                f"skipped={report.skipped} failed={report.failed} dry_run_rows={report.dry_run_rows}"
            )
        if args.table in ("insights", "all"):
            report = sync_insights(conn, creds, dry_run=args.dry_run)
            print(
                f"insights: created={report.created} updated={report.updated} "
                f"skipped={report.skipped} failed={report.failed} dry_run_rows={report.dry_run_rows}"
            )
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
