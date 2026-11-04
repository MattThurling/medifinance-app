"""Per-column breakdown of the three raw_* tables in hubspot.db.

Writes `field_breakdown_<table>.csv` with columns:
  column, non_null, null_count, coverage_pct,
  distinct_populated, most_common, most_common_count, most_common_pct

For contact columns matching sensitive-field patterns (DOB, sort code, account
number, income, CCJ, NI number, passport, salary, bank/account name) the
most_common value is replaced with <REDACTED> — counts still reported.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "hubspot.db"
TABLES = ["raw_contacts", "raw_companies", "raw_deals"]

# Sensitive field patterns — matched case-insensitively against column names.
# Only applied to raw_contacts (per findings-so-far.md).
SENSITIVE_PATTERNS = [
    r"\bdob\b", r"\bbirth\b", r"sort\s*code", r"account\s*number",
    r"\baccount\s*no\b", r"\baccount\s*name\b", r"\bincome\b", r"\bsalary\b",
    r"\bccj\b", r"ni\s*number", r"national\s*insurance", r"\bpassport\b",
    r"\bbank\b",
]
SENSITIVE_RE = re.compile("|".join(SENSITIVE_PATTERNS), re.IGNORECASE)

MODE_STR_LIMIT = 80  # truncate long mode values for readability


def q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def breakdown(conn: sqlite3.Connection, table: str) -> tuple[int, list[dict]]:
    total = conn.execute(f"SELECT COUNT(*) FROM {q(table)}").fetchone()[0]
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({q(table)})")]
    is_contacts = table == "raw_contacts"

    rows: list[dict] = []
    for c in cols:
        non_null = conn.execute(f"SELECT COUNT({q(c)}) FROM {q(table)}").fetchone()[0]
        null_n = total - non_null
        coverage_pct = round(100.0 * non_null / total, 2) if total else 0.0

        if non_null == 0:
            distinct = 0
            mode_val: str | None = None
            mode_n = 0
            mode_pct = 0.0
        else:
            distinct = conn.execute(
                f"SELECT COUNT(DISTINCT {q(c)}) FROM {q(table)} WHERE {q(c)} IS NOT NULL"
            ).fetchone()[0]
            mode_row = conn.execute(
                f"SELECT {q(c)}, COUNT(*) FROM {q(table)} "
                f"WHERE {q(c)} IS NOT NULL GROUP BY {q(c)} "
                f"ORDER BY COUNT(*) DESC LIMIT 1"
            ).fetchone()
            mode_val, mode_n = mode_row
            mode_pct = round(100.0 * mode_n / non_null, 2)

            if is_contacts and SENSITIVE_RE.search(c):
                mode_val = "<REDACTED>"
            elif mode_val is not None:
                mode_val = str(mode_val)
                if len(mode_val) > MODE_STR_LIMIT:
                    mode_val = mode_val[: MODE_STR_LIMIT - 3] + "..."

        rows.append({
            "column": c,
            "non_null": non_null,
            "null_count": null_n,
            "coverage_pct": coverage_pct,
            "distinct_populated": distinct,
            "most_common": mode_val if mode_val is not None else "",
            "most_common_count": mode_n,
            "most_common_pct": mode_pct,
        })
    return total, rows


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = [
        "column", "non_null", "null_count", "coverage_pct",
        "distinct_populated", "most_common", "most_common_count", "most_common_pct",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def summarise(table: str, total: int, rows: list[dict]) -> None:
    n_cols = len(rows)
    empty = sum(1 for r in rows if r["non_null"] == 0)
    single_val = sum(1 for r in rows if r["distinct_populated"] == 1 and r["non_null"] > 0)
    full = sum(1 for r in rows if r["coverage_pct"] == 100.0)
    partial = sum(1 for r in rows if 0 < r["coverage_pct"] < 100.0)
    print(f"\n{table}  ({total} rows, {n_cols} columns)")
    print(f"  100% populated:      {full}")
    print(f"  partially populated: {partial}")
    print(f"  totally empty:       {empty}")
    print(f"  single-value junk:   {single_val}  (distinct=1, coverage>0)")

    top = sorted(
        (r for r in rows if r["non_null"] > 0),
        key=lambda r: (-r["coverage_pct"], r["column"]),
    )[:12]
    print(f"  top {len(top)} by coverage:")
    for r in top:
        mc = r["most_common"] or ""
        print(
            f"    {r['column'][:48]:48s}  "
            f"{r['coverage_pct']:>6.2f}%  distinct={r['distinct_populated']:<5d}  "
            f"mode={mc[:35]!r} ({r['most_common_pct']}%)"
        )


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        for table in TABLES:
            total, rows = breakdown(conn, table)
            out = HERE / f"field_breakdown_{table}.csv"
            write_csv(out, rows)
            summarise(table, total, rows)
            print(f"  → wrote {out.name}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
