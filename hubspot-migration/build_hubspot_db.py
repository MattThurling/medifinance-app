"""Load the three HubSpot CSV exports into a fresh hubspot.db SQLite file.

Raw stage only: every column stored as TEXT, original HubSpot column names
preserved verbatim (quoted). Empty strings become NULL. Idempotent — drops
and recreates each raw_* table on every run.

Sensitive fields (DOB, sort codes, account numbers, income, CCJ status) are
loaded as-is but never printed. Row counts and column counts are the only
per-table output.
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXPORTS = HERE / "hubspot exports"
DB_PATH = HERE / "hubspot.db"

SOURCES = [
    ("raw_contacts", EXPORTS / "all-contacts.csv"),
    ("raw_companies", EXPORTS / "all-companies.csv"),
    ("raw_deals", EXPORTS / "my-deals.csv"),
]


def dedupe_headers(header: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Return (unique_header, renames). Second+ occurrence of a name (matched
    case-insensitively, since SQLite identifiers are) gets a __2/__3 suffix so
    no column is silently dropped or merged."""
    counts: dict[str, int] = {}
    out: list[str] = []
    renames: list[tuple[str, str]] = []
    for name in header:
        key = name.casefold()
        counts[key] = counts.get(key, 0) + 1
        if counts[key] == 1:
            out.append(name)
        else:
            new = f"{name}__{counts[key]}"
            out.append(new)
            renames.append((name, new))
    return out, renames


def load_csv(conn: sqlite3.Connection, table: str, path: Path) -> tuple[int, int, list[tuple[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise SystemExit(f"{path.name}: empty file")

        header, renames = dedupe_headers(header)

        def q(ident: str) -> str:
            return '"' + ident.replace('"', '""') + '"'

        cols_ddl = ", ".join(f"{q(c)} TEXT" for c in header)
        conn.execute(f"DROP TABLE IF EXISTS {q(table)}")
        conn.execute(f"CREATE TABLE {q(table)} ({cols_ddl})")

        placeholders = ", ".join("?" for _ in header)
        insert_sql = f"INSERT INTO {q(table)} VALUES ({placeholders})"

        rows_written = 0
        batch: list[tuple] = []
        for row in reader:
            if len(row) != len(header):
                raise SystemExit(
                    f"{path.name}: row {rows_written + 1} has {len(row)} fields, "
                    f"header has {len(header)}"
                )
            batch.append(tuple(v if v != "" else None for v in row))
            if len(batch) >= 500:
                conn.executemany(insert_sql, batch)
                rows_written += len(batch)
                batch.clear()
        if batch:
            conn.executemany(insert_sql, batch)
            rows_written += len(batch)

    return len(header), rows_written, renames


def main() -> int:
    if not EXPORTS.is_dir():
        raise SystemExit(f"exports dir not found: {EXPORTS}")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        with conn:
            print(f"→ {DB_PATH.name}")
            for table, path in SOURCES:
                if not path.is_file():
                    raise SystemExit(f"missing source: {path}")
                cols, rows, renames = load_csv(conn, table, path)
                print(f"  {table}: {cols} cols, {rows} rows")
                for original, new in renames:
                    print(f"    renamed duplicate column {original!r} → {new!r}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
