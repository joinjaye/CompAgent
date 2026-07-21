"""src/analysis/run.py 单测：dry_run 模式不写库/不调 LLM、category=other 被跳过、
批次级 upsert、locale 复用（EN 先跑完才能被 FR 复用）、llm_cache 命中跳过重复调用。
全部离线：真实 schema.sql 建的临时库 + monkeypatch 掉 call_llm / load_llm_credentials。
"""

from __future__ import annotations

import json

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


BATCH_DATE = "2026-07-14"


def _insert(conn, *, source, locale, article_id, group_id, category, title="T", content="C", status_hint="new"):
    result = upsert_announcement(
        conn, source=source, locale=locale, article_id=article_id,
        title=title, content=content, post_time=f"{BATCH_DATE}T00:00:00Z",
        fetched_at=f"{BATCH_DATE}T01:00:00Z", category=category, group_id=group_id,
    )
    conn.execute("UPDATE announcements SET category = ? WHERE uid = ?", (category, result.uid))
    return result.uid


FAKE_LLM_JSON = json.dumps({
    "batch_summary": "test summary",
    "articles": [],
    "zmx_comparison": {"diff_type": "不适用", "analysis": None, "evidence_indices": [], "priority": "低", "priority_reason": None},
})


@pytest.fixture()
def fake_credentials(monkeypatch):
    creds = LlmCredentials(api_key="sk-test", api_base="https://api.example.com/v1", model="gpt-test")
    monkeypatch.setattr("src.analysis.run.load_llm_credentials", lambda: creds)
    return creds


def test_dry_run_does_not_write_db_or_call_llm(conn, monkeypatch):
    _insert(conn, source="Bitunix", locale="EN", article_id="1", group_id="g1", category="campaign")
    conn.commit()

    def boom(*a, **k):
        raise AssertionError("call_llm should not be invoked in dry_run mode")

    monkeypatch.setattr("src.analysis.run.call_llm", boom)

    from src.analysis.run import run
    report = run(conn, batch_date=BATCH_DATE, sources=("Bitunix",), dry_run=True)

    assert report.analyzed == 0
    assert conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0] == 0


def test_category_other_is_skipped(conn, fake_credentials, monkeypatch):
    _insert(conn, source="Bitunix", locale="EN", article_id="1", group_id="g1", category="other")
    conn.commit()

    def boom(*a, **k):
        raise AssertionError("call_llm should not be invoked for category=other")

    monkeypatch.setattr("src.analysis.run.call_llm", boom)

    from src.analysis.run import run
    report = run(conn, batch_date=BATCH_DATE, sources=("Bitunix",), dry_run=False)

    assert report.analyzed == 0
    assert conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0] == 0


def test_full_run_creates_insight_row(conn, fake_credentials, monkeypatch):
    uid = _insert(conn, source="Bitunix", locale="EN", article_id="1", group_id="g1", category="campaign")
    conn.commit()

    monkeypatch.setattr("src.analysis.run.call_llm", lambda *a, **k: (FAKE_LLM_JSON, 42))

    from src.analysis.batch import compute_batch_id
    from src.analysis.run import run

    report = run(conn, batch_date=BATCH_DATE, sources=("Bitunix",), dry_run=False)
    conn.commit()

    assert report.analyzed == 1
    assert report.llm_calls == 1
    assert report.total_tokens == 42

    insight_id = compute_batch_id("Bitunix", "campaign", "EN", BATCH_DATE)
    row = conn.execute("SELECT * FROM insights WHERE id = ?", (insight_id,)).fetchone()
    assert row is not None
    assert row["summary"] == "test summary"
    assert row["article_count"] == 1
    assert json.loads(row["related_uids"]) == [uid]
    assert row["is_locale_derived"] == 0
    assert row["prompt_version"] == "campaign-v3"


@pytest.mark.parametrize("category", ["listing", "delisting"])
def test_listing_categories_never_load_credentials_or_call_llm(conn, monkeypatch, category):
    _insert(conn, source="Bitunix", locale="EN", article_id="1", group_id="g1", category=category)
    conn.commit()

    def boom(*a, **k):
        raise AssertionError("listing/delisting must not touch the LLM path")

    monkeypatch.setattr("src.analysis.run.load_llm_credentials", boom)
    monkeypatch.setattr("src.analysis.run.call_llm", boom)

    from src.analysis.run import run
    report = run(conn, batch_date=BATCH_DATE, sources=("Bitunix",), dry_run=False)

    assert report.llm_calls == 0
    assert report.analyzed == 0
    assert conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0] == 0


