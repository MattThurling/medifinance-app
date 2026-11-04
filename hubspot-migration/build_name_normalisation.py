"""Scan contacts_for_migration for name-field problems and produce a decision
CSV the client can review in one pass.

Detected issues (all case-insensitive):
  title_prefix           first_name starts with Dr / Prof / Mr / Mrs / Ms / etc.
  full_name_in_first     last_name empty AND first_name has a space
  middle_name            first_name has 2+ words AND last_name populated
  empty_first_populated_last  first_name blank, last_name populated
  all_caps               first or last is UPPERCASE (>=3 chars)
  all_lower              first or last is lowercase (>=3 chars)
  alt_differs            "First name (2)" or "Last Name (2)" differs

For each flagged contact the CSV includes a proposed cleaner split. Titles
have no home in the current schema — recorded in `stripped_title` so nothing
is lost, and we can revisit adding `Contact.title` if it's common.

Action values:
  apply   — use proposed_first_name / proposed_last_name during migration
  skip    — no change; migrate current_first_name / current_last_name as-is
"""

from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "hubspot.db"
OUT = Path(__file__).resolve().parent / "name_normalisation.csv"

# British-style honorifics recognised at the start of first_name.
TITLE_TOKENS = [
    r"Dr", r"Doctor", r"Prof", r"Professor", r"Mr", r"Mrs", r"Ms", r"Miss",
    r"Sir", r"Dame", r"Lord", r"Lady", r"Rev", r"Reverend", r"Hon",
    r"Fr", r"Br", r"Sr",
]
TITLE_RE = re.compile(
    r"^(?P<title>(?:" + "|".join(TITLE_TOKENS) + r"))\.?\s+(?P<rest>.+)$",
    re.IGNORECASE,
)


def norm(s: str | None) -> str:
    return (s or "").strip()


def is_all_caps(s: str) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", s)
    return len(letters) >= 3 and letters.upper() == letters and letters.lower() != letters


def is_all_lower(s: str) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", s)
    return len(letters) >= 3 and letters.lower() == letters and letters.upper() != letters


def smart_titlecase(s: str) -> str:
    """Title-case a name string, preserving intentional mixed-case tokens
    (like McDonald, O'Brien, BlackSwan)."""
    if not s:
        return s
    words = re.split(r"(\s+|-)", s)  # keep separators
    out = []
    for w in words:
        if not w or w.isspace() or w == "-":
            out.append(w)
            continue
        # Preserve tokens that already have internal capitals (Mc, O', BlackSwan)
        internal_caps = sum(1 for i, ch in enumerate(w[1:], 1) if ch.isupper())
        if internal_caps > 0 and not w.isupper():
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return "".join(out)


def analyse(first: str, last: str, alt_first: str, alt_last: str) -> dict:
    issues: list[str] = []
    proposed_first = first
    proposed_last  = last
    stripped_title = ""
    stripped_middle = ""

    # 1. Title prefix in first_name
    m = TITLE_RE.match(first)
    if m:
        issues.append("title_prefix")
        stripped_title = m.group("title").rstrip(".")
        proposed_first = m.group("rest").strip()

    # After stripping title, re-examine the remaining first_name
    # for the "full name in one field" and "middle name" cases.

    if proposed_first and not last:
        parts = proposed_first.split()
        if len(parts) >= 2:
            issues.append("full_name_in_first")
            proposed_first = parts[0]
            proposed_last  = " ".join(parts[1:])
    elif proposed_first and last:
        parts = proposed_first.split()
        if len(parts) >= 2:
            issues.append("middle_name")
            # Preserve first name as the first token; the rest is middle name
            stripped_middle = " ".join(parts[1:])
            proposed_first = parts[0]

    if not first and last:
        # first empty, last populated — sometimes full name in last_name
        parts = last.split()
        if len(parts) >= 2:
            issues.append("empty_first_populated_last")
            proposed_first = parts[0]
            proposed_last  = " ".join(parts[1:])

    # 2. Casing
    if is_all_caps(proposed_first) or is_all_caps(proposed_last):
        issues.append("all_caps")
        proposed_first = smart_titlecase(proposed_first)
        proposed_last  = smart_titlecase(proposed_last)
    elif is_all_lower(proposed_first) or is_all_lower(proposed_last):
        issues.append("all_lower")
        proposed_first = smart_titlecase(proposed_first)
        proposed_last  = smart_titlecase(proposed_last)

    # 3. (2) column disagreements
    if alt_first and alt_first != first:
        issues.append("alt_first_differs")
    if alt_last and alt_last != last:
        issues.append("alt_last_differs")

    return {
        "issues": issues,
        "proposed_first_name": proposed_first,
        "proposed_last_name":  proposed_last,
        "stripped_title":      stripped_title,
        "stripped_middle":     stripped_middle,
    }


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    rows = list(conn.execute("""
        SELECT c."Record ID",
               c."First Name",
               c."Last Name",
               c."Email",
               c."First name (2)",
               c."Last Name (2)"
        FROM contacts_for_migration c
    """))

    scanned = 0
    flagged = []
    for cid, first, last, email, alt_first, alt_last in rows:
        first, last = norm(first), norm(last)
        alt_first, alt_last = norm(alt_first), norm(alt_last)
        email = norm(email)
        scanned += 1
        result = analyse(first, last, alt_first, alt_last)
        if not result["issues"]:
            continue
        flagged.append({
            "contact_id":            cid,
            "current_first_name":    first,
            "current_last_name":     last,
            "current_email":         email,
            "alt_first_name":        alt_first if alt_first != first else "",
            "alt_last_name":         alt_last  if alt_last  != last  else "",
            "detected_issues":       ", ".join(result["issues"]),
            "proposed_first_name":   result["proposed_first_name"],
            "proposed_last_name":    result["proposed_last_name"],
            "stripped_title":        result["stripped_title"],
            "stripped_middle":       result["stripped_middle"],
            "action":                "",
            "notes":                 "",
        })

    fields = [
        "contact_id","current_first_name","current_last_name","current_email",
        "alt_first_name","alt_last_name","detected_issues",
        "proposed_first_name","proposed_last_name",
        "stripped_title","stripped_middle","action","notes",
    ]
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(flagged)

    # Report
    print(f"scanned:  {scanned}")
    print(f"flagged:  {len(flagged)}  ({100*len(flagged)/scanned:.1f}% of contacts)")
    print(f"→ {OUT.name}")

    from collections import Counter
    issue_counts: Counter[str] = Counter()
    for r in flagged:
        for iss in r["detected_issues"].split(", "):
            if iss:
                issue_counts[iss] += 1
    print("\nissue distribution:")
    for iss, n in issue_counts.most_common():
        print(f"  {n:>4}  {iss}")

    n_title = sum(1 for r in flagged if r["stripped_title"])
    if n_title:
        print(f"\n{n_title} contacts have a title that would be discarded on migration —")
        print(f"if these matter, we should add a Contact.title field to the schema.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
