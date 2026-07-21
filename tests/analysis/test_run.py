"""src/analysis/run.py 单测：dry_run 模式不写库/不调 LLM、category=other 被跳过、
staged-v1 两段编排（Stage1 每篇一次调用 + Stage3 每批一次调用，Phase②起）、批次级
upsert、locale 复用（EN 先跑完才能被 FR 复用）、llm_cache 命中跳过重复调用、budget
熔断可能在 Stage1/Stage3 任一段触发（比旧版单体调用更细粒度）。全部离线：真实
schema.sql 建的临时库 + monkeypatch 掉 call_llm / load_llm_credentials。
"""

from __future__ import annotations

import json

import pytest

from src.analysis.config import LlmCredentials
from src.analysis.prompts import SYSTEM_BUSINESS_JUDGMENT, SYSTEM_FACT_EXTRACTION
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


def _stage1_response(index: int, event_type: str = "created", tokens_hint: int = 10) -> str:
    return json.dumps({"i": index, "event_type": event_type, "confidence": 0.9})


def _stage3_response(indices: list[int], gap_type: str = "not_applicable") -> str:
    return json.dumps({"items": [
        {"i": i, "gap_type": gap_type, "business_impact": "low", "novelty": 0, "urgency": 0,
         "zmx_evidence": [], "reason": "r"} for i in indices
    ]})


def make_fake_call_llm(*, tokens_per_call: int = 10):
    """通用 staged-v1 双段 fake：按 system prompt 区分 Stage1（单篇）/Stage3（批量），
    从 user payload 里解析出真实的 i / items 索引，不是写死返回值。"""
    call_count = {"n": 0}

    def fake_call_llm(system, user, **kwargs):
        call_count["n"] += 1
        if system == SYSTEM_FACT_EXTRACTION:
            payload = json.loads(user.split("input:\n", 1)[1].split("\nrequired_schema:", 1)[0])
            return _stage1_response(payload["i"]), tokens_per_call
        assert system == SYSTEM_BUSINESS_JUDGMENT
        payload = json.loads(user.split("input:\n", 1)[1].split("\noutput_schema:", 1)[0])
        indices = [item["i"] for item in payload["items"]]
        return _stage3_response(indices), tokens_per_call

    return fake_call_llm, call_count


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

    fake_call_llm, call_count = make_fake_call_llm()
    monkeypatch.setattr("src.analysis.run.call_llm", fake_call_llm)

    from src.analysis.batch import compute_batch_id
    from src.analysis.run import run

    report = run(conn, batch_date=BATCH_DATE, sources=("Bitunix",), dry_run=False)
    conn.commit()

    # 1 篇文章 = Stage1 1 次 + Stage3 1 次
    assert call_count["n"] == 2
    assert report.analyzed == 1
    assert report.llm_calls == 2
    assert report.total_tokens == 20

    insight_id = compute_batch_id("Bitunix", "campaign", "EN", BATCH_DATE)
    row = conn.execute("SELECT * FROM insights WHERE id = ?", (insight_id,)).fetchone()
    assert row is not None
    assert row["article_count"] == 1
    assert json.loads(row["related_uids"]) == [uid]
    assert row["is_locale_derived"] == 0
    assert row["prompt_version"] == "article-facts-v1+business-judgment-v1"
    assert row["diff_type"] == "不适用"  # gap_type=not_applicable 的默认 fake 响应
    assert row["llm_tokens_used"] == 20

    articles = json.loads(row["articles_analysis"])
    assert articles[0]["uid"] == uid
    assert articles[0]["event_type"] == "created"
    assert articles[0]["diff_type"] == "不适用"


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

    fake_call_llm, call_count = make_fake_call_llm()
    monkeypatch.setattr("src.analysis.run.call_llm", fake_call_llm)

    from src.analysis.run import run
    run(conn, batch_date=BATCH_DATE, sources=("Bitunix",), dry_run=False)
    conn.commit()
    report2 = run(conn, batch_date=BATCH_DATE, sources=("Bitunix",), dry_run=False)
    conn.commit()

    assert call_count["n"] == 2  # 第一轮 Stage1+Stage3；第二轮全部命中缓存，没有真的再调
    assert report2.cache_hits == 2  # Stage1 + Stage3 各命中一次
    assert report2.llm_calls == 0


