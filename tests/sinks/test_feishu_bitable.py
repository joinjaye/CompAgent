"""src/sinks/feishu_bitable.py 单测：全部 mock 飞书 API，不发真实请求。

覆盖：_request 的 token 刷新 + 业务错误码重试、ensure_fields 建列幂等（含默认主字段
改名）、_sync_table 的新建/更新/跳过三种记录路径、dry_run 不调用飞书 API、
sync_log 正确写入。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.connection import init_db
from src.db.operations import upsert_announcement
from src.sinks import feishu_bitable as fb


@pytest.fixture(autouse=True)
def _clear_token_cache():
    fb._token_cache.clear()
    yield
    fb._token_cache.clear()


@pytest.fixture(autouse=True)
def _no_rate_limit_sleep(monkeypatch):
    # 测试不需要真的等待 RATE_LIMIT_SLEEP_S，否则几十个字段/记录的用例会拖慢整个套件。
    monkeypatch.setattr(fb.time, "sleep", lambda *_: None)


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def _insert_announcement(conn: sqlite3.Connection, article_id: str, **overrides) -> str:
    defaults = dict(
        source="Bitunix",
        locale="EN",
        article_id=article_id,
        url=f"https://example.com/{article_id}",
        title=f"Title {article_id}",
        content=f"Content {article_id}",
        post_time="2026-07-15T00:00:00Z",
        category="campaign",
        raw_category="123",
    )
    defaults.update(overrides)
    result = upsert_announcement(conn, **defaults)
    conn.commit()
    return result.uid


def _insert_insight(conn: sqlite3.Connection, insight_id: str, **overrides) -> str:
    defaults = dict(
        id=insight_id,
        batch_date="2026-07-15",
        source="Bitunix",
        category="campaign",
        locale="EN",
        article_count=1,
        related_uids="[]",
        is_locale_derived=False,
        derived_from_id=None,
        summary="s",
        articles_analysis="[]",
        zmx_diff=None,
        diff_type="不适用",
        priority="低",
        zmx_evidence_uids="[]",
        prompt_version="campaign-v1",
        llm_tokens_used=100,
        created_at="2026-07-15T00:00:00Z",
        updated_at="2026-07-15T00:00:00Z",
    )
    defaults.update(overrides)
    conn.execute(
        """
        INSERT INTO insights (
            id, batch_date, source, category, locale, article_count, related_uids,
            is_locale_derived, derived_from_id, summary, articles_analysis, zmx_diff,
            diff_type, priority, zmx_evidence_uids, prompt_version, llm_tokens_used,
            created_at, updated_at
        ) VALUES (:id, :batch_date, :source, :category, :locale, :article_count, :related_uids,
                  :is_locale_derived, :derived_from_id, :summary, :articles_analysis, :zmx_diff,
                  :diff_type, :priority, :zmx_evidence_uids, :prompt_version, :llm_tokens_used,
                  :created_at, :updated_at)
        """,
        defaults,
    )
    conn.commit()
    return insight_id


TEST_CREDS = fb.FeishuCredentials(
    app_id="cli_test",
    app_secret="secret_test",
    announcements_app_token="appAnn",
    announcements_table_id="tblAnn",
    insights_app_token="appIns",
    insights_table_id="tblIns",
)


class FakeFeishuServer:
    """内存里模拟一个飞书多维表 app：字段/记录，按 table_id 隔离。"""

    def __init__(self):
        self.fields: dict[str, list[dict]] = {}
        self.records: dict[str, dict[str, dict]] = {}
        self._next_field_id = 1
        self._next_record_id = 1
        self.calls: list[tuple[str, str]] = []

    def seed_default_table(self, table_id: str, default_field_name: str = "文本") -> None:
        self.fields[table_id] = [{"field_id": "fldDefault", "field_name": default_field_name, "type": 1}]
        self.records[table_id] = {}

    def _wrap_value(self, table_id: str, field_name: str, value):
        ftype = next((f["type"] for f in self.fields.get(table_id, []) if f["field_name"] == field_name), None)
        if ftype == fb.FIELD_TYPE_TEXT and isinstance(value, str):
            return [{"type": "text", "text": value}]
        if ftype == fb.FIELD_TYPE_NUMBER and value is not None:
            # 真实飞书 API 把 Number 字段读回来时是字符串（实测 "2"/"16732"，不是 JSON
            # 数字），fake server 复现这个行为，防止 _record_needs_update 的数字比较
            # 回归成直接 have != want（字符串 vs int 永远不相等）。
            return str(value)
        return value

    def request(self, method, path, *, app_id, app_secret, json_body=None, params=None, max_retries=3):
        self.calls.append((method, path))
        parts = path.strip("/").split("/")
        # bitable/v1/apps/{app_token}/tables/{table_id}/...
        table_id = parts[5]
        rest = parts[6:]

        if rest == ["fields"] and method == "GET":
            return {"code": 0, "data": {"items": self.fields.get(table_id, []), "has_more": False}}

        if rest == ["fields"] and method == "POST":
            field_id = f"fld{self._next_field_id}"
            self._next_field_id += 1
            new_field = {"field_id": field_id, "field_name": json_body["field_name"], "type": json_body["type"]}
            self.fields.setdefault(table_id, []).append(new_field)
            return {"code": 0, "data": {"field": new_field}}

        if len(rest) == 2 and rest[0] == "fields" and method == "PUT":
            field_id = rest[1]
            # 真实飞书 API 要求改名请求也带上 type，缺失时返回 99992402（field
            # validation failed）；这里在 fake server 里复现这个要求，防止 rename_field
            # 的调用方回归成只传 field_name。
            if "type" not in json_body:
                return {"code": 99992402, "msg": "field validation failed: type is required"}
            for f in self.fields.get(table_id, []):
                if f["field_id"] == field_id:
                    f["field_name"] = json_body["field_name"]
            return {"code": 0, "data": {}}

        if rest == ["records"] and method == "GET":
            items = [
                {"record_id": rid, "fields": fields} for rid, fields in self.records.get(table_id, {}).items()
            ]
            return {"code": 0, "data": {"items": items, "has_more": False}}

        if rest == ["records", "batch_create"] and method == "POST":
            created = []
            for rec in json_body["records"]:
                rid = f"rec{self._next_record_id}"
                self._next_record_id += 1
                wrapped = {k: self._wrap_value(table_id, k, v) for k, v in rec["fields"].items()}
                self.records.setdefault(table_id, {})[rid] = wrapped
                created.append({"record_id": rid, "fields": wrapped})
            return {"code": 0, "data": {"records": created}}

        if len(rest) == 2 and rest[0] == "records" and method == "PUT":
            record_id = rest[1]
            wrapped = {k: self._wrap_value(table_id, k, v) for k, v in json_body["fields"].items()}
            self.records.setdefault(table_id, {}).setdefault(record_id, {}).update(wrapped)
            return {"code": 0, "data": {"record": {"record_id": record_id, "fields": self.records[table_id][record_id]}}}

        raise AssertionError(f"未预期的请求：{method} {path}")


@pytest.fixture
def fake_server(monkeypatch):
    server = FakeFeishuServer()
    server.seed_default_table("tblAnn")
    server.seed_default_table("tblIns")
    monkeypatch.setattr(fb, "_request", server.request)
    return server


# ============================================================
# 低层 _request：token 刷新 + 业务错误码
# ============================================================


def test_get_tenant_access_token_caches_and_reuses(monkeypatch):
    calls = []

    def fake_fetch_json(url, *, method="GET", headers=None, body=None):
        calls.append(url)
        return {"code": 0, "tenant_access_token": "tok-1", "expire": 7200}

    monkeypatch.setattr(fb, "fetch_json", fake_fetch_json)
    t1 = fb._get_tenant_access_token("app1", "secret1")
    t2 = fb._get_tenant_access_token("app1", "secret1")
    assert t1 == t2 == "tok-1"
    assert len(calls) == 1  # 第二次命中缓存，没有重新请求


def test_request_refreshes_token_on_invalid_code(monkeypatch):
    token_calls = {"n": 0}
    business_calls = {"n": 0}

    def fake_fetch_json(url, *, method="GET", headers=None, body=None):
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            token_calls["n"] += 1
            return {"code": 0, "tenant_access_token": f"tok-{token_calls['n']}", "expire": 7200}
        business_calls["n"] += 1
        if headers["Authorization"] == "Bearer tok-1":
            return {"code": 99991663, "msg": "token invalid"}
        return {"code": 0, "data": {"ok": True}}

    monkeypatch.setattr(fb, "fetch_json", fake_fetch_json)
    resp = fb._request("GET", "/bitable/v1/apps/a/tables/t/fields", app_id="app1", app_secret="secret1")
    assert resp["code"] == 0
    assert token_calls["n"] == 2  # 第一个 token 失效后重新获取了一次
    assert business_calls["n"] == 2


def test_request_raises_after_retries_exhausted(monkeypatch):
    def fake_fetch_json(url, *, method="GET", headers=None, body=None):
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "tok-1", "expire": 7200}
        return {"code": 12345, "msg": "some persistent error"}

    monkeypatch.setattr(fb, "fetch_json", fake_fetch_json)
    with pytest.raises(fb.FeishuApiError, match="12345"):
        fb._request("GET", "/bitable/v1/apps/a/tables/t/fields", app_id="app1", app_secret="secret1", max_retries=2)


# ============================================================
# ensure_fields：建列幂等 + 默认主字段改名
# ============================================================


def test_ensure_fields_renames_default_field_and_creates_rest(fake_server):
    fb.ensure_fields("appAnn", "tblAnn", fb.ANNOUNCEMENTS_FIELD_SPECS, TEST_CREDS)
    names = [f["field_name"] for f in fake_server.fields["tblAnn"]]
    assert names[0] == "uid"  # 默认字段被改名成业务主键，排第一
    for expected_name, _ in fb.ANNOUNCEMENTS_FIELD_SPECS:
        assert expected_name in names


def test_ensure_fields_is_idempotent_no_duplicate_creation(fake_server):
    fb.ensure_fields("appAnn", "tblAnn", fb.ANNOUNCEMENTS_FIELD_SPECS, TEST_CREDS)
    first_call_count = len(fake_server.calls)
    fb.ensure_fields("appAnn", "tblAnn", fb.ANNOUNCEMENTS_FIELD_SPECS, TEST_CREDS)
    names = [f["field_name"] for f in fake_server.fields["tblAnn"]]
    assert len(names) == len(fb.ANNOUNCEMENTS_FIELD_SPECS)  # 没有重复字段
    # 第二次只发生一次 GET fields 查询，没有任何 POST/PUT
    second_call_new = [c for c in fake_server.calls[first_call_count:] if c[0] in ("POST", "PUT")]
    assert second_call_new == []


# ============================================================
# _sync_table：新建 / 更新 / 跳过 + sync_log 写入
# ============================================================


def test_sync_announcements_creates_new_records_and_logs(conn, fake_server):
    _insert_announcement(conn, "a1")
    _insert_announcement(conn, "a2")

    report = fb.sync_announcements(conn, TEST_CREDS)
    conn.commit()

    assert report.created == 2
    assert report.updated == 0
    assert report.skipped == 0
    assert report.failed == 0
    assert len(fake_server.records["tblAnn"]) == 2

    log_rows = conn.execute("SELECT * FROM sync_log WHERE target='bitable_announcements'").fetchall()
    assert len(log_rows) == 2
    assert all(r["action"] == "create" and r["status"] == "success" for r in log_rows)


def test_sync_announcements_skips_unchanged_records_on_rerun(conn, fake_server):
    _insert_announcement(conn, "a1")
    fb.sync_announcements(conn, TEST_CREDS)
    conn.commit()

    report = fb.sync_announcements(conn, TEST_CREDS)
    conn.commit()

    assert report.created == 0
    assert report.updated == 0
    assert report.skipped == 1

    log_rows = conn.execute(
        "SELECT * FROM sync_log WHERE target='bitable_announcements' AND action='skip'"
    ).fetchall()
    assert len(log_rows) == 1
    assert log_rows[0]["status"] == "success"


def test_sync_announcements_updates_changed_record(conn, fake_server):
    uid = _insert_announcement(conn, "a1", title="Old title")
    fb.sync_announcements(conn, TEST_CREDS)
    conn.commit()

    conn.execute("UPDATE announcements SET title = 'New title' WHERE uid = ?", (uid,))
    conn.commit()

    report = fb.sync_announcements(conn, TEST_CREDS)
    conn.commit()

    assert report.created == 0
    assert report.updated == 1
    assert report.skipped == 0

    record = next(iter(fake_server.records["tblAnn"].values()))
    assert fb._extract_text_field(record["title"]) == "New title"

    log_rows = conn.execute(
        "SELECT * FROM sync_log WHERE target='bitable_announcements' AND action='update'"
    ).fetchall()
    assert len(log_rows) == 1
    assert log_rows[0]["record_id"] == uid
    assert log_rows[0]["status"] == "success"


def test_sync_insights_creates_and_maps_number_checkbox_fields(conn, fake_server):
    _insert_insight(conn, "ins1", article_count=5, is_locale_derived=True, llm_tokens_used=0)

    report = fb.sync_insights(conn, TEST_CREDS)
    conn.commit()

    assert report.created == 1
    record = next(iter(fake_server.records["tblIns"].values()))
    assert fb._extract_number_field(record["article_count"]) == 5
    assert record["is_locale_derived"] is True
    assert fb._extract_number_field(record["llm_tokens_used"]) == 0

    log_rows = conn.execute("SELECT * FROM sync_log WHERE target='bitable_insights'").fetchall()
    assert len(log_rows) == 1
    assert log_rows[0]["record_id"] == "ins1"


def test_sync_insights_rerun_is_fully_idempotent_despite_stringified_numbers(conn, fake_server):
    """飞书把 Number 字段读回来是字符串（"5" 不是 5），_record_needs_update 如果不做数字
    强制转换比较，会把这种类型差异误判成"内容变了"，每次重跑都产生不必要的 update。"""
    _insert_insight(conn, "ins1", article_count=5, llm_tokens_used=100)
    fb.sync_insights(conn, TEST_CREDS)
    conn.commit()

    report = fb.sync_insights(conn, TEST_CREDS)
    conn.commit()

    assert report.created == 0
    assert report.updated == 0
    assert report.skipped == 1


def test_sync_announcements_rerun_is_idempotent_with_empty_string_content(conn, fake_server):
    """content='' 这种空字符串列（如 Zoomex 纯图片公告）如果原样写进飞书 Text 字段，
    飞书不会持久化成"空但存在"的值，读回来是 None——如果 _build_fields 仍然把它当作
    有效值 '' 参与比较，会导致每次重跑都被误判为需要 update。"""
    _insert_announcement(conn, "a1", content="")
    fb.sync_announcements(conn, TEST_CREDS)
    conn.commit()

    report = fb.sync_announcements(conn, TEST_CREDS)
    conn.commit()

    assert report.created == 0
    assert report.updated == 0
    assert report.skipped == 1


# ============================================================
# dry_run：不调用飞书 API，不写 sync_log
# ============================================================


def test_dry_run_does_not_call_feishu_api(conn, monkeypatch):
    def fail_request(*args, **kwargs):
        raise AssertionError("dry_run 不应该调用 _request")

    monkeypatch.setattr(fb, "_request", fail_request)
    _insert_announcement(conn, "a1")

    report = fb.sync_announcements(conn, TEST_CREDS, dry_run=True)

    assert report.dry_run_rows == 1
    assert report.created == 0
    log_rows = conn.execute("SELECT * FROM sync_log").fetchall()
    assert log_rows == []


# ============================================================
# 字段值转换工具函数
# ============================================================


def test_extract_text_field_handles_plain_string_and_segments():
    assert fb._extract_text_field("plain") == "plain"
    assert fb._extract_text_field([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"
    assert fb._extract_text_field(None) is None


def test_truncate_text_appends_ellipsis_when_over_limit():
    long_text = "x" * (fb.TEXT_FIELD_LIMIT + 100)
    truncated = fb._truncate_text(long_text)
    assert len(truncated) == fb.TEXT_FIELD_LIMIT
    assert truncated.endswith("…")


def test_truncate_text_leaves_short_text_untouched():
    assert fb._truncate_text("short") == "short"
