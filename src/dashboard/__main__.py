"""CLI: python -m src.dashboard --db-path <db> --out docs/data/dashboard.json

生产环境的定时任务只需要在每日采集/分析跑完后追加这一步，把最新的 db 状态导出成
静态 JSON，再把 docs/ 提交或部署到 GitHub Pages 即可——本模块不关心调度，只负责
"给定一个 db，产出这一份 JSON"这个单一职责，调度/发布本身留给后续 Phase 8。
"""
import argparse
import sys

from src.dashboard.export_data import export


def main() -> None:
    parser = argparse.ArgumentParser(description="导出竞品情报看板数据")
    parser.add_argument("--db-path", default="data/competitor_intel.db", help="SQLite 数据库路径")
    parser.add_argument("--out", default="docs/data/dashboard.json", help="输出 JSON 路径")
    args = parser.parse_args()

    data = export(args.db_path, args.out)
    print(f"导出完成：{args.out}")
    print(f"  as_of_date: {data['meta']['as_of_date']}")
    print(f"  insights: {data['meta']['insights_total']}（模拟 {data['meta']['insights_mock']}）")
    for source, info in data["sources"].items():
        status = "✓" if info["active"] else "✗ 无数据"
        print(f"  {source}: {status} 今日 {info['today']} / 累计 {info['total']}")


if __name__ == "__main__":
    sys.exit(main())