def test_rerun_same_day_hits_cache_and_skips_llm_call(conn, fake_credentials, monkeypatch):
    _insert(conn, source="Bitunix", locale="EN", article_id="1", group_id="g1", category="campaign")
    conn.commit()

    call_count = {"n": 0}

    def fake_call_llm(*a, **k):
        call_count["n"] += 1
        return FAKE_LLM_JSON, 42

    monkeypatch.setattr("src.analysis.run.call_llm", fake_call_llm)

    from src.analysis.run import run
    run(conn, batch_date=BATCH_DATE, sources=("Bitunix",), dry_run=False)
    conn.commit()
    report2 = run(conn, batch_date=BATCH_DATE, sources=("Bitunix",), dry_run=False)
    conn.commit()

    assert call_count["n"] == 1  # 第二次应该命中缓存，没有真的再调一次
    assert report2.cache_hits == 1
    assert report2.llm_calls == 0


def test_locale_derives_from_en_without_calling_llm(conn, fake_credentials, monkeypatch):
    _insert(conn, source="Bitunix", locale="EN", article_id="1", group_id="g1", category="campaign")
    _insert(conn, source="Bitunix", locale="FR", article_id="1", group_id="g1", category="campaign")
    conn.commit()

    call_count = {"n": 0}

    def fake_call_llm(*a, **k):
        call_count["n"] += 1
        return FAKE_LLM_JSON, 42

    monkeypatch.setattr("src.analysis.run.call_llm", fake_call_llm)

    from src.analysis.run import run
    report = run(conn, batch_date=BATCH_DATE, sources=("Bitunix",), dry_run=False)
    conn.commit()

    assert call_count["n"] == 1  # 只有 EN 真正调用了 LLM，FR 走复用
    assert report.derived == 1
    assert report.analyzed == 1

    from src.analysis.batch import compute_batch_id
    fr_row = conn.execute(
        "SELECT * FROM insights WHERE id = ?", (compute_batch_id("Bitunix", "campaign", "FR", BATCH_DATE),)
    ).fetchone()
    assert fr_row["is_locale_derived"] == 1
    assert fr_row["summary"] == "test summary"
    assert fr_row["llm_tokens_used"] == 0
    assert fr_row["derived_from_id"] == compute_batch_id("Bitunix", "campaign", "EN", BATCH_DATE)


def test_locale_with_region_exclusive_entry_does_not_derive(conn, fake_credentials, monkeypatch):
    _insert(conn, source="Bitunix", locale="EN", article_id="1", group_id="g1", category="campaign")
    _insert(conn, source="Bitunix", locale="FR", article_id="1", group_id="g1", category="campaign")
    _insert(conn, source="Bitunix", locale="FR", article_id="2", group_id="g2", category="campaign")
    conn.commit()

    call_count = {"n": 0}

    def fake_call_llm(*a, **k):
        call_count["n"] += 1
        return FAKE_LLM_JSON, 10

    monkeypatch.setattr("src.analysis.run.call_llm", fake_call_llm)

    from src.analysis.run import run
    report = run(conn, batch_date=BATCH_DATE, sources=("Bitunix",), dry_run=False)
    conn.commit()

    assert call_count["n"] == 2  # EN + FR 都要真正调用（FR 有独占条目，不能复用）
    assert report.derived == 0
    assert report.analyzed == 2


def test_token_cap_stops_before_next_uncached_batch(conn, fake_credentials, monkeypatch):
    _insert(conn, source="Bitunix", locale="EN", article_id="1", group_id="g1", category="campaign")
    _insert(conn, source="Weex", locale="EN", article_id="1", group_id="g2", category="campaign")
    conn.commit()
    calls = {"n": 0}

    def fake_call_llm(*a, **k):
        calls["n"] += 1
        return FAKE_LLM_JSON, 10

    monkeypatch.setattr("src.analysis.run.call_llm", fake_call_llm)

    from src.analysis.run import run
    report = run(
        conn, batch_date=BATCH_DATE, sources=("Bitunix", "Weex"),
        dry_run=False, max_tokens=5,
    )

    assert calls["n"] == 1
    assert report.llm_calls == 1
    assert report.skipped_token_cap == 1
