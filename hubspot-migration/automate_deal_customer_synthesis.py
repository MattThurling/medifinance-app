"""Synthesise Contacts from deal P1 fields for deals with no linked contact.

Rule: create a Contact only when P1 First name, P1 Last name AND email are
all populated on the deal. Anything less strict pulls in too many junk /
staff-typed emails.

Same-email deals dedupe to a single synthesised Contact (multi-deal customers).

Writes `synthesised_deal_contacts`:
    synth_id, source_deal_ids, first_name, last_name, email,
    phone, mobile, title, date_of_birth, home_address, home_postcode

Downstream migration unions this with the migrated raw_contacts to fill
`Contact` model rows and populate `Deal.customer`.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "hubspot.db"


def norm_email(e: str | None) -> str | None:
    if not e:
        return None
    e = e.strip().lower()
    return e if "@" in e else None


def norm_phone(p: str | None) -> str | None:
    if not p:
        return None
    d = re.sub(r"\D", "", p)
    if d.startswith("44"):
        d = "0" + d[2:]
    return d if len(d) >= 7 else None


def main() -> int:
    conn = sqlite3.connect(DB_PATH)

    rows = list(conn.execute("""
        SELECT "Record ID"          AS deal_id,
               "P1 First name",
               "P1 Last name",
               "P1 Title",
               "P1 Date of Birth",
               "P1 Home Address",
               "P1 Home Postcode",
               "email",
               "Phone",
               "Mobile",
               "Associated Company IDs (Primary)"
        FROM deals_for_migration
        WHERE "Associated Contact IDs" IS NULL
          AND "P1 First name" IS NOT NULL
          AND "P1 Last name"  IS NOT NULL
          AND "email"         IS NOT NULL
    """))

    # Group deals by normalised email — same person across multiple deals =
    # one synthesised Contact.
    groups: dict[str, list[dict]] = defaultdict(list)
    for deal_id, p1_first, p1_last, p1_title, p1_dob, p1_addr, p1_pc, \
        email, phone, mobile, primary_co in rows:
        e = norm_email(email)
        if e is None:
            continue
        groups[e].append({
            "deal_id": deal_id,
            "first_name": (p1_first or "").strip(),
            "last_name":  (p1_last  or "").strip(),
            "title":      (p1_title or "").strip(),
            "dob":        p1_dob,
            "address":    (p1_addr or "").strip(),
            "postcode":   (p1_pc   or "").strip(),
            "email":      email.strip(),
            "phone":      norm_phone(phone),
            "mobile":     norm_phone(mobile),
        })

    with conn:
        conn.execute("DROP TABLE IF EXISTS synthesised_deal_contacts")
        conn.execute("""
            CREATE TABLE synthesised_deal_contacts (
                synth_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                source_deal_ids TEXT NOT NULL,
                first_name      TEXT,
                last_name       TEXT,
                title           TEXT,
                email           TEXT NOT NULL,
                phone           TEXT,
                mobile          TEXT,
                date_of_birth   TEXT,
                home_address    TEXT,
                home_postcode   TEXT
            )
        """)

        insert_rows = []
        for e, dg in groups.items():
            # Take the first non-empty value for each field across the group
            def pick(field):
                for d in dg:
                    v = d.get(field)
                    if v:
                        return v
                return None
            insert_rows.append((
                ";".join(d["deal_id"] for d in dg),
                pick("first_name"), pick("last_name"), pick("title"),
                e,
                pick("phone"), pick("mobile"),
                pick("dob"), pick("address"), pick("postcode"),
            ))
        conn.executemany(
            "INSERT INTO synthesised_deal_contacts "
            "(source_deal_ids, first_name, last_name, title, email, phone, "
            " mobile, date_of_birth, home_address, home_postcode) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            insert_rows,
        )

    n_contacts = conn.execute("SELECT COUNT(*) FROM synthesised_deal_contacts").fetchone()[0]
    n_deals_covered = sum(1 for e in groups.values() for _ in e)
    n_multi = sum(1 for g in groups.values() if len(g) > 1)

    print(f"deals qualifying for synthesis:  {len(rows)}")
    print(f"synthesised Contacts created:    {n_contacts}")
    print(f"contacts serving multiple deals: {n_multi}")
    print(f"deals now with a customer FK:    {n_deals_covered}")
    print(f"deals still with null customer:  1017 - {n_deals_covered} = {1017 - n_deals_covered}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
