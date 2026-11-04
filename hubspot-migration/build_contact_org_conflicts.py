"""Contact ↔ Organisation conflict resolution CSV.

Ignores the junk `Associated Company` field (100% populated with the literal
string "Associated Company"). The real HubSpot linkage is
`Associated Company IDs` / `Associated Company IDs (Primary)`.

Two categories are surfaced:
  disagreement       Company Name typed AND linked org exists, but the typed
                     name doesn't normalise to any linked-org name.
  only_company_name  Company Name typed AND no HubSpot linkage. May match an
                     existing Organisation record via fuzzy match (auto-link),
                     or need creation.

Already-agreeing rows and "linked but no typed name" cases are auto-resolved
by the migration; not shown to the client.

Applies decisions from role_mailbox, name_normalisation, and duplicates
before building the CSV — so the client sees the current best-known state.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "hubspot.db"
OUT = Path(__file__).resolve().parent / "contact_org_conflicts.csv"


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r"\b(ltd\.?|limited|plc|llp|llc|inc\.?|corp(oration)?|the|"
               r"t/a|trading as|dba)\b", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def parse_ids(cell: str | None) -> list[str]:
    if not cell:
        return []
    return [x.strip() for x in cell.split(";") if x.strip()]


def main() -> int:
    conn = sqlite3.connect(DB_PATH)

    # Decisions from earlier phases
    role_action = dict(conn.execute("SELECT contact_id, action FROM decisions_role_mailbox"))
    dup_action  = dict(conn.execute("SELECT contact_id, action FROM decisions_duplicate_contacts"))

    # Excluded contacts: role_mailbox {org_contact_method, drop} + dup {merge_into_primary, drop}
    def excluded(cid: str) -> bool:
        if role_action.get(cid) in ("org_contact_method", "drop"):
            return True
        if dup_action.get(cid) in ("merge_into_primary", "drop"):
            return True
        return False

    # Organisations: id → name, and normalised name index for fuzzy match
    orgs = {oid: name for oid, name in conn.execute(
        'SELECT "Record ID", "Company name" FROM raw_companies WHERE "Company name" IS NOT NULL'
    )}
    org_norm_index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for oid, name in orgs.items():
        org_norm_index[norm(name)].append((oid, name))

    def find_org_matches(typed_name: str, limit: int = 3) -> list[tuple[str, str, float]]:
        """Return up to `limit` best (org_id, org_name, score) matches for typed_name."""
        t = norm(typed_name)
        if not t:
            return []
        # Exact normalised match first
        if t in org_norm_index:
            return [(oid, name, 1.0) for oid, name in org_norm_index[t]][:limit]
        # Substring match — typed within org name or vice versa (fast pass)
        exact_sub = []
        for key, entries in org_norm_index.items():
            if key and (t in key or key in t):
                for oid, name in entries:
                    exact_sub.append((oid, name, 0.9))
        if len(exact_sub) >= limit:
            return exact_sub[:limit]
        # Fall back to sequence similarity, but only on candidates sharing a token
        tokens = set(t.split())
        scored: list[tuple[str, str, float]] = list(exact_sub)
        seen_ids = {oid for oid, _, _ in scored}
        for key, entries in org_norm_index.items():
            if not key or not (tokens & set(key.split())):
                continue
            s = similarity(t, key)
            if s < 0.7:
                continue
            for oid, name in entries:
                if oid in seen_ids:
                    continue
                scored.append((oid, name, s))
                seen_ids.add(oid)
        scored.sort(key=lambda r: -r[2])
        return scored[:limit]

    # Pull each contact
    rows_out: list[dict] = []
    scanned = 0
    agreeing = 0
    only_linked = 0

    contacts = list(conn.execute("""
        SELECT c."Record ID", c."First Name", c."Last Name", c."Email",
               c."Company Name",
               c."Associated Company IDs",
               c."Associated Company IDs (Primary)"
        FROM contacts_for_migration c
    """))

    # Apply name-normalisation decisions for display
    name_decision = {
        r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT contact_id, action, proposed_first_name, proposed_last_name "
            "FROM decisions_name_normalisation"
        )
    }

    def deal_count(cid: str) -> int:
        return conn.execute(
            'SELECT COUNT(DISTINCT d."Record ID") FROM raw_deals d '
            'WHERE (\';\'||COALESCE(d."Associated Contact IDs",\'\')||\';\') LIKE ?',
            (f"%;{cid};%",),
        ).fetchone()[0]

    def deal_company_names(cid: str) -> str:
        rows = conn.execute("""
            SELECT DISTINCT o."Company name"
            FROM raw_deals d
            LEFT JOIN raw_companies o
                   ON (';'||COALESCE(d."Associated Company IDs",'')||';') LIKE ('%;'||o."Record ID"||';%')
            WHERE (';'||COALESCE(d."Associated Contact IDs",'')||';') LIKE ?
              AND o."Company name" IS NOT NULL
        """, (f"%;{cid};%",)).fetchall()
        return "; ".join(r[0] for r in rows if r[0])

    for cid, first, last, email, company_name, assoc_ids, primary_ids in contacts:
        scanned += 1
        if excluded(cid):
            continue

        # Apply name-normalisation for display
        if cid in name_decision:
            act, pf, pl = name_decision[cid]
            if act == "apply":
                first = pf or first
                last  = pl or last

        linked_ids = parse_ids(assoc_ids) or parse_ids(primary_ids)
        linked_names = [orgs.get(oid, f"(unknown org {oid})") for oid in linked_ids]
        linked_norm = {norm(n) for n in linked_names}

        typed = (company_name or "").strip()

        if not typed and linked_ids:
            only_linked += 1
            continue
        if not typed and not linked_ids:
            continue  # nothing to decide

        # Both typed and linked → check agreement
        if linked_ids:
            if norm(typed) in linked_norm:
                agreeing += 1
                continue
            conflict_type = "disagreement"
        else:
            conflict_type = "only_company_name"

        # Fuzzy suggestions from Organisation records
        matches = find_org_matches(typed, limit=3)
        top_match_name = matches[0][1] if matches else ""
        top_match_score = f"{matches[0][2]:.2f}" if matches else ""
        other_matches = "; ".join(f"{n} ({s:.2f})" for _, n, s in matches[1:]) if len(matches) > 1 else ""

        rows_out.append({
            "contact_id":            cid,
            "first_name":            first or "",
            "last_name":             last or "",
            "email":                 email or "",
            "company_name_typed":    typed,
            "current_linked_orgs":   "; ".join(linked_names),
            "top_org_match":         top_match_name,
            "top_org_match_score":   top_match_score,
            "other_org_matches":     other_matches,
            "deal_companies":        deal_company_names(cid),
            "deal_count":            deal_count(cid),
            "conflict_type":         conflict_type,
            "action":                "",
            "notes":                 "",
        })

    fields = list(rows_out[0].keys()) if rows_out else []
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        rows_out.sort(key=lambda r: (r["conflict_type"], -int(r["deal_count"])))
        w.writerows(rows_out)

    print(f"contacts scanned:                  {scanned}")
    print(f"auto-resolved (agreeing):          {agreeing}")
    print(f"auto-resolved (only linked):       {only_linked}")
    print(f"rows in CSV (need client input):   {len(rows_out)}")
    counts_by_type: dict[str, int] = defaultdict(int)
    for r in rows_out:
        counts_by_type[r["conflict_type"]] += 1
    for t, n in counts_by_type.items():
        print(f"  {t}: {n}")
    print(f"→ {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
