"""CLI: python -m src.db <command>

目前只有 init。补数、重建等操作也应走这里新增子命令，
而不是让各 phase 自己写零散的建库脚本。
"""

from __future__ import annotations

import argparse

from src.db.connection import DEFAULT_DB_PATH, init_db


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m src.db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="按 schema.sql 建库（幂等）")
    init_parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite 文件路径，默认 {DEFAULT_DB_PATH}",
    )

    args = parser.parse_args()

    if args.command == "init":
        db_path = init_db(args.db_path)
        print(f"database initialized at {db_path}")


if __name__ == "__main__":
    main()
