"""Phase 3 pipeline CLI：

    python -m src.pipeline group-check
    python -m src.pipeline classify --dry-run
    python -m src.pipeline classify --apply
    python -m src.pipeline region --apply
    python -m src.pipeline dedup --apply
    python -m src.pipeline eval --sample 30

只处理 Bitunix + Weex（Zoomex 可选加入，Phemex/BingX/Lbank 的 collector 还不存在，
不在本 CLI 的默认 sources 范围内）。dedup 是例外——默认扫描全部源（含 Zoomex），
见 src/pipeline/dedup.py 顶部说明。
"""

from __future__ import annotations

import argparse

from src.db.connection import DEFAULT_DB_PATH, connect
from src.pipeline import category, dedup, eval as eval_mod, grouping, region
from src.pipeline.config import load_category_mapping, load_source_locales

DEFAULT_SOURCES = ("Bitunix", "Weex")


def cmd_group_check(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        source_locales = load_source_locales()
        sources = tuple(args.sources.split(",")) if args.sources else ("Bitunix", "Weex", "Zoomex")
        report = grouping.scan_group_consistency(conn, source_locales, sources=sources)
        grouping.print_report(report)
    finally:
        conn.close()


def cmd_classify(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        mapping = load_category_mapping()
        sources = tuple(args.sources.split(",")) if args.sources else DEFAULT_SOURCES
        if args.apply:
            counts = category.apply_layer1_layer2(conn, mapping, sources=sources)
            conn.commit()
            print(f"已写入 category 列。各 layer 命中数：{counts}")
        else:
            report = category.dry_run(conn, mapping, sources=sources, sample_size=args.sample)
            category.print_dry_run_report(report)
    finally:
        conn.close()


def cmd_region(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        source_locales = load_source_locales()
        sources = tuple(args.sources.split(",")) if args.sources else DEFAULT_SOURCES
        report = region.apply_region_exclusive(conn, source_locales, sources=sources)
        conn.commit()
        region.print_report(report)
    finally:
        conn.close()


def cmd_dedup(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        sources = tuple(args.sources.split(",")) if args.sources else None
        if args.apply:
            report = dedup.apply_dedup(conn, sources=sources)
            conn.commit()
        else:
            clusters = dedup.find_duplicate_clusters(conn, sources=sources)
            report = dedup.DedupReport(
                clusters_found=len(clusters),
                rows_marked=sum(len(c.duplicate_uids) for c in clusters),
                samples=clusters[:20],
            )
        dedup.print_report(report)
    finally:
        conn.close()


def cmd_eval(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        mapping = load_category_mapping()
        sources = tuple(args.sources.split(",")) if args.sources else ("Bitunix", "Weex", "Zoomex")
        rows = eval_mod.collect_classified_rows(conn, mapping, sources=sources)
        sample = eval_mod.stratified_sample(rows, target=args.sample)
        eval_mod.print_sample(sample)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    sub = parser.add_subparsers(dest="command", required=True)

    p_group = sub.add_parser("group-check", help="跨语言归组一致性防御性扫描")
    p_group.add_argument("--sources", help="逗号分隔，默认 Bitunix,Weex,Zoomex")
    p_group.set_defaults(func=cmd_group_check)

    p_classify = sub.add_parser("classify", help="第一层+第二层分类打标")
    p_classify.add_argument("--dry-run", action="store_true", default=True)
    p_classify.add_argument("--apply", action="store_true")
    p_classify.add_argument("--sample", type=int, default=20)
    p_classify.add_argument("--sources", help="逗号分隔，默认 Bitunix,Weex")
    p_classify.set_defaults(func=cmd_classify)

    p_region = sub.add_parser("region", help="地区独占标记（直接 apply，无 dry-run）")
    p_region.add_argument("--sources", help="逗号分隔，默认 Bitunix,Weex")
    p_region.set_defaults(func=cmd_region)

    p_dedup = sub.add_parser("dedup", help="同源同 locale 标题+正文完全一致的重复公告检测")
    p_dedup.add_argument("--dry-run", action="store_true", default=True)
    p_dedup.add_argument("--apply", action="store_true")
    p_dedup.add_argument("--sources", help="逗号分隔，默认全部源（含 Zoomex）")
    p_dedup.set_defaults(func=cmd_dedup)

    p_eval = sub.add_parser("eval", help="分层抽样打印分类结果供人工核对")
    p_eval.add_argument("--sample", type=int, default=30)
    p_eval.add_argument("--sources", help="逗号分隔，默认 Bitunix,Weex,Zoomex")
    p_eval.set_defaults(func=cmd_eval)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
