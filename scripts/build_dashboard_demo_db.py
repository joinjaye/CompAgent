"""
Phase 7（可视化看板）专用：把三个各自独立的真实采集库合并成一份 demo 库
（data/dashboard_demo.db），供 src/dashboard 导出脚本测试用。

这不是生产链路的一部分——生产的 python -m src.dashboard 直接指向真实的单一日常库
（如 data/competitor_intel.db），不需要这个合并步骤。本脚本只解决一个 demo 数据的
现实问题：当前没有任何一个真实库同时具备"多个竞品 × 当天新增 × 真实分类打标"的
完整状态（Weex 因「Weex 路径问题」暂停采集；Bitunix 的完整当日样本、BingX、
Phemex、Lbank 的真实分类结果分散在三个不同 session 各自建的库里），所以把它们
的真实数据（不是编造数据，都是真实抓取/真实分类结果）拼到一起，让看板有足够
丰富的真实基础可以展示。

合并内容：
- Bitunix（32 条真实当日样本）+ Zoomex（2018 条真实基线）+ 7 条真实 insights，
  原样取自 data/test_daily_20260715.db，不做任何改动。
- BingX（40 条，真实分类结果）取自 data/competitor_intel.db。
- Phemex（178 条）+ Lbank（1679 条，真实分类结果）取自
  data/run_20260715_bitunix_phemex_lbank.db。
- Weex 不合并（0 条）——如实反映"Weex 路径问题"导致采集暂停的真实现状，看板会
  显式标出这个源当前无数据，而不是编造假数据掩盖过去。

时间戳归一化（唯一的非原样搬运）：BingX/Phemex/Lbank 三个源的 fetched_at 原始值
分散在 2026-07-14 和 2026-07-15 两个真实日期（因为它们是不同 session 在不同时刻
真实抓取的），只改写 fetched_at 的日期部分（保留原始时分秒）到 2026-07-15，让
它们在 export_data.py 的"今日"窗口判断里对齐成同一天，看起来像一次连贯的每日
批次——这是为了让 demo 呈现一个可读的"今日概览"，不是编造内容本身（title/content/
category/post_time 等业务字段完全不动）。生产环境不需要这一步：真实的每日调度会
让 fetched_at 天然落在同一天。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db.connection import init_db  # noqa: E402

DEMO_DB = ROOT / "data" / "dashboard_demo.db"
TEST_DAILY_DB = ROOT / "data" / "test_daily_20260715.db"
COMPETITOR_DB = ROOT / "data" / "competitor_intel.db"
RUN_DB = ROOT / "data" / "run_20260715_bitunix_phemex_lbank.db"
TODAY = "2026-07-15"

ANNOUNCEMENT_COLS = (
    "uid, group_id, source, locale, article_id, url, title, content, raw_category, "
    "content_hash, post_time, update_time, fetched_at, status, category, "
    "is_region_exclusive, push_status, source_endpoint"
)

INSIGHT_COLS = (
    "id, batch_date, source, category, locale, article_count, related_uids, "
    "is_locale_derived, derived_from_id, summary, articles_analysis, zmx_diff, "
    "diff_type, priority, zmx_evidence_uids, prompt_version, llm_tokens_used, "
    "created_at, updated_at"
)


def main() -> None:
    if DEMO_DB.exists():
        DEMO_DB.unlink()
    init_db(str(DEMO_DB))

    conn = sqlite3.connect(str(DEMO_DB))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(f"ATTACH DATABASE '{TEST_DAILY_DB}' AS test_daily")
    conn.execute(f"ATTACH DATABASE '{COMPETITOR_DB}' AS main_db")
    conn.execute(f"ATTACH DATABASE '{RUN_DB}' AS run_db")

    # Bitunix（32，今日真实样本）+ Zoomex（2018，真实基线）：原样搬运，不改任何字段
    conn.execute(
        f"INSERT INTO announcements ({ANNOUNCEMENT_COLS}) "
        f"SELECT {ANNOUNCEMENT_COLS} FROM test_daily.announcements "
        f"WHERE source IN ('Bitunix', 'Zoomex')"
    )
    conn.execute(
        f"INSERT INTO insights ({INSIGHT_COLS}) SELECT {INSIGHT_COLS} FROM test_daily.insights"
    )

    # BingX（40，真实分类结果）：只归一化 fetched_at 的日期部分
    conn.execute(
        f"""INSERT INTO announcements ({ANNOUNCEMENT_COLS})
            SELECT uid, group_id, source, locale, article_id, url, title, content, raw_category,
                   content_hash, post_time, update_time,
                   '{TODAY}' || substr(fetched_at, 11) AS fetched_at,
                   status, category, is_region_exclusive, push_status, source_endpoint
            FROM main_db.announcements WHERE source = 'BingX'"""
    )

    # Phemex + Lbank（真实分类结果）：同样只归一化 fetched_at 日期部分
    conn.execute(
        f"""INSERT INTO announcements ({ANNOUNCEMENT_COLS})
            SELECT uid, group_id, source, locale, article_id, url, title, content, raw_category,
                   content_hash, post_time, update_time,
                   '{TODAY}' || substr(fetched_at, 11) AS fetched_at,
                   status, category, is_region_exclusive, push_status, source_endpoint
            FROM run_db.announcements WHERE source IN ('Phemex', 'Lbank')"""
    )

    conn.commit()

    counts = conn.execute(
        "SELECT source, COUNT(*) FROM announcements GROUP BY source ORDER BY source"
    ).fetchall()
    insight_count = conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0]
    conn.execute("DETACH DATABASE test_daily")
    conn.execute("DETACH DATABASE main_db")
    conn.execute("DETACH DATABASE run_db")
    conn.close()

    print(f"built {DEMO_DB}")
    for source, n in counts:
        print(f"  {source}: {n}")
    print(f"  insights: {insight_count}")


if __name__ == "__main__":
    main()
