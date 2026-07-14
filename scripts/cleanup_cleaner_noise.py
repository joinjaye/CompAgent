"""清理"改清洗规则 -> --force-full"两步操作产生的技术噪音。

背景：`--force-full` 本身不会消化噪音，只会**制造**噪音——任何一次 html_text.py/
slate_json.py 清洗规则的改动，都会让存量数据的 content_hash 变化，`--force-full`
重新落库时会把这些行判成 status='changed'，把"清洗规则升级前的旧文本"当成一个
"真实的旧版本"塞进 content_history。这批 changed/history 从来不是源站发布过的
任何东西，纯粹是解析器的产物，如果不清理，会在 Phase 4（LLM 拿 content_history
对比"改了什么"）和 Phase 6（status=changed 触发推送规则）里被当成真实公告变更
处理，产生幻觉分析结论。

正确的两步流程（缺一不可，见 CLAUDE.md「Phase 2.8」）：
    1. 对受影响的源跑一次 `python -m src.collectors ... --force-full`
       （这一步产生噪音，不是消除）
    2. 紧接着跑本脚本，把第 1 步产生的噪音抹掉：
       - DELETE 对应的 content_history 行
       - 把 status='changed' 的行重置成 'new'（不是 'unchanged'——这些行还没有被
         任何下游处理过，只是不该被当成"发生过变更"）

用法：
    # 按 content_history.captured_at 时间窗圈定范围（先用 --histogram 看分布，
    # 确认这批记录确实聚集在目标窗口，不要凭猜测传时间戳）
    python scripts/cleanup_cleaner_noise.py --histogram
    python scripts/cleanup_cleaner_noise.py --start 2026-07-14T03:00:00Z --end 2026-07-14T05:00:00Z
    python scripts/cleanup_cleaner_noise.py --start ... --end ... --apply

    # 或者直接给一个 uid 列表（一行一个 uid），跳过时间窗猜测
    python scripts/cleanup_cleaner_noise.py --uid-file uids.txt --apply

默认是 dry-run：只打印范围 + 抽样 before/after diff，不改库。要求显式传 --apply
才会真正执行 DELETE/UPDATE。幂等：对已经清理过的范围重跑，--apply 是安全的 no-op
（scope 为空或 changed_count/history_count 为 0）。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db.connection import DEFAULT_DB_PATH, connect  # noqa: E402


@dataclass
class CleanupResult:
    uids: list[str] = field(default_factory=list)
    history_count: int = 0
    changed_count: int = 0
    applied: bool = False


def print_histogram(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT captured_at, COUNT(*) FROM content_history GROUP BY captured_at ORDER BY captured_at"
    ).fetchall()
    print("content_history.captured_at 分布：")
    for captured_at, count in rows:
        print(f"  {captured_at}  {count}")
    total = sum(r[1] for r in rows)
    print(f"  合计 {total} 行")


def resolve_uids(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
    uids: list[str] | None = None,
) -> list[str]:
    if uids is not None:
        return list(uids)
    rows = conn.execute(
        "SELECT DISTINCT uid FROM content_history WHERE captured_at >= ? AND captured_at <= ?",
        (start, end),
    ).fetchall()
    return [r[0] for r in rows]


def cleanup(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    start: str | None = None,
    end: str | None = None,
    uids: list[str] | None = None,
    apply: bool = False,
    sample: int = 10,
    verbose: bool = True,
) -> CleanupResult:
    if uids is None and not (start and end):
        raise ValueError("必须传 uids，或者同时传 start 和 end")

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        scope_uids = resolve_uids(conn, start=start, end=end, uids=uids)
        if not scope_uids:
            if verbose:
                print("范围内没有匹配的 content_history 行，无需处理。")
            return CleanupResult(uids=[], history_count=0, changed_count=0, applied=False)

        placeholders = ",".join("?" * len(scope_uids))

        history_count = conn.execute(
            f"SELECT COUNT(*) FROM content_history WHERE uid IN ({placeholders})", scope_uids
        ).fetchone()[0]
        changed_count = conn.execute(
            f"SELECT COUNT(*) FROM announcements WHERE status='changed' AND uid IN ({placeholders})",
            scope_uids,
        ).fetchone()[0]

        if verbose:
            print(f"范围内 uid 数：{len(scope_uids)}")
            print(f"content_history 待删行数：{history_count}")
            print(f"announcements.status='changed' 待重置行数：{changed_count}")

            sample_rows = conn.execute(
                f"""
                SELECT ch.uid, a.source, a.locale, a.article_id, a.title,
                       ch.content AS old_content, a.content AS new_content
                FROM content_history ch
                JOIN announcements a ON a.uid = ch.uid
                WHERE ch.uid IN ({placeholders})
                LIMIT ?
                """,
                scope_uids + [sample],
            ).fetchall()
            print(f"\n--- 抽样 {len(sample_rows)} 条 before/after ---")
            for row in sample_rows:
                print("=" * 80)
                print(f"{row['source']} {row['locale']} {row['article_id']} {row['title']!r}")
                print(f"OLD: {row['old_content'][:200]!r}")
                print(f"NEW: {row['new_content'][:200]!r}")

        if not apply:
            if verbose:
                print("\ndry-run，未改库。加 --apply 才会真正执行。")
            return CleanupResult(uids=scope_uids, history_count=history_count, changed_count=changed_count, applied=False)

        conn.isolation_level = None
        conn.execute("BEGIN")
        try:
            conn.execute(f"DELETE FROM content_history WHERE uid IN ({placeholders})", scope_uids)
            conn.execute(
                f"UPDATE announcements SET status='new' WHERE status='changed' AND uid IN ({placeholders})",
                scope_uids,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        if verbose:
            print(
                f"\n已执行：删除 content_history {history_count} 行，"
                f"重置 announcements.status='changed' -> 'new' 共 {changed_count} 行。"
            )
        return CleanupResult(uids=scope_uids, history_count=history_count, changed_count=changed_count, applied=True)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--start", help="captured_at 范围起点（含），UTC ISO8601")
    parser.add_argument("--end", help="captured_at 范围终点（含），UTC ISO8601")
    parser.add_argument("--uid-file", help="uid 列表文件（一行一个），与 --start/--end 二选一")
    parser.add_argument("--sample", type=int, default=10, help="打印几条 before/after diff（默认 10）")
    parser.add_argument("--histogram", action="store_true", help="只打印 content_history.captured_at 分布后退出")
    parser.add_argument("--apply", action="store_true", help="真正执行删除/重置（默认 dry-run）")
    args = parser.parse_args()

    if args.histogram:
        conn = connect(args.db)
        try:
            print_histogram(conn)
        finally:
            conn.close()
        return

    uids = None
    if args.uid_file:
        uids = [line.strip() for line in Path(args.uid_file).read_text().splitlines() if line.strip()]
    elif not (args.start and args.end):
        parser.error("必须传 --uid-file，或者同时传 --start 和 --end（也可以只传 --histogram 看分布）")

    cleanup(args.db, start=args.start, end=args.end, uids=uids, apply=args.apply, sample=args.sample)


if __name__ == "__main__":
    main()
