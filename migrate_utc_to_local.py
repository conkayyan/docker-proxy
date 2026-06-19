"""一次性迁移:把数据库里历史 UTC 时间转换为系统本地时区。

背景:
- app.py 之前所有时间字段都用 datetime.utcnow() 写入,实际是 UTC。
- 改成 datetime.now() 后,新写入是 naive 本地时间。
- 直接切换会导致新旧数据相差若干小时,heartbeat_at 等比较逻辑失真。

本脚本把现存记录统一加上"系统本地时区偏移量",让旧数据对齐到新的本地时间口径。
运行后会提示 dry-run 与正式执行两种模式,默认 dry-run。

用法:
    python migrate_utc_to_local.py            # dry-run,只打印将要做的事
    python migrate_utc_to_local.py --apply    # 真正写入
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 数据库路径与 app.py 一致:instance/app.db
DB_PATH = Path(__file__).parent / "instance" / "app.db"

# 需要迁移的 (table, column) 对
TIME_COLUMNS: list[tuple[str, str]] = [
    ("users", "created_at"),
    ("registries", "created_at"),
    ("copy_tasks", "created_at"),
    ("copy_tasks", "started_at"),
    ("copy_tasks", "finished_at"),
    ("copy_tasks", "heartbeat_at"),
]


def local_offset() -> timedelta:
    """计算系统本地时区相对 UTC 的偏移量。

    用 now() 与 utcnow() 之差,避免依赖具体时区名字,跨部署通用。
    """
    sample_n = 3
    deltas = [
        datetime.now() - datetime.utcnow() for _ in range(sample_n)
    ]
    # 三次取样应一致;若不一致(例如夏令时跳变),取众数
    avg = sum(deltas, timedelta(0)) / sample_n
    return avg


def column_exists(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def migrate(dry_run: bool) -> int:
    if not DB_PATH.exists():
        print(f"database not found: {DB_PATH}", file=sys.stderr)
        return 2

    offset = local_offset()
    print(f"local offset vs UTC: {offset} ({offset.total_seconds() / 3600:.1f}h)")
    if offset == timedelta(0):
        print("offset is zero; nothing to do")
        return 0

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    total_rows = 0
    for table, column in TIME_COLUMNS:
        if not column_exists(cur, table, column):
            print(f"  skip {table}.{column} (column not found)")
            continue
        cur.execute(
            f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL"
        )
        rows = cur.fetchall()
        if not rows:
            print(f"  {table}.{column}: 0 non-null rows")
            continue
        # 形如 "2026-06-18 20:36:29.190797" —— SQLite 存的格式
        updated = 0
        for row_id, raw in rows:
            try:
                dt = datetime.fromisoformat(str(raw))
            except ValueError:
                print(f"    skip unparseable {table}.{column} id={row_id}: {raw!r}")
                continue
            new_dt = dt + offset
            new_raw = new_dt.isoformat(sep=" ")
            if dry_run:
                if updated < 3:
                    print(f"    {table}.{column} id={row_id}: {raw} -> {new_raw}")
            else:
                cur.execute(
                    f"UPDATE {table} SET {column} = ? WHERE id = ?",
                    (new_raw, row_id),
                )
            updated += 1
        print(f"  {table}.{column}: {updated} row(s) {'would be ' if dry_run else ''}updated")
        total_rows += updated

    if dry_run:
        conn.rollback()
        print(f"\ndry-run: {total_rows} row(s) would be updated. re-run with --apply to commit.")
    else:
        conn.commit()
        print(f"\napplied: {total_rows} row(s) updated.")
    conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write changes (default is dry-run)",
    )
    args = parser.parse_args()
    return migrate(dry_run=not args.apply)


if __name__ == "__main__":
    sys.exit(main())
