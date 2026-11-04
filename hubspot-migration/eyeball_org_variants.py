"""Surface likely-duplicate Organisation spellings across:
  - existing raw_companies (kept for migration)
  - new orgs synthesised from contact typed Company Names
  - new orgs synthesised from deal Funder / Introducer / Supplier free-text

Clustering strategy — pragmatic, tuned for a review pass:
  1. Normalise each name (lowercase, strip corp suffixes / stopwords / punctuation)
  2. Extract distinctive tokens (length ≥ 3, not in a stopword set)
  3. Two orgs share a cluster if they share at least one distinctive token
     that appears in ≤ 8 orgs total (filters out generic tokens like "dental",
     "clinic" that would otherwise form mega-clusters)
  4. Union-find over shared-token edges

Writes `org_merge_candidates.csv`: one row per org in a cluster of size ≥ 2.
Client action per row (optional):
  merge_into=<org_key>    collapse into that key
  keep_separate           genuinely different orgs, leave as-is
"""

from __future__ import annotations

import csv
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "hubspot.db"
OUT = Path(__file__).resolve().parent / "org_merge_candidates.csv"

TOKEN_STOPWORDS = {
    "the", "and", "of", "for", "at", "in", "on", "to", "by", "with",
    "ltd", "limited", "plc", "llp", "llc", "inc", "corp", "corporation",
    "company", "group", "holdings", "uk", "international", "services",
    "trust", "co", "association", "associates",
    # domain-y stopwords that would over-cluster dental practices
    "dental", "practice", "clinic", "surgery", "care", "medical",
    "healthcare", "health", "clinics", "practices",
}

MAX_TOKEN_ORG_COUNT = 3  # a token appearing in >3 orgs is too generic to bridge orgs
                          # ("dental", "clinic", "vet" etc. bridge unrelated orgs otherwise)


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r"\b(ltd\.?|limited|plc|llp|llc|inc\.?|corp(oration)?|the|"
               r"t/a|trading as|dba|company|group|holdings|associates?)\b",
               " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokens(normalised: str) -> set[str]:
    return {t for t in normalised.split() if len(t) >= 3 and t not in TOKEN_STOPWORDS}


class UF:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def add(self, x: str) -> None:
        self.p.setdefault(x, x)

    def find(self, x: str) -> str:
        self.add(x)
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

    # Each org entry: dict with key ("<source>::<name>" as canonical id), name,
    # source, source_id (org record id or contact id or raw_value), reach (deals / contacts).
    entries: dict[str, dict] = {}

    def add(key: str, name: str, source: str, source_id: str, reach: int) -> None:
        entries[key] = {
            "key": key, "name": name, "source": source,
            "source_id": source_id, "reach": reach,
        }

    # 1. Existing kept orgs (companies_for_migration)
    for oid, name in conn.execute(
        'SELECT "Record ID", "Company name" FROM companies_for_migration '
        'WHERE "Company name" IS NOT NULL'
    ):
        # reach = deal count + contact edges
        n_deals = conn.execute(
            'SELECT COUNT(*) FROM raw_deals '
            'WHERE (";"||COALESCE("Associated Company IDs","")||";") LIKE ?',
            (f"%;{oid};%",),
        ).fetchone()[0]
        n_edges = conn.execute(
            "SELECT COUNT(*) FROM contact_organisation_edges WHERE organisation_id = ?",
            (oid,),
        ).fetchone()[0]
        add(f"existing::{oid}", name, "existing_org", oid, n_deals + n_edges)

    # 2. New orgs from contact-typed Company Names (automate_contact_orgs.py)
    for cid, name in conn.execute(
        "SELECT contact_id, created_org_name FROM decisions_contact_orgs "
        "WHERE action = 'keep_with_typed_new_org' AND created_org_name IS NOT NULL"
    ):
        add(f"contact::{cid}", name, "created_from_contact", cid, 1)

    # 3. New orgs from deal Funder / Introducer / Supplier free-text
    for dim, raw, n in conn.execute(
        "SELECT dimension, raw_value, deal_count FROM decisions_deal_orgs "
        "WHERE action = 'created_new_org' AND created_new_org_name IS NOT NULL"
    ):
        add(f"deal_{dim}::{raw}", raw.strip(), f"created_from_deal_{dim}", raw, n)

    # Build token → set of keys index
    token_to_keys: dict[str, set[str]] = defaultdict(set)
    key_tokens: dict[str, set[str]] = {}
    for key, e in entries.items():
        tk = tokens(norm(e["name"]))
        key_tokens[key] = tk
        for t in tk:
            token_to_keys[t].add(key)

    # Filter tokens: keep only those appearing in 2..MAX orgs (distinctive but shared)
    distinctive_tokens = {t: keys for t, keys in token_to_keys.items()
                          if 2 <= len(keys) <= MAX_TOKEN_ORG_COUNT}

    uf = UF()
    edge_via: dict[frozenset[str], set[str]] = defaultdict(set)
    for t, keys in distinctive_tokens.items():
        keys_list = list(keys)
        for k in keys_list:
            uf.add(k)
        for i in range(len(keys_list)):
            for j in range(i + 1, len(keys_list)):
                a, b = keys_list[i], keys_list[j]
                shared = key_tokens[a] & key_tokens[b]
                # Merge only when the pair shares:
                #   - a subset relationship (short name is inside long name), OR
                #   - two or more DISTINCTIVE (rare) tokens
                # Since we pre-filtered token_to_keys to rare tokens only,
                # every entry in `shared` is already rare.
                if len(shared) >= 2 or (key_tokens[a] and (
                    key_tokens[a].issubset(key_tokens[b]) or
                    key_tokens[b].issubset(key_tokens[a])
                )):
                    uf.union(a, b)
                    edge_via[frozenset({a, b})] |= shared

    groups = uf.groups()

    # Emit
    rows: list[dict] = []
    cluster_id = 0
    for root, members in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        cluster_id += 1
        # Find shared token summary
        shared_tokens: set[str] = set()
        for i, a in enumerate(members):
            for b in members[i+1:]:
                shared_tokens |= edge_via.get(frozenset({a, b}), set())
        for member in sorted(members, key=lambda k: (
            entries[k]["source"] != "existing_org",  # existing first
            -entries[k]["reach"],
            entries[k]["name"].lower(),
        )):
            e = entries[member]
            rows.append({
                "cluster_id":       cluster_id,
                "cluster_size":     len(members),
                "shared_tokens":    " ".join(sorted(shared_tokens)),
                "source":           e["source"],
                "source_id":        e["source_id"],
                "name":             e["name"],
                "reach":            e["reach"],
                "action":           "",
                "notes":            "",
            })

    fields = list(rows[0].keys()) if rows else []
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    # Report
    print(f"total orgs surveyed:            {len(entries)}")
    print(f"clusters of ≥ 2 candidates:     {cluster_id}")
    print(f"rows in CSV:                    {len(rows)}")
    print(f"→ {OUT.name}")

    # Breakdown of clusters by "does it contain an existing org?"
    with_existing = 0
    only_new = 0
    for root, members in groups.items():
        if any(entries[m]["source"] == "existing_org" for m in members):
            with_existing += 1
        else:
            only_new += 1
    print(f"\ncluster composition:")
    print(f"  contains an existing org (likely 'link new → existing'): {with_existing}")
    print(f"  only new orgs (likely 'collapse duplicates'):            {only_new}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
