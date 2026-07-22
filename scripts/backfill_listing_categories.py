"""对全部历史 Listing/Delisting 批次补跑币种赛道分类。

可安全重跑：每个 50 条分块有独立缓存；业务批次全部完成后才覆盖 insights。
"""
from __future__ import annotations

import argparse

from src.analysis.run import run
from src.db.connection import DEFAULT_DB_PATH, connect

COMPETITORS = ("Bitunix", "Weex", "BingX", "Phemex", "Lbank")
CATEGORIES = ("listing", "delisting")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--provider", choices=["openai_http", "cursor_agent"], default="cursor_agent")
    parser.add_argument("--source", help="逗号分隔；默认全部五家竞品")
    parser.add_argument("--max-calls", type=int, help="每个历史日期的调用上限；默认不限制")
    parser.add_argument("--max-tokens", type=int, help="每个历史日期的 token 上限；默认不限制")
    args = parser.parse_args()

    sources = tuple(args.source.split(",")) if args.source else COMPETITORS
    conn = connect(args.db)
    try:
        placeholders = ",".join("?" * len(sources))
        dates = [
            row[0] for row in conn.execute(
                f"""SELECT DISTINCT date(fetched_at)
                    FROM announcements
                    WHERE source IN ({placeholders})
                      AND category IN ('listing', 'delisting')
                      AND duplicate_of IS NULL
                    ORDER BY date(fetched_at)""",
                sources,
            ).fetchall() if row[0]
        ]
        totals = {"analyzed": 0, "derived": 0, "calls": 0, "tokens": 0, "failed": 0, "skipped": 0}
        for batch_date in dates:
            report = run(
                conn, batch_date=batch_date, sources=sources, categories=CATEGORIES,
                provider=args.provider, max_calls=args.max_calls, max_tokens=args.max_tokens,
                include_unchanged=True,
            )
            conn.commit()
            totals["analyzed"] += report.analyzed
            totals["derived"] += report.derived
            totals["calls"] += report.llm_calls
            totals["tokens"] += report.total_tokens
            totals["failed"] += report.validation_failed
            totals["skipped"] += report.skipped_budget_cap
            print(
                f"{batch_date}: analyzed={report.analyzed} derived={report.derived} "
                f"calls={report.llm_calls} tokens={report.total_tokens} "
                f"validation_failed={report.validation_failed} skipped={report.skipped_budget_cap}"
            )
        print("TOTAL " + " ".join(f"{key}={value}" for key, value in totals.items()))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
