"""src/analysis/zmx_catalog.py 单测：覆盖 Phase① 的两个核心修复——① 提取不受
lookback 窗口限制（>90 天的 Zoomex 公告一样会被捞到，这是老版本 zmx_baseline 做不到
的）；② mechanism_type 是封闭/半封闭枚举，不匹配的一律落 other + raw_mechanism_label，
不允许 LLM 自造新类型。另外覆盖 per-article EN→locale 派生、rollup 的
yes/no/partial 三态判断、以及提取熔断上限。全部离线：真实 schema.sql 建的临时库 +
monkeypatch 掉 call_llm。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.analysis.config import LlmCredentials
from src.db.connection import get_connection
from src.db.operations import upsert_announcement


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture()
def conn(db_path):
    with get_connection(db_path) as conn:
        conn.executescript(_read_schema())
        yield conn


def _read_schema() -> str:
    from src.db.connection import SCHEMA_PATH
    return SCHEMA_PATH.read_text(encoding="utf-8")


def _insert_zoomex(conn, *, locale, article_id, category, title="T", content="C", post_time=None, group_id=None):
    post_time = post_time or "2026-07-15T00:00:00Z"
    result = upsert_announcement(
        conn, source="Zoomex", locale=locale, article_id=article_id,
        title=title, content=content, post_time=post_time,
        fetched_at="2026-07-15T01:00:00Z", category=category,
        group_id=group_id or f"zoomex_{article_id}",
    )
    return result.uid


@pytest.fixture()
def fake_credentials(monkeypatch):
    creds = LlmCredentials(api_key="k", api_base="https://example.invalid/v1", model="test-model")
    monkeypatch.setattr("src.analysis.zmx_catalog.load_llm_credentials", lambda: creds)
    return creds


def _extract_json(uid, mechanism_type, **overrides):
    article = {"uid": uid, "mechanism_type": mechanism_type, "core_summary": "s", "key_mechanics": "m"}
    article.update(overrides)
    return json.dumps({"articles": [article]})


# ---------------------------------------------------------------- lookback 移除 ----

def test_list_pending_zoomex_rows_has_no_lookback_window(conn):
    """老版本 zmx_baseline 只看近 90 天；这里故意插入一条 200 天前的 Zoomex 公告，
    确认新版本一样会捞到——这是修复"无法断言缺失"问题的字面验证。
    """
    from src.analysis.zmx_catalog import list_pending_zoomex_rows

    old_post_time = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    uid = _insert_zoomex(conn, locale="EN", article_id="old-1", category="campaign", post_time=old_post_time)

    rows = list_pending_zoomex_rows(conn, category="campaign", locale="EN")

    assert [r["uid"] for r in rows] == [uid]


# ---------------------------------------------------------------- 枚举兜底 ----

def test_run_extraction_falls_back_to_other_for_unknown_mechanism_type(conn, fake_credentials, monkeypatch):
    from src.analysis.zmx_catalog import run_extraction

    uid = _insert_zoomex(conn, locale="EN", article_id="1", category="campaign", title="Something new")

    def fake_call_llm(system, user, **kwargs):
        # "这是 LLM 自己编的新类型名"，不在 config/zmx_mechanism_taxonomy.yaml 里
        return _extract_json(uid, "totally_made_up_type", raw_mechanism_label=None), 10

    monkeypatch.setattr("src.analysis.zmx_catalog.call_llm", fake_call_llm)

    report = run_extraction(conn, category="campaign", locale="EN", provider="openai_http")
    assert report.llm_calls == 1
    assert report.validation_failed == 0

    row = conn.execute("SELECT mechanism_type, raw_mechanism_label FROM zmx_summary WHERE source_uid = ?", (uid,)).fetchone()
    assert row["mechanism_type"] == "other"
    assert row["raw_mechanism_label"] == "totally_made_up_type"


def test_run_extraction_accepts_known_taxonomy_key(conn, fake_credentials, monkeypatch):
    from src.analysis.zmx_catalog import run_extraction

    uid = _insert_zoomex(conn, locale="EN", article_id="1", category="campaign", title="Deposit bonus")

    monkeypatch.setattr(
        "src.analysis.zmx_catalog.call_llm",
        lambda system, user, **kwargs: (_extract_json(uid, "deposit_reward"), 10),
    )

    run_extraction(conn, category="campaign", locale="EN", provider="openai_http")

    row = conn.execute("SELECT mechanism_type, raw_mechanism_label FROM zmx_summary WHERE source_uid = ?", (uid,)).fetchone()
    assert row["mechanism_type"] == "deposit_reward"
    assert row["raw_mechanism_label"] is None


# ---------------------------------------------------------------- per-article EN 派生 ----

def test_fr_row_derives_from_en_without_calling_llm(conn, fake_credentials, monkeypatch):
    from src.analysis.zmx_catalog import run_extraction

    uid_en = _insert_zoomex(conn, locale="EN", article_id="1", category="campaign", title="EN title", group_id="g1")
    uid_fr = _insert_zoomex(conn, locale="FR", article_id="1", category="campaign", title="FR title", group_id="g1")

    calls = {"n": 0}

    def fake_call_llm(system, user, **kwargs):
        calls["n"] += 1
        return _extract_json(uid_en, "deposit_reward"), 10

    monkeypatch.setattr("src.analysis.zmx_catalog.call_llm", fake_call_llm)

    # 不传 locale：同一次 run 内 EN 先跑完，FR 才有机会派生
    report = run_extraction(conn, category="campaign", provider="openai_http")

    assert calls["n"] == 1  # 只有 EN 真正调用了 LLM
    assert report.derived == 1

    fr_row = conn.execute("SELECT mechanism_type, is_locale_derived, llm_tokens_used FROM zmx_summary WHERE source_uid = ?", (uid_fr,)).fetchone()
    assert fr_row["mechanism_type"] == "deposit_reward"
    assert fr_row["is_locale_derived"] == 1
    assert fr_row["llm_tokens_used"] == 0


def test_fr_row_without_matching_group_id_does_not_derive(conn, fake_credentials, monkeypatch):
    from src.analysis.zmx_catalog import run_extraction

    uid_en = _insert_zoomex(conn, locale="EN", article_id="1", category="campaign", title="EN title", group_id="g1")
    uid_fr = _insert_zoomex(conn, locale="FR", article_id="2", category="campaign", title="FR-only title", group_id="g2")

    calls = {"n": 0}

    def fake_call_llm(system, user, **kwargs):
        calls["n"] += 1
        if uid_en in user or "EN title" in user:
            return _extract_json(uid_en, "deposit_reward"), 10
        return _extract_json(uid_fr, "lucky_draw"), 10

    monkeypatch.setattr("src.analysis.zmx_catalog.call_llm", fake_call_llm)

    report = run_extraction(conn, category="campaign", provider="openai_http")

    assert calls["n"] == 2  # FR 没有可派生的同 group_id EN 记录，必须真调用
    assert report.derived == 0

    fr_row = conn.execute("SELECT is_locale_derived FROM zmx_summary WHERE source_uid = ?", (uid_fr,)).fetchone()
    assert fr_row["is_locale_derived"] == 0


# ---------------------------------------------------------------- rollup ----

def test_rollup_marks_exists_yes_when_summary_rows_present(conn):
    from src.analysis.zmx_catalog import run_rollup, upsert_summary_row

    uid = _insert_zoomex(conn, locale="EN", article_id="1", category="campaign", title="Deposit bonus")
    upsert_summary_row(
        conn, source_uid=uid, group_id="g1", category="campaign", locale="EN",
        content_hash="h1", prompt_version="v1", mechanism_type="deposit_reward",
        core_summary="充值送奖励",
    )
    conn.commit()

    run_rollup(conn, category="campaign")

    row = conn.execute(
        "SELECT exists_flag, example_uids FROM zmx_catalog_entry WHERE category='campaign' AND mechanism_type='deposit_reward'"
    ).fetchone()
    assert row["exists_flag"] == "yes"
    assert uid in json.loads(row["example_uids"])


def test_rollup_marks_exists_no_when_taxonomy_key_has_zero_rows(conn):
    from src.analysis.zmx_catalog import run_rollup

    # 完全没有插入任何 zmx_summary 数据
    run_rollup(conn, category="campaign")

    row = conn.execute(
        "SELECT exists_flag FROM zmx_catalog_entry WHERE category='campaign' AND mechanism_type='grid_trading_contest'"
    ).fetchone()
    assert row["exists_flag"] == "no"


def test_rollup_marks_exists_partial_on_term_overlap_with_other_bucket(conn):
    from src.analysis.zmx_catalog import run_rollup, upsert_summary_row

    uid = _insert_zoomex(conn, locale="EN", article_id="1", category="campaign", title="Pizza voucher event")
    upsert_summary_row(
        conn, source_uid=uid, group_id="g1", category="campaign", locale="EN",
        content_hash="h1", prompt_version="v1", mechanism_type="other",
        raw_mechanism_label="Pizza Day trading voucher event",
    )
    conn.commit()

    run_rollup(conn, category="campaign")

    # festival_theme_campaign 的真实示例里含 "Pizza Day Party...trading voucher"，
    # 跟上面这条 other 桶记录的 raw_mechanism_label 有真实词项重叠
    row = conn.execute(
        "SELECT exists_flag FROM zmx_catalog_entry WHERE category='campaign' AND mechanism_type='festival_theme_campaign'"
    ).fetchone()
    assert row["exists_flag"] == "partial"


# ---------------------------------------------------------------- 熔断 ----

def test_max_calls_budget_cap_skips_remaining_batches(conn, fake_credentials, monkeypatch):
    from src.analysis.zmx_catalog import run_extraction

    uid1 = _insert_zoomex(conn, locale="EN", article_id="1", category="campaign", title="A", group_id="g1")
    uid2 = _insert_zoomex(conn, locale="EN", article_id="2", category="product", title="B", group_id="g2")

    calls = {"n": 0}

    def fake_call_llm(system, user, **kwargs):
        calls["n"] += 1
        if uid1 in user:
            return _extract_json(uid1, "deposit_reward"), 10
        return _extract_json(uid2, "trading"), 10

    monkeypatch.setattr("src.analysis.zmx_catalog.call_llm", fake_call_llm)

    report = run_extraction(conn, locale="EN", provider="openai_http", batch_size=1, max_calls=1)

    assert calls["n"] == 1
    assert report.llm_calls == 1
    assert report.skipped_budget_cap == 1


# ---------------------------------------------------------------- select_relevant_catalog 停用词 ----


def _catalog_entry(uid: str, mechanism_type: str, title: str, key_mechanics: str):
    from src.analysis.zmx_catalog import ZmxCatalogEntry

    return ZmxCatalogEntry(
        uid=uid, title=title, mechanism_type=mechanism_type, key_mechanics=key_mechanics,
        reward_range=None, target_users=None, start_date=None, end_date=None, post_time=None,
    )


def test_select_relevant_catalog_ignores_generic_stopwords():
    """2026-07-22 真实数据发现：跟 staged.py::recall_candidates 同一个问题——不
    过滤"users"/"account"/"platform"这类高频词，几乎任何两段英文文本都会碰出
    重叠，导致批次级候选窄化选到不相关的条目。"""
    from src.analysis.zmx_catalog import select_relevant_catalog

    rows = [{"title": "AUSTRAC registration for remittance entity", "content": "Users can view their account on the platform."}]
    unrelated = _catalog_entry("z1", "wallet", "Quick transfer", "Users click transfer on the platform to use their account.")
    relevant = _catalog_entry("z2", "other", "Registration", "Remittance entity registration process for users.")
    other_filler = [_catalog_entry(f"z{i}", "earn", f"filler {i}", "generic filler text") for i in range(3, 6)]

    selected = select_relevant_catalog(rows, [unrelated, relevant, *other_filler], max_entries=1)
    assert selected[0].uid == "z2"
