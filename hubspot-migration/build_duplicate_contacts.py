"""Detect probable duplicate contacts and produce a decision CSV.

Signals used (union-find over these edges builds clusters):
  - Shared email (normalised: strip + lowercase). Role-mailbox local-parts
    like info@ / admin@ / proposals@ are EXCLUDED — those are shared inboxes,
    not duplicate identities.
  - Shared phone or mobile (normalised to UK canonical: strip non-digits,
    convert +44... → 0..., require ≥7 digits).

Rows output only for clusters of size ≥ 2. Within each cluster, the contact
with the most deals is flagged `is_suggested_primary=1` — client can override.

Applies both decisions_role_mailbox (drops org_contact_method / drop) and
decisions_name_normalisation (uses proposed_first_name / proposed_last_name
where action='apply') before deduping.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "hubspot.db"
OUT = Path(__file__).resolve().parent / "duplicate_contacts.csv"

ROLE_LOCALPARTS = {
    "info", "enquiries", "enquiry", "hello", "contact", "contactus",
    "admin", "office", "mail", "team", "reception",
    "accounts", "accounting", "finance", "invoicing", "billing",
    "sales", "business",
    "proposals", "quotes", "applications", "apply", "application",
    "support", "helpdesk", "customerservice",
    "hr", "careers", "jobs",
    "marketing", "media",
    "noreply", "no-reply", "donotreply",
    "post", "postmaster",
}
ROLE_TOKEN_RE = re.compile(
    r"^(info|enquir|proposals?|accounts?|billing|invoicing|admin|office|"
    r"reception|hello|contact|sales|support|noreply|no-reply)"
    r"(-|\.|_|@|$)",
    re.IGNORECASE,
)


def norm_email(e: str | None) -> str | None:
    if not e:
        return None
    e = e.strip().lower()
    if "@" not in e:
        return None
    local = e.split("@", 1)[0]
    if local in ROLE_LOCALPARTS or ROLE_TOKEN_RE.match(local):
        return None
    return e


def norm_phone(p: str | None) -> str | None:
    if not p:
        return None
    digits = re.sub(r"\D", "", p)
    if digits.startswith("44"):
        digits = "0" + digits[2:]
    return digits if len(digits) >= 7 else None


class UF:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def _add(self, x: str) -> None:
        self.p.setdefault(x, x)

    def find(self, x: str) -> str:
        self._add(x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, x: str, y: str) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.p[rx] = ry

    def groups(self) -> dict[str, list[str]]:
        g: dict[str, list[str]] = defaultdict(list)
        for x in self.p:
            g[self.find(x)].append(x)
        return {k: v for k, v in g.items() if len(v) >= 2}


def main() -> int:
    conn = sqlite3.connect(DB_PATH)

    role_action = dict(conn.execute("SELECT contact_id, action FROM decisions_role_mailbox"))
    name_decision = {
        r[0]: r for r in conn.execute(
            "SELECT contact_id, action, proposed_first_name, proposed_last_name "
            "FROM decisions_name_normalisation"
        )
    }

    raw = list(conn.execute("""
        SELECT c."Record ID", c."First Name", c."Last Name",
               c."Email", c."Phone Number", c."Mobile Phone Number"
        FROM contacts_for_migration c
    """))

    contacts: dict[str, dict] = {}
    for cid, first, last, email, phone, mobile in raw:
        if role_action.get(cid) in ("org_contact_method", "drop"):
            continue
        if cid in name_decision:
            _, act, pf, pl = name_decision[cid]
            if act == "apply":
                first = pf or first
                last  = pl or last
        contacts[cid] = {
            "first_name": (first or "").strip(),
            "last_name":  (last  or "").strip(),
            "email":      (email or "").strip(),
            "phone":      (phone or "").strip(),
            "mobile":     (mobile or "").strip(),
        }

    # Index by normalised keys.
    by_email: dict[str, list[str]] = defaultdict(list)
    by_phone: dict[str, list[str]] = defaultdict(list)
    for cid, c in contacts.items():
        e = norm_email(c["email"])
        if e:
            by_email[e].append(cid)
        for src in (c["phone"], c["mobile"]):
            p = norm_phone(src)
            if p:
                by_phone[p].append(cid)

    # Track keys that appear on ≥ OVERSHARED_THRESHOLD contacts — for reporting.
    # (Doesn't affect merging; sub-clustering by name handles false positives.)
    OVERSHARED_THRESHOLD = 5
    overshared_emails = {k: len(v) for k, v in by_email.items() if len(v) >= OVERSHARED_THRESHOLD}
    overshared_phones = {k: len(v) for k, v in by_phone.items() if len(v) >= OVERSHARED_THRESHOLD}

    def name_key(cid: str) -> str:
        """Bucket for sub-clustering: prefer last name, fall back to first,
        else '*noname*'. Case-insensitive."""
        c = contacts[cid]
        return (
            c["last_name"].strip().lower()
            or c["first_name"].strip().lower()
            or "*noname*"
        )

    def sub_clusters(members: list[str]) -> list[list[str]]:
        """Split members by name_key; return only sub-groups of size ≥ 2."""
        buckets: dict[str, list[str]] = defaultdict(list)
        for m in members:
            buckets[name_key(m)].append(m)
        return [g for g in buckets.values() if len(g) >= 2]

    uf = UF()
    signal_for: dict[str, set[str]] = defaultdict(set)
    for e, cids in by_email.items():
        if len(cids) < 2:
            continue
        for sub in sub_clusters(cids):
            for c in sub:
                uf._add(c)
                signal_for[c].add(f"email={e}")
            for a in sub[1:]:
                uf.union(sub[0], a)
    for p, cids in by_phone.items():
        if len(cids) < 2:
            continue
        for sub in sub_clusters(cids):
            for c in sub:
                uf._add(c)
                signal_for[c].add(f"phone={p}")
            for a in sub[1:]:
                uf.union(sub[0], a)

    groups = uf.groups()

    # deal_count is used both to sort cluster members and to seed suggested primary
    def deal_count(cid: str) -> int:
        return conn.execute(
            'SELECT COUNT(DISTINCT d."Record ID") FROM raw_deals d '
            'WHERE (\';\'||COALESCE(d."Associated Contact IDs", \'\')||\';\') LIKE ?',
            (f"%;{cid};%",),
        ).fetchone()[0]

    def orgs_for(cid: str) -> str:
        rows = conn.execute("""
            SELECT DISTINCT o."Company name"
            FROM contact_organisation_edges e
            LEFT JOIN raw_companies o ON o."Record ID" = e.organisation_id
            WHERE e.contact_id = ?
        """, (cid,)).fetchall()
        return "; ".join(r[0] for r in rows if r[0])

    # Emit rows
    rows_out: list[dict] = []
    cluster_id = 0
    for root, members in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        cluster_id += 1
        counts = {m: deal_count(m) for m in members}
        # Suggested primary: most deals; ties broken by max populated fields; then contact_id
        def score(cid):
            c = contacts[cid]
            populated = sum(1 for k in ("first_name","last_name","email","phone","mobile") if c[k])
            return (-counts[cid], -populated, cid)
        ordered = sorted(members, key=score)
        primary = ordered[0]
        signals = sorted({s for m in members for s in signal_for.get(m, set())})
        sig_summary = "; ".join(signals)

        for cid in ordered:
            c = contacts[cid]
            rows_out.append({
                "cluster_id":            cluster_id,
                "cluster_size":          len(members),
                "match_signals":         sig_summary,
                "contact_id":            cid,
                "first_name":            c["first_name"],
                "last_name":             c["last_name"],
                "email":                 c["email"],
                "phone":                 c["phone"],
                "mobile":                c["mobile"],
                "associated_orgs":       orgs_for(cid),
                "deal_count":            counts[cid],
                "is_suggested_primary":  1 if cid == primary else 0,
                "action":                "",
                "notes":                 "",
            })

    fields = list(rows_out[0].keys()) if rows_out else []
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows_out)

    # Summary
    print(f"kept contacts scanned:  {len(contacts)}")
    print(f"clusters found:         {cluster_id}")
    print(f"contact rows in CSV:    {len(rows_out)}")
    print(f"→ {OUT.name}")

    if overshared_emails or overshared_phones:
        print(f"\nover-shared keys detected (≥ {OVERSHARED_THRESHOLD} contacts each"
              f" — merged only where names also match, so placeholder-phone chains"
              f" don't produce false duplicates):")
        for k, n in sorted(overshared_emails.items(), key=lambda x: -x[1]):
            print(f"  email {k!r}: {n} contacts")
        for k, n in sorted(overshared_phones.items(), key=lambda x: -x[1]):
            print(f"  phone {k!r}: {n} contacts")

    sizes: dict[int, int] = defaultdict(int)
    for root, members in groups.items():
        sizes[len(members)] += 1
    print("\ncluster size distribution:")
    for size in sorted(sizes):
        print(f"  size {size}: {sizes[size]} clusters")

    email_only = sum(1 for m in groups.values()
                     if all("email=" in s and "phone=" not in s
                            for cid in m for s in signal_for.get(cid, {"email="})))
    phone_only = sum(1 for m in groups.values()
                     if all("phone=" in s and "email=" not in s
                            for cid in m for s in signal_for.get(cid, {"phone="})))
    both       = cluster_id - email_only - phone_only
    print(f"\nby signal:")
    print(f"  email-only:  {email_only}")
    print(f"  phone-only:  {phone_only}")
    print(f"  both:        {both}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
