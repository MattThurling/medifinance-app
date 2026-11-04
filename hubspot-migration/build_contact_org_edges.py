"""Build `contact_organisation_edges` — one row per (contact, organisation) pair
combining every source HubSpot gives us, with provenance flags.

Sources merged:
  1. contact_export     — raw_contacts."Associated Company IDs"
  2. contact_primary    — raw_contacts."Associated Company IDs (Primary)"
  3. company_export     — raw_companies."Associated Contact IDs"
  4. deal_inference     — cross of raw_deals's contact IDs × company IDs

The table exposes each edge with its provenance flags, the deal IDs that
support any deal-inferred edge, and a corroboration count — so the migration
can trust the M2M `Contact.organisations` field while retaining an audit
trail showing where each link came from.

Raw data is left untouched. Re-run to rebuild after CSV re-import.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "hubspot.db"


def parse_ids(cell: str | None) -> list[str]:
    if not cell:
        return []
    return [x.strip() for x in cell.split(";") if x.strip()]


def main() -> int:
    conn = sqlite3.connect(DB_PATH)

    # edge → dict of {contact_export, contact_primary, company_export, deal_inference}
    flags: dict[tuple[str, str], dict[str, bool]] = defaultdict(
        lambda: {"contact_export": False, "contact_primary": False,
                 "company_export": False, "deal_inference": False}
    )
    # edge → set of deal_ids that contributed via deal_inference
    deal_ids: dict[tuple[str, str], set[str]] = defaultdict(set)

    # Source 1: contact_export
    for contact_id, cell in conn.execute(
        'SELECT "Record ID", "Associated Company IDs" FROM raw_contacts'
    ):
        for org_id in parse_ids(cell):
            flags[(contact_id, org_id)]["contact_export"] = True

    # Source 2: contact_primary
    for contact_id, cell in conn.execute(
        'SELECT "Record ID", "Associated Company IDs (Primary)" FROM raw_contacts'
    ):
        for org_id in parse_ids(cell):
            flags[(contact_id, org_id)]["contact_primary"] = True

    # Source 3: company_export
    for org_id, cell in conn.execute(
        'SELECT "Record ID", "Associated Contact IDs" FROM raw_companies'
    ):
        for contact_id in parse_ids(cell):
            flags[(contact_id, org_id)]["company_export"] = True

    # Source 4: deal_inference — cross of deal's contacts × deal's companies
    for deal_id, c_cell, co_cell in conn.execute(
        'SELECT "Record ID", "Associated Contact IDs", "Associated Company IDs" FROM raw_deals'
    ):
        contacts = parse_ids(c_cell)
        companies = parse_ids(co_cell)
        if not contacts or not companies:
            continue
        for c in contacts:
            for co in companies:
                flags[(c, co)]["deal_inference"] = True
                deal_ids[(c, co)].add(deal_id)

    # Write into a fresh table
    with conn:
        conn.execute("DROP TABLE IF EXISTS contact_organisation_edges")
        conn.execute("""
            CREATE TABLE contact_organisation_edges (
                contact_id            TEXT NOT NULL,
                organisation_id       TEXT NOT NULL,
                from_contact_export   INTEGER NOT NULL DEFAULT 0,
                from_contact_primary  INTEGER NOT NULL DEFAULT 0,
                from_company_export   INTEGER NOT NULL DEFAULT 0,
                from_deal_inference   INTEGER NOT NULL DEFAULT 0,
                corroboration_count   INTEGER NOT NULL,
                source_deal_ids       TEXT,
                PRIMARY KEY (contact_id, organisation_id)
            )
        """)
        conn.execute(
            "CREATE INDEX contact_org_edges_contact_ix "
            "ON contact_organisation_edges(contact_id)"
        )
        conn.execute(
            "CREATE INDEX contact_org_edges_org_ix "
            "ON contact_organisation_edges(organisation_id)"
        )

        rows = []
        for edge, f in flags.items():
            contact_id, org_id = edge
            corr = sum(int(v) for v in f.values())
            deals = ";".join(sorted(deal_ids.get(edge, ()))) or None
            rows.append((
                contact_id, org_id,
                int(f["contact_export"]), int(f["contact_primary"]),
                int(f["company_export"]), int(f["deal_inference"]),
                corr, deals,
            ))
        conn.executemany(
            "INSERT INTO contact_organisation_edges VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )

    # Report
    total = conn.execute("SELECT COUNT(*) FROM contact_organisation_edges").fetchone()[0]
    print(f"contact_organisation_edges: {total} unique edges")

    print("\nCoverage by source:")
    for col in ("from_contact_export", "from_contact_primary",
                "from_company_export", "from_deal_inference"):
        n = conn.execute(
            f"SELECT COUNT(*) FROM contact_organisation_edges WHERE {col} = 1"
        ).fetchone()[0]
        print(f"  {col:<24s}  {n:>5}  ({100*n/total:.1f}%)")

    print("\nCorroboration (edges by # of sources agreeing):")
    for r in conn.execute("""
        SELECT corroboration_count, COUNT(*)
        FROM contact_organisation_edges
        GROUP BY corroboration_count
        ORDER BY corroboration_count
    """):
        print(f"  {r[0]} source(s):  {r[1]:>5}  ({100*r[1]/total:.1f}%)")

    # The interesting recovery: edges that ONLY exist via deal inference —
    # these are the 46-ish "no link in either export" edges the triangle
    # check surfaced.
    n_deal_only = conn.execute("""
        SELECT COUNT(*) FROM contact_organisation_edges
        WHERE from_deal_inference = 1
          AND from_contact_export = 0
          AND from_contact_primary = 0
          AND from_company_export = 0
    """).fetchone()[0]
    print(f"\nEdges recovered only via deal_inference: {n_deal_only}")
    print("  (contact-org pairs HubSpot doesn't link explicitly but the deal implies)")

    # Contacts with more than one associated organisation — the M2M cases
    multi = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT contact_id FROM contact_organisation_edges
            GROUP BY contact_id HAVING COUNT(*) > 1
        )
    """).fetchone()[0]
    total_contacts_with_orgs = conn.execute("""
        SELECT COUNT(DISTINCT contact_id) FROM contact_organisation_edges
    """).fetchone()[0]
    print(f"\nContacts with an org link: {total_contacts_with_orgs}")
    print(f"  of which linked to >1 organisation: {multi}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