def test_locale_derives_from_en_without_calling_llm(conn, fake_credentials, monkeypatch):
    _insert(conn, source="Bitunix", locale="EN", article_id="1", group_id="g1", category="campaign")
    _insert(conn, source="Bitunix", locale="FR", article_id="1", group_id="g1", category="campaign")
    conn.commit()

    fake_call_llm, call_count = make_fake_call_llm()
    monkeypatch.setattr("src.analysis.run.call_llm", fake_call_llm)

    from src.analysis.run import run
    report = run(conn, batch_date=BATCH_DATE, sources=("Bitunix",), dry_run=False)
    conn.commit()

    assert call_count["n"] == 2  # 只有 EN 真正调用了 LLM（Stage1+Stage3），FR 走复用
    assert report.derived == 1
    assert report.analyzed == 1

    from src.analysis.batch import compute_batch_id
    en_row = conn.execute(
        "SELECT * FROM insights WHERE id = ?", (compute_batch_id("Bitunix", "campaign", "EN", BATCH_DATE),)
    ).fetchone()
    fr_row = conn.execute(
        "SELECT * FROM insights WHERE id = ?", (compute_batch_id("Bitunix", "campaign", "FR", BATCH_DATE),)
    ).fetchone()
    assert fr_row["is_locale_derived"] == 1
    assert fr_row["summary"] == en_row["summary"]
    assert fr_row["llm_tokens_used"] == 0
    assert fr_row["derived_from_id"] == compute_batch_id("Bitunix", "campaign", "EN", BATCH_DATE)


def test_locale_with_region_exclusive_entry_does_not_derive(conn, fake_credentials, monkeypatch):
    # content 故意各不相同：Stage1 的缓存 key 只按 content_hash 算，同一份内容会
    # 天然复用同一次事实抽取（这本身是正确行为），这里要测的是"FR 批次不能整批
    # 复用 EN 批次"，用不同内容避免碰巧撞上 Stage1 的 content_hash 级缓存。
    _insert(conn, source="Bitunix", locale="EN", article_id="1", group_id="g1", category="campaign", content="EN content")
    _insert(conn, source="Bitunix", locale="FR", article_id="1", group_id="g1", category="campaign", content="FR content g1")
    _insert(conn, source="Bitunix", locale="FR", article_id="2", group_id="g2", category="campaign", content="FR content g2")
    conn.commit()

    fake_call_llm, call_count = make_fake_call_llm()
    monkeypatch.setattr("src.analysis.run.call_llm", fake_call_llm)

    from src.analysis.run import run
    report = run(conn, batch_date=BATCH_DATE, sources=("Bitunix",), dry_run=False)
    conn.commit()

    # EN（1 篇）= Stage1 1 + Stage3 1；FR（2 篇，有独占条目不能复用）= Stage1 2 + Stage3 1
    assert call_count["n"] == 5
    assert report.derived == 0
    assert report.analyzed == 2


def test_budget_cap_can_stop_mid_stage1_or_before_stage3(conn, fake_credentials, monkeypatch):
    """新的 staged 流程下，熔断粒度比旧版更细：一个批次可能 Stage1 跑完了但 Stage3
    没跑（本例：Bitunix 1 篇文章耗尽预算后，Stage3 检查熔断触发，整批仍然跳过，不
    写不完整的 insight），也可能 Stage1 都没机会开始（Weex）。"""
    _insert(conn, source="Bitunix", locale="EN", article_id="1", group_id="g1", category="campaign")
    _insert(conn, source="Weex", locale="EN", article_id="1", group_id="g2", category="campaign")
    conn.commit()

    fake_call_llm, call_count = make_fake_call_llm(tokens_per_call=10)
    monkeypatch.setattr("src.analysis.run.call_llm", fake_call_llm)

    from src.analysis.run import run
    report = run(
        conn, batch_date=BATCH_DATE, sources=("Bitunix", "Weex"),
        dry_run=False, max_tokens=5,
    )

    # Bitunix: Stage1 的第 1 次调用在熔断触发前就已经发出（budget 检查发生在调用前，
    # 初始 total_tokens=0 < 5），之后 Stage3 检查熔断触发、跳过；Weex: 连 Stage1 第
    # 一次调用都没有机会发出（此时 total_tokens=10 >= 5）。
    assert call_count["n"] == 1
    assert report.llm_calls == 1
    assert report.analyzed == 0
    assert report.skipped_budget_cap == 2
    assert conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0] == 0
