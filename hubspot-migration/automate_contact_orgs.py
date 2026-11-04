"""Automated contact-org resolution (replaces Phase 2C manual review).

Ruleset:
  deal + edge                     → keep, use edges
  deal + no edge + typed          → fuzzy-match typed name against orgs;
                                    auto-link if ≥ THRESHOLD or exact normalised;
                                    else mark 'create new org'
  deal + no edge + no typed       → keep; deal.organisation supplies context
  no deal + edge                  → keep; contact has real org linkage
                                    (likely supplier/lender/introducer staff)
  no deal + no edge               → drop

Writes:
  decisions_contact_orgs (SQLite table)   the authoritative decision per contact
  automated_created_orgs.csv              new Orgs to be created (client eyeball)
  automated_dropped_contacts.csv          audit trail of drops

Applies existing decisions from role_mailbox, duplicate_contacts and
name_normalisation before running.
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


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r"\b(ltd\.?|limited|plc|llp|llc|inc\.?|corp(oration)?|the|"
               r"t/a|trading as|dba)\b", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_ids(cell: str | None) -> list[str]:
    if not cell:
        return []
    return [x.strip() for x in cell.split(";") if x.strip()]


def main() -> int:
    conn = sqlite3.connect(DB_PATH)

    role  = dict(conn.execute("SELECT contact_id, action FROM decisions_role_mailbox"))
    dup   = dict(conn.execute("SELECT contact_id, action FROM decisions_duplicate_contacts"))

    # Build org name index for fuzzy lookup
    orgs = {oid: name for oid, name in conn.execute(
        'SELECT "Record ID", "Company name" FROM raw_companies WHERE "Company name" IS NOT NULL'
    )}
    org_norm_index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for oid, name in orgs.items():
        org_norm_index[norm(name)].append((oid, name))

    # All normalised keys with tokens, for prefiltering
    org_keys = [(k, set(k.split())) for k in org_norm_index if k]

    def fuzzy_match(typed: str) -> tuple[str, str, float] | None:
        """Return (org_id, org_name, score) if we can match typed to an existing
        Organisation with confidence ≥ FUZZY_THRESHOLD; else None. Exact
        normalised match short-circuits to score=1.0."""
        t = norm(typed)
        if not t:
            return None
        if t in org_norm_index:
            oid, name = org_norm_index[t][0]
            return (oid, name, 1.0)
        typed_tokens = set(t.split())
        best: tuple[str, str, float] | None = None
        for key, tokens in org_keys:
            if not (typed_tokens & tokens):
                continue
            s = SequenceMatcher(None, t, key).ratio()
            if s >= FUZZY_THRESHOLD and (best is None or s > best[2]):
                oid, name = org_norm_index[key][0]
                best = (oid, name, s)
        return best

    # Pre-compute deal linkage per contact
    contact_has_deal: dict[str, bool] = {
        cid: False for cid, in conn.execute('SELECT "Record ID" FROM raw_contacts')
    }
    for deal_id, cell in conn.execute(
        'SELECT "Record ID", "Associated Contact IDs" FROM raw_deals'
    ):
        for cid in parse_ids(cell):
            if cid in contact_has_deal:
                contact_has_deal[cid] = True

    # Pre-compute edges per contact
    edges_for: dict[str, list[str]] = defaultdict(list)
    for cid, oid in conn.execute(
        "SELECT contact_id, organisation_id FROM contact_organisation_edges"
    ):
        edges_for[cid].append(oid)

    # Now the main pass
    with conn:
        conn.execute("DROP TABLE IF EXISTS decisions_contact_orgs")
        conn.execute("""
            CREATE TABLE decisions_contact_orgs (
                contact_id        TEXT PRIMARY KEY,
                action            TEXT NOT NULL,
                linked_org_ids    TEXT,
                created_org_name  TEXT,
                match_score       REAL,
                drop_reason       TEXT
            )
        """)

        counts: dict[str, int] = defaultdict(int)
        created_rows: list[dict] = []
        dropped_rows: list[dict] = []
        insert_rows: list[tuple] = []

        for cid, first, last, email, typed, assoc_ids, primary_ids in conn.execute("""
            SELECT c."Record ID", c."First Name", c."Last Name", c."Email",
                   c."Company Name", c."Associated Company IDs",
                   c."Associated Company IDs (Primary)"
            FROM contacts_for_migration c
        """):
            # Exclude already-decided drops/merges
            if role.get(cid) in ("org_contact_method", "drop"):
                continue
            if dup.get(cid)  in ("merge_into_primary", "drop"):
                continue

            has_deal = contact_has_deal.get(cid, False)
            edges    = edges_for.get(cid, [])
            typed    = (typed or "").strip()

            action: str
            linked: list[str] = []
            created_name: str | None = None
            score: float | None = None
            drop_reason: str | None = None

            if not has_deal and not edges:
                action = "drop"
                drop_reason = "no deal + no edge"
                dropped_rows.append({
                    "contact_id": cid,
                    "first_name": first or "",
                    "last_name":  last or "",
                    "email":      email or "",
                    "typed_company_name": typed,
                    "drop_reason": drop_reason,
                })
            elif not has_deal and edges:
                action = "keep_no_deal"
                linked = edges
            elif has_deal and edges:
                action = "keep_with_edges"
                linked = edges
            elif has_deal and not edges and not typed:
                action = "keep_from_deal"
                # Migration will use Deal.organisation; no M2M edge yet
            else:
                # has_deal and not edges and typed
                match = fuzzy_match(typed)
                if match:
                    action = "keep_with_typed_match"
                    linked = [match[0]]
                    score  = match[2]
                else:
                    action = "keep_with_typed_new_org"
                    created_name = typed
                    created_rows.append({
                        "contact_id": cid,
                        "first_name": first or "",
                        "last_name":  last or "",
                        "email":      email or "",
                        "created_org_name": typed,
                    })

            counts[action] += 1
            insert_rows.append((
                cid, action,
                ";".join(linked) if linked else None,
                created_name,
                score,
                drop_reason,
            ))

        conn.executemany(
            "INSERT INTO decisions_contact_orgs VALUES (?,?,?,?,?,?)",
            insert_rows,
        )

    # Audit CSVs
    if created_rows:
        p = HERE / "automated_created_orgs.csv"
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=created_rows[0].keys())
            w.writeheader()
            w.writerows(created_rows)
    if dropped_rows:
        p = HERE / "automated_dropped_contacts.csv"
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=dropped_rows[0].keys())
            w.writeheader()
            w.writerows(dropped_rows)

    # Report
    total = sum(counts.values())
    surviving = total - counts.get("drop", 0)
    print(f"contacts evaluated: {total}")
    print(f"surviving:          {surviving}")
    print(f"dropped:            {counts.get('drop', 0)}")
    print()
    print("action distribution:")
    for a, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {n:>4}  {a}")

    n_created = counts.get("keep_with_typed_new_org", 0)
    if n_created:
        print(f"\n{n_created} new Orgs will be created from typed Company Names")
        print(f"  → automated_created_orgs.csv  (eyeball for typos/junk)")

    n_drop = counts.get("drop", 0)
    if n_drop:
        print(f"\n{n_drop} contacts dropped (no deal + no edge)")
        print(f"  → automated_dropped_contacts.csv  (audit trail)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
