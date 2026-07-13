"""CLI: python -m src.probe --all

Phase 1 验收工具：对 config/sources.yaml 里已填的源发真实请求，确认能拿到
>=1 条真实公告；受阻的源打印明确原因。不做任何猜测请求。
"""

from __future__ import annotations

import argparse
import sys

from src.probe.core import DEFAULT_SOURCES_PATH, ProbeResult, load_sources, probe_all


def _print_table(results: list[ProbeResult]) -> None:
    header = f"{'exchange':<10} {'locale':<10} {'status':<8} {'http':<6} {'count':<6} note"
    print(header)
    print("-" * len(header))
    for r in results:
        http_str = str(r.http_code) if r.http_code is not None else "-"
        print(f"{r.source:<10} {r.locale:<10} {r.status:<8} {http_str:<6} {r.count:<6} {r.note}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m src.probe")
    parser.add_argument("--all", action="store_true", help="探测 sources.yaml 里所有源")
    parser.add_argument("--source", default=None, help="只探测单个 source（如 bitunix）")
    parser.add_argument(
        "--sources-path", default=str(DEFAULT_SOURCES_PATH), help="sources.yaml 路径"
    )
    args = parser.parse_args()

    if not args.all and not args.source:
        parser.error("需要 --all 或 --source <name>")

    sources = load_sources(args.sources_path)
    results = probe_all(sources, source_filter=args.source)
    _print_table(results)

    ok = sum(1 for r in results if r.status == "OK")
    blocked = sum(1 for r in results if r.status == "BLOCKED")
    failed = sum(1 for r in results if r.status == "FAIL")
    print(f"\n{ok} OK, {blocked} BLOCKED, {failed} FAIL (共 {len(results)} 个 source×locale)")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
