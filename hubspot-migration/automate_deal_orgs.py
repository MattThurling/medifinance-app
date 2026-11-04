"""Automated resolution of Funder / Introducer / Supplier free-text fields on
deals → Organisation FKs (replaces Phase 2D/2E/2F manual review).

For each dimension:
  1. Enumerate distinct raw values in deals_for_migration
  2. Junk values (N/A, blank, "-", None, unknown, none) → treated as "no org"
  3. Real values → normalise → fuzzy-match against raw_companies
       exact normalised match          → auto-link (score 1.0)
       SequenceMatcher ratio ≥ 0.85    → auto-link
       otherwise                       → create new Org from the raw value
  4. Emit one audit CSV per dimension so the client can eyeball outcomes

Result stored in table `decisions_deal_orgs`:
    dimension, raw_value, action, matched_org_id, match_score,
    created_new_org_name, deal_count

Downstream migration joins raw_deals against this table on trimmed raw value
to resolve the FK per deal.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "hubspot.db"
HERE = Path(__file__).resolve().parent

FUZZY_THRESHOLD = 0.85

JUNK_VALUES = {"", "n/a", "na", "-", "--", ".", "none", "null", "unknown",
               "not applicable", "tbc", "tba", "?"}

DIMENSIONS = [
    ("funder",     "Funder"),
    ("introducer", "Introducer"),
    ("supplier",   "Supplier"),
]


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r"\b(ltd\.?|limited|plc|llp|llc|inc\.?|corp(oration)?|the|"
               r"t/a|trading as|dba)\b", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_junk(s: str | None) -> bool:
    if not s:
        return True
    return s.strip().lower() in JUNK_VALUES


def build_org_index(conn: sqlite3.Connection):
    """Return (org_norm_index, org_keys) — org_norm_index maps norm→[(id,name)]
    and org_keys is a prefiltering list of (key, token_set)."""
    orgs = list(conn.execute(
        'SELECT "Record ID", "Company name" FROM raw_companies '
        'WHERE "Company name" IS NOT NULL'
    ))
    index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for oid, name in orgs:
        index[norm(name)].append((oid, name))
    keys = [(k, set(k.split())) for k in index if k]
    return index, keys


def match_org(typed: str, index, keys) -> tuple[str, str, float] | None:
    t = norm(typed)
    if not t:
        return None
    if t in index:
        oid, name = index[t][0]
        return (oid, name, 1.0)
    typed_tokens = set(t.split())
    best: tuple[str, str, float] | None = None
    for key, tokens in keys:
        if not (typed_tokens & tokens):
            continue
        s = SequenceMatcher(None, t, key).ratio()
        if s >= FUZZY_THRESHOLD and (best is None or s > best[2]):
            oid, name = index[key][0]
            best = (oid, name, s)
    return best


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    org_index, org_keys = build_org_index(conn)

    with conn:
        conn.execute("DROP TABLE IF EXISTS decisions_deal_orgs")
        conn.execute("""
            CREATE TABLE decisions_deal_orgs (
                dimension            TEXT NOT NULL,
                raw_value            TEXT NOT NULL,
                action               TEXT NOT NULL,
                matched_org_id       TEXT,
                match_score          REAL,
                created_new_org_name TEXT,
                deal_count           INTEGER NOT NULL,
                PRIMARY KEY (dimension, raw_value)
            )
        """)

        for dim_key, column in DIMENSIONS:
            distinct = list(conn.execute(f"""
                SELECT "{column}" AS v, COUNT(*) AS n
                FROM deals_for_migration
                WHERE "{column}" IS NOT NULL AND TRIM("{column}") <> ''
                GROUP BY "{column}"
                ORDER BY n DESC
            """))

            rows_for_table: list[tuple] = []
            counts: dict[str, int] = defaultdict(int)
            audit_rows: list[dict] = []
            for raw_value, n in distinct:
                if is_junk(raw_value):
                    action = "treated_as_junk"
                    row = (dim_key, raw_value, action, None, None, None, n)
                    audit_rows.append({
                        "raw_value": raw_value, "deal_count": n,
                        "action": action, "matched_org_name": "",
                        "matched_org_id": "", "match_score": "",
                        "created_new_org_name": "",
                    })
                else:
                    m = match_org(raw_value, org_index, org_keys)
                    if m:
                        action = "auto_matched"
                        row = (dim_key, raw_value, action, m[0], m[2], None, n)
                        audit_rows.append({
                            "raw_value": raw_value, "deal_count": n,
                            "action": action, "matched_org_name": m[1],
                            "matched_org_id": m[0], "match_score": f"{m[2]:.3f}",
                            "created_new_org_name": "",
                        })
                    else:
                        action = "created_new_org"
                        row = (dim_key, raw_value, action, None, None, raw_value.strip(), n)
                        audit_rows.append({
                            "raw_value": raw_value, "deal_count": n,
                            "action": action, "matched_org_name": "",
                            "matched_org_id": "", "match_score": "",
                            "created_new_org_name": raw_value.strip(),
                        })
                counts[action] += 1
                rows_for_table.append(row)

            conn.executemany(
                "INSERT INTO decisions_deal_orgs VALUES (?,?,?,?,?,?,?)",
                rows_for_table,
            )

            # Audit CSV per dimension
            p = HERE / f"automated_deal_{dim_key}_matches.csv"
            with p.open("w", newline="", encoding="utf-8") as f:
                fields = ["raw_value","deal_count","action",
                          "matched_org_name","matched_org_id","match_score",
                          "created_new_org_name"]
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                # Sort: junk first (usually few), then auto_matched by count desc,
                # then created_new_org by count desc.
                order = {"treated_as_junk": 0, "auto_matched": 1, "created_new_org": 2}
                audit_rows.sort(key=lambda r: (order[r["action"]], -r["deal_count"]))
                w.writerows(audit_rows)

            print(f"\n{dim_key} ({column}):")
            print(f"  distinct raw values:  {len(distinct)}")
            for a in ("auto_matched", "created_new_org", "treated_as_junk"):
                print(f"    {a:20s}  {counts.get(a, 0)}")
            print(f"  → {p.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
