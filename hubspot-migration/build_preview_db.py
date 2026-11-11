"""Build preview.sqlite3 — a copy of the Django project's db-old.sqlite3 with
CRM tables cleared and repopulated from the migration staging data.

Loads from all the decision + staging tables produced so far:
  - companies_for_migration (kept orgs)
  - decisions_contact_orgs (contact-org resolution)
  - decisions_deal_orgs (funder/introducer/supplier resolution)
  - decisions_role_mailbox, decisions_duplicate_contacts,
    decisions_name_normalisation (contact cleanup)
  - synthesised_deal_contacts
  - deals_for_migration
  - contact_organisation_edges (for the M2M)

Notes with type='hubspot_migration_comment' are attached to each derived row
so provenance is visible in the Django admin / app UI.

Usage:
  python build_preview_db.py
  cp preview.sqlite3 ../db.sqlite3          # (../medifinance-app/db.sqlite3 in the old layout)
  # Then start the Django server: python manage.py runserver
  # The template must already carry every crm migration the INSERTs below
  # need — bring it forward first with:
  #   DATABASE_URL=sqlite:///$PWD/db-old.sqlite3 python manage.py migrate

The template db-old.sqlite3 is not modified.
"""

from __future__ import annotations

import csv
import re
import shutil
import sqlite3
import sys
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DB = HERE / "hubspot.db"
# Schema template: the app's db-old.sqlite3. This folder normally lives inside
# the app repo (hubspot-migration/); fall back to the old sibling-folder layout.
TEMPLATE_DB = (
    HERE.parent / "db-old.sqlite3"
    if (HERE.parent / "manage.py").exists()
    else HERE.parent / "medifinance-app" / "db-old.sqlite3"
)
OUT_DB = HERE / "preview.sqlite3"

STAMP = "2026-08-03 12:00:00.000000"

# Tables we clear before populating. Order matters — respect FK dependencies.
CLEAR_TABLES = [
    "crm_note",
    "crm_stage",
    "crm_proposal",
    "crm_participation",
    "crm_deal_co_applicants",
    "crm_deal",
    "crm_contact_organisations",
    "crm_contact",
    "crm_organisation",
    "accounts_user_groups",
    "accounts_user_user_permissions",
    "accounts_user",
]


# HubSpot Pipeline → Deal.Type code (crm/models.py). Patient Finance and
# Personal Loans never appear here — those pipelines are out of scope.
PIPELINE_TYPE = {
    "Asset Finance": "asset_finance",
    "MF Commercial Finance": "commercial_finance",
    "Medifinance Ltd Business Loans": "business_loans",
    "Structured Finance": "structured_finance",
    "KMC Associates": "kmc_associates",
    "Short term Finance": "short_term_finance",
}


# HubSpot custom "Source" pick-list label → Deal.Source code (crm/models.py).
# 1:1 mapping of every value seen on in-scope deals ("TriPay" only occurs on
# out-of-scope pipelines). Unknown labels warn and fall through to NULL.
SOURCE_CODES = {
    "Introducer": "introducer",
    "Existing Customer": "existing_customer",
    "Supplier": "supplier",
    "Referral": "referral",
    "Exhibition": "exhibition",
    "Meeting": "meeting",
    "Website": "website",
    "Search Engine": "search_engine",
    "Campaign/Advert": "campaign_advert",
    "e-shot": "e_shot",
    "Twitter": "twitter",
    "Other": "other",
}

# MediFinance's own company record in HubSpot. 177 deals carry "Medifinance"
# as their Introducer, which auto-matches to this org — that means "no
# external introducer", so it must not drive the Source inference below.
MEDIFINANCE_ORG_HS_ID = "3212912855"


# HubSpot Sector pick-list label → Organisation.Sector code (crm/models.py).
# Used verbatim on raw_deals."Sector" and raw_contacts."Sector".
SECTOR_CODES = {
    "Dental": "dental",
    "Dental Lab": "dental_lab",
    "Doctors": "doctors",
    "Pharmacy": "pharmacy",
    "Veterinary": "veterinary",
    "Physio/Chiro": "physio_chiro",
    "Cosmetic": "cosmetic",
    "Optical": "optical",
    "Hearing": "hearing",
    "Hair Clinic": "hair_clinic",
    "Education": "education",
    "Non-healthcare profession": "non_healthcare",
    "Other": "other",
}

# Conservative raw_companies."Industry" → sector mapping. Industry is HubSpot
# auto-enrichment; only values validated against deal evidence are trusted
# (Dental: 191/193 orgs agree, Veterinary: 4/4). Everything else — incl.
# "Hospital & Health Care", "Pharmaceuticals", "Medical Practice" — was
# contradicted or unsupported and stays unmapped. Per-org provenance lands in
# sector_derivation_audit.csv.
INDUSTRY_TO_SECTOR = {"Dental": "dental", "Veterinary": "veterinary"}


# ─── ID maps: raw HubSpot IDs → Django integer PKs ────────────────────────
org_id_map:  dict[str, int] = {}
contact_id_map: dict[str, int] = {}
deal_id_map:  dict[str, int] = {}
synth_contact_map: dict[str, int] = {}
new_org_by_source: dict[tuple[str, str], int] = {}
user_id_map: dict[str, int] = {}


def norm_dt(s: str | None) -> str:
    if not s:
        return STAMP
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s + " 00:00:00.000000"
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", s):
        return s + ":00.000000"
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", s):
        return s + ".000000"
    return s


_MONTHS = {m[:3]: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}


def norm_date(s: str | None) -> str | None:
    """Normalise the export's DOB formats to ISO. UK data, so day-first:
    23/04/1965, 23/04/65, 23041965, 9th Dec 1943, 23.June 1955, 99 99 99.
    Returns None when unparseable — callers count failures but never print
    the value (sensitive)."""
    if not s:
        return None
    s = s.strip()
    m = re.match(r"^\d{4}-\d{2}-\d{2}", s)
    if m:
        return m.group(0)
    m = (re.fullmatch(r"(\d{1,2})[/. ](\d{1,2})[/. ](\d{2}|\d{4})", s)
         or re.fullmatch(r"(\d{2})(\d{2})(\d{4})", s))
    if m:
        d, mo, y = (int(g) for g in m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?[,. ]+([A-Za-z]+)[,. ]+(\d{4})", s)
        if not m or m[2].lower()[:3] not in _MONTHS:
            return None
        d, mo, y = int(m[1]), _MONTHS[m[2].lower()[:3]], int(m[3])
    if y < 100:
        # two-digit DOB years: ≤ current year (26) → 2000s, else 1900s
        y += 2000 if y <= 26 else 1900
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def split_address(single: str | None) -> tuple[str, str]:
    if not single:
        return "", ""
    parts = [p.strip() for p in re.split(r"[\r\n]|,\s*", single) if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0][:255], ""
    return parts[0][:255], (", ".join(parts[1:]))[:255]


def as_decimal(s: str | None) -> str | None:
    if s is None or s == "":
        return None
    try:
        return f"{float(s):.2f}"
    except (TypeError, ValueError):
        return None


# ─── Stage derivation (Phase 2J) ──────────────────────────────────────────
#
# Precedence rules, first match wins:
#   1. dead label                        -> lost
#   2. paid-off label                    -> settled
#   3. live label, close + term elapsed  -> settled, else deal_live
#   4. any other label but an MF invoice number exists (label is stale;
#      the invoice is better evidence)   -> settled if term elapsed, else mf_invoiced
#   5. active label + activity in the last 12 months -> natural mapping
#   6. everything else                   -> dormant
#
# Each deal gets a reconstructed chain of Stage events (application first —
# or initial_contact, for deals still at HubSpot "Initial Contact" — and the
# final stage last) rather than a single flattened stage. The final event's
# note records the original HubSpot pipeline/stage so nothing is misrepresented.

TODAY = date.today()
ACTIVITY_CUTOFF = TODAY.replace(year=TODAY.year - 1)

DEAD_LABELS = {"lost", "dead", "ntu dead", "other provider"}
PAID_OFF_LABELS = {"paid off"}
LIVE_LABELS = {"deal live", "live", "closed won"}
ACTIVE_LABEL_MAP = {
    "create_new": "application",
    "application received/privacy ok": "application",
    "gdpr required": "application",
    "initial contact": "initial_contact",
    "assigned to owner": "application",
    "email 1 sent": "application",
    "email 2 sent": "application",
    "email 3 sent": "application",
    "more info": "info_received",
    "active negotiations": "info_received",
    "early discussion": "info_received",
    "ready to prop": "proposal_submitted",
    "propped": "proposal_submitted",
    "indicative terms": "proposal_submitted",
    "quoted": "proposal_submitted",
    "engagement letter": "proposal_submitted",
    "accepted": "proposal_approved",
    "accepted / next step?": "proposal_approved",
    "sanctioned": "proposal_approved",
    "docs out": "documents_out",
    "awaiting invoice": "supplier_invoice_requested",
    "awaiting payout": "supplier_invoice_received",
}
MIGRATION_NOTE = "Reconstructed during HubSpot migration."

stage_audit: Counter = Counter()  # (pipeline, hubspot_stage, final_stage) -> n
term_end_counts: Counter = Counter()  # crm_deal.term_end_date source -> n
participation_counts: Counter = Counter()  # with-supplier / orphan / none -> n
source_counts: Counter = Counter()  # crm_deal.source provenance -> n
source_audit: list[tuple[str, str, str]] = []  # (hubspot_id, deal_name, introducer_text) inferred
_unknown_sources: set[str] = set()


def parse_term_months(s: str | None) -> int | None:
    m = re.match(r"(\d+)\s*month", (s or "").strip())
    return int(m.group(1)) if m else None


def as_date(s: str | None) -> date | None:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s or "")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def add_months(d: date, months: int) -> date:
    y, m = d.year + (d.month - 1 + months) // 12, (d.month - 1 + months) % 12 + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def derive_stage_events(r: dict) -> list[tuple[str, str, str]]:
    """Return the reconstructed (name, occurred_at, note) Stage chain for a deal."""
    label = (r.get("Deal Stage") or "").strip()
    key = label.lower()
    pipeline = (r.get("Pipeline") or "").strip()
    invoice_no = (r.get("Invoice No") or "").strip()
    created = r.get("Create Date")
    closed = r.get("Close Date") or r.get("Last Modified Date")
    last_activity = r.get("Last Activity Date") or r.get("Last Modified Date")

    close_d = as_date(closed)
    term_months = parse_term_months(r.get("Term"))
    settle_d = add_months(close_d, term_months) if close_d and term_months else None
    term_elapsed = settle_d is not None and settle_d <= TODAY

    provenance = f"HubSpot: {pipeline} / {label}."
    lost_reason = (r.get("Closed Lost Reason") or "").strip()
    if lost_reason:
        provenance += f" Closed lost reason: {lost_reason}."

    # Deals still at HubSpot "Initial Contact" never reached application, so
    # their chain starts (and, if active, ends) at initial_contact instead.
    base_stage = ("initial_contact"
                  if ACTIVE_LABEL_MAP.get(key) == "initial_contact" else "application")
    events: list[tuple[str, str | None]] = [(base_stage, created)]
    if key in DEAD_LABELS:
        if invoice_no:
            events.append(("mf_invoiced", closed))
        events.append(("lost", closed))
    elif key in PAID_OFF_LABELS:
        # Paid off = settled early — date it when the deal was marked paid off,
        # not the projected close + term (which may be in the future).
        if invoice_no:
            events.append(("mf_invoiced", closed))
        events.append(("deal_live", closed))
        events.append(("settled", last_activity))
    elif key in LIVE_LABELS:
        if invoice_no:
            events.append(("mf_invoiced", closed))
        events.append(("deal_live", closed))
        if term_elapsed:
            events.append(("settled", settle_d.isoformat()))
    elif invoice_no:
        events.append(("mf_invoiced", closed))
        if term_elapsed:
            events.append(("settled", settle_d.isoformat()))
    elif key in ACTIVE_LABEL_MAP and (as_date(last_activity) or date.min) >= ACTIVITY_CUTOFF:
        mapped = ACTIVE_LABEL_MAP[key]
        if mapped != base_stage:
            events.append((mapped, last_activity))
    else:
        events.append(("dormant", last_activity))

    # occurred_at must never go backwards, or the intended final stage would
    # not be the deal's "current" stage (ordering is -occurred_at, -pk). Also
    # clamp to today — HubSpot Close Date can be a future forecast.
    cap = TODAY.isoformat() + " 12:00:00.000000"
    chain: list[list[str]] = []
    floor = ""
    for name, when in events:
        dt = max(min(norm_dt(when), cap), floor)
        floor = dt
        chain.append([name, dt, MIGRATION_NOTE])
    chain[-1][2] = f"{MIGRATION_NOTE} {provenance}"

    stage_audit[(pipeline, label, chain[-1][0])] += 1
    return [tuple(e) for e in chain]


def clear_target(out: sqlite3.Connection) -> None:
    out.execute("PRAGMA foreign_keys = OFF")
    for t in CLEAR_TABLES:
        out.execute(f'DELETE FROM "{t}"')
    # Reset autoincrement counters
    seq_names = "','".join(CLEAR_TABLES)
    out.execute(f"DELETE FROM sqlite_sequence WHERE name IN ('{seq_names}')")


# Phase 2I decisions — HubSpot Deal owner name → real user details.
USER_MAPPING_CSV = HERE / "user_mapping_decisions.csv"


def load_users(src: sqlite3.Connection, out: sqlite3.Connection) -> None:
    with USER_MAPPING_CSV.open(newline="", encoding="utf-8-sig") as f:
        mapping = {row["hubspot_owner_name"]: row for row in csv.DictReader(f)}
    # Two CSV rows may share an email (alias: same person under two HubSpot
    # names) — accounts_user.email is unique, so dedupe to a single account.
    email_to_pk: dict[str, int] = {}
    for row in src.execute('''
        SELECT name, SUM(n) AS n FROM (
            SELECT "Deal owner" AS name, COUNT(*) AS n FROM raw_deals GROUP BY 1
            UNION ALL SELECT "Contact owner", COUNT(*) FROM raw_contacts GROUP BY 1
            UNION ALL SELECT "Company owner", COUNT(*) FROM raw_companies GROUP BY 1
        ) WHERE name IS NOT NULL AND name <> ''
        GROUP BY name ORDER BY n DESC'''):
        name, n = row[0], row[1]
        dec = mapping.get(name)
        if dec:
            first, last, email = dec["first_name"], dec["last_name"], dec["email"]
            role = dec["role"]
            active = 1 if dec["is_active"].strip().upper() == "TRUE" else 0
        else:
            print(f"  ⚠ Owner name not in {USER_MAPPING_CSV.name} — "
                  f"placeholder user created ({n} records): {name}")
            clean = name.replace("(Deactivated User)", "").strip()
            deactivated = "(Deactivated User)" in name
            first, last, email = "", "", ""
            if "@" in clean:
                email = clean
            else:
                parts = clean.split()
                first = parts[0] if parts else ""
                last = " ".join(parts[1:]) if len(parts) > 1 else ""
                email = f"{clean.lower().replace(' ', '.')}@placeholder.migration"
            role, active = "associate", 0 if deactivated else 1
        if email in email_to_pk:
            user_id_map[name] = email_to_pk[email]
            continue
        cur = out.execute("""INSERT INTO accounts_user
            (password, is_superuser, email, first_name, last_name, role,
             is_active, is_staff, is_finance, date_joined, hubspot_id)
            VALUES ('!', 0, ?, ?, ?, ?, ?, 0, 0, ?, NULL)""",
            (email, first, last, role, active, STAMP))
        user_id_map[name] = email_to_pk[email] = cur.lastrowid


# Phase 2K sensitive-fields policy — sensitive_fields.csv, decided 2026-08-04.
# Every row must carry a final_action so a future CSV refresh can't silently
# reopen the question, and the migrate_to_existing set must match exactly the
# sensitive fields load_contacts actually maps. Dropped fields remain in
# raw_contacts only and must never appear in logs or sample output.
SENSITIVE_CSV = HERE / "sensitive_fields.csv"
SENSITIVE_FIELDS_MIGRATED = {"Date of Birth", "Home Address"}


def check_sensitive_policy() -> None:
    with SENSITIVE_CSV.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    undecided = [r["field_name"] for r in rows
                 if (r.get("final_action") or "").strip() not in ("drop", "migrate_to_existing")]
    if undecided:
        raise SystemExit(f"{SENSITIVE_CSV.name}: missing/unknown final_action for "
                         f"{undecided} — client must decide before building (Phase 2K)")
    migrated = {r["field_name"] for r in rows if r["final_action"].strip() == "migrate_to_existing"}
    if migrated != SENSITIVE_FIELDS_MIGRATED:
        raise SystemExit(f"{SENSITIVE_CSV.name}: migrate_to_existing fields {sorted(migrated)} "
                         f"don't match the fields this build maps {sorted(SENSITIVE_FIELDS_MIGRATED)}")
    print(f"sensitive fields policy (2K): {len(migrated)} migrated, "
          f"{len(rows) - len(migrated)} dropped (remain in raw_contacts only)")


# Reviewed names for orgs with no Company name but a URL — see
# nameless_org_names.csv. Unreviewed nameless orgs fall back to a mechanical
# guess from the domain (title-cased first label) and are reported so they
# can be added to the CSV on the next refresh.
NAMELESS_CSV = HERE / "nameless_org_names.csv"
nameless_names: dict[str, str] = {}
if NAMELESS_CSV.exists():
    with NAMELESS_CSV.open(newline="", encoding="utf-8-sig") as _f:
        nameless_names = {row["hubspot_id"]: row["name"] for row in csv.DictReader(_f)}


def guess_name_from_url(url: str) -> str:
    """Mechanical fallback: 'https://www.blossomdental.co.uk' -> 'Blossomdental'."""
    host = re.sub(r"^[a-z]+://", "", url.strip().lower()).split("/")[0]
    host = re.sub(r"^www\.", "", host)
    label = host.split(".")[0]
    label = re.sub(r"[-_]+", " ", label).strip()
    return label.title()


def load_organisations(src: sqlite3.Connection, out: sqlite3.Connection) -> None:
    unreviewed_nameless: list[str] = []
    # 1. Kept from companies_for_migration
    for r in src.execute("SELECT * FROM companies_for_migration"):
        r = dict(r)
        name = (r.get("Company name") or "").strip()
        name_guess_note = None
        if not name:
            url = (r.get("Website URL") or "").strip()
            name = nameless_names.get(r["Record ID"], "")
            if name:
                name_guess_note = (
                    f"Name guessed from the company URL `{url}` during HubSpot "
                    f"migration (no Company name in HubSpot) — reviewed in "
                    f"nameless_org_names.csv."
                )
            elif url:
                name = guess_name_from_url(url)
                unreviewed_nameless.append(f"{r['Record ID']} {url} -> {name!r}")
                name_guess_note = (
                    f"Name mechanically guessed from the company URL `{url}` "
                    f"during HubSpot migration (no Company name in HubSpot) — "
                    f"UNREVIEWED, may be wrong."
                )
        cur = out.execute("""INSERT INTO crm_organisation
            (created_at, updated_at, name, legal_name, trading_name,
             companies_house_number, address_line1, address_line2,
             address_city, address_county, address_postcode,
             url, email, phone, hubspot_id, owner_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            norm_dt(r.get("Create Date")), norm_dt(r.get("Last Modified Date")),
            name[:255], "", "", "",
            (r.get("Street Address") or "")[:255],
            (r.get("Street Address 2") or "")[:255],
            (r.get("City") or "")[:100],
            (r.get("State/Region") or "")[:100],
            (r.get("Postal Code") or "")[:10],
            (r.get("Website URL") or "")[:255],
            "",  # email populated later
            (r.get("Phone Number") or "")[:32],
            r.get("Record ID"),
            user_id_map.get(r.get("Company owner")),
        ))
        org_id_map[r["Record ID"]] = cur.lastrowid
        if name_guess_note:
            out.execute("""INSERT INTO crm_note
                (created_at, updated_at, type, organisation_id, content, datetime, hubspot_id)
                VALUES (?,?,'hubspot_migration_comment',?,?,?,NULL)""",
                (STAMP, STAMP, cur.lastrowid, name_guess_note, STAMP))

    if unreviewed_nameless:
        print(f"  ⚠ {len(unreviewed_nameless)} nameless orgs auto-guessed from URL — "
              f"review and add to {NAMELESS_CSV.name}:")
        for line in unreviewed_nameless:
            print(f"    {line}")

    # 2. New orgs from contact-typed Company Names
    for cid, name in src.execute("""
        SELECT contact_id, created_org_name FROM decisions_contact_orgs
        WHERE action = 'keep_with_typed_new_org' AND created_org_name IS NOT NULL
    """):
        cur = out.execute("""INSERT INTO crm_organisation
            (created_at, updated_at, name, legal_name, trading_name,
             companies_house_number, address_line1, address_line2,
             address_city, address_county, address_postcode,
             url, email, phone, hubspot_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (STAMP, STAMP, name[:255], "", "", "", "", "", "", "", "", "", "", ""))
        new_org_by_source[("contact", cid)] = cur.lastrowid
        out.execute("""INSERT INTO crm_note
            (created_at, updated_at, type, organisation_id, content, datetime, hubspot_id)
            VALUES (?,?,'hubspot_migration_comment',?,?,?,NULL)""",
            (STAMP, STAMP, cur.lastrowid,
             f"Auto-created during HubSpot migration from typed Company Name on Contact hubspot_id={cid}.",
             STAMP))

    # 3. New orgs from deal free-text (funder/introducer/supplier)
    for dim, raw_value in src.execute("""
        SELECT dimension, raw_value FROM decisions_deal_orgs
        WHERE action = 'created_new_org'
    """):
        cur = out.execute("""INSERT INTO crm_organisation
            (created_at, updated_at, name, legal_name, trading_name,
             companies_house_number, address_line1, address_line2,
             address_city, address_county, address_postcode,
             url, email, phone, hubspot_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (STAMP, STAMP, raw_value.strip()[:255], "", "", "", "", "", "", "", "", "", "", ""))
        new_org_by_source[(f"deal_{dim}", raw_value)] = cur.lastrowid
        out.execute("""INSERT INTO crm_note
            (created_at, updated_at, type, organisation_id, content, datetime, hubspot_id)
            VALUES (?,?,'hubspot_migration_comment',?,?,?,NULL)""",
            (STAMP, STAMP, cur.lastrowid,
             f"Auto-created during HubSpot migration from deal `{dim}` free-text field. Raw value: `{raw_value}`.",
             STAMP))


def load_contacts(src: sqlite3.Connection, out: sqlite3.Connection) -> None:
    role = {r[0]: r[1] for r in src.execute("SELECT contact_id, action FROM decisions_role_mailbox")}
    dup  = {r[0]: r[1] for r in src.execute("SELECT contact_id, action FROM decisions_duplicate_contacts")}
    name_dec = {r[0]: (r[1], r[2], r[3]) for r in src.execute(
        "SELECT contact_id, action, proposed_first_name, proposed_last_name "
        "FROM decisions_name_normalisation")}
    name_titles = {r[0]: r[1] for r in src.execute(
        "SELECT contact_id, stripped_title FROM decisions_name_normalisation "
        "WHERE action = 'apply' AND stripped_title IS NOT NULL")}
    contact_orgs_actions = {r[0]: r[1] for r in src.execute(
        "SELECT contact_id, action FROM decisions_contact_orgs")}

    def excluded(cid):
        return (role.get(cid) in ("org_contact_method", "drop")
                or dup.get(cid) in ("merge_into_primary", "drop")
                or contact_orgs_actions.get(cid) == "drop")

    dob_unparsed = 0
    for r in src.execute("SELECT * FROM contacts_for_migration"):
        r = dict(r)
        cid = r["Record ID"]
        if excluded(cid):
            continue
        first = (r.get("First Name") or "").strip()
        last  = (r.get("Last Name")  or "").strip()
        if cid in name_dec:
            act, pf, pl = name_dec[cid]
            if act == "apply":
                first = (pf or first)
                last  = (pl or last)
        title = (name_titles.get(cid) or "")[:20]
        line1, line2 = split_address(r.get("Home Address"))
        # Phase 2K decision: primary "Date of Birth" column wins over the
        # duplicate export column; a note records the conflict on the contact.
        dob = norm_date(r.get("Date of Birth"))
        dob_alt = norm_date(r.get("Date of Birth__2"))
        if (r.get("Date of Birth") and not dob) or (r.get("Date of Birth__2") and not dob_alt):
            dob_unparsed += 1
        cur = out.execute("""INSERT INTO crm_contact
            (created_at, updated_at, title, first_name, last_name,
             email, phone, date_of_birth,
             home_address_line1, home_address_line2, home_address_city,
             home_address_county, home_address_postcode, hubspot_id, user_id,
             owner_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""", (
            norm_dt(r.get("Create Date")), norm_dt(r.get("Last Modified Date")),
            title, first[:150], last[:150],
            (r.get("Email") or "")[:254],
            (r.get("Phone Number") or "")[:32],
            dob or dob_alt,
            line1, line2, "", "",
            (r.get("Home Postcode") or "")[:10],
            cid,
            user_id_map.get(r.get("Contact owner")),
        ))
        contact_id_map[cid] = cur.lastrowid
        if dob and dob_alt and dob != dob_alt:
            out.execute("""INSERT INTO crm_note
                (created_at, updated_at, type, contact_id, content, datetime, hubspot_id)
                VALUES (?,?,'hubspot_migration_comment',?,?,?,NULL)""",
                (STAMP, STAMP, cur.lastrowid,
                 f"HubSpot export held two conflicting Date of Birth values; "
                 f"kept {dob} from the primary column, the duplicate column said {dob_alt}. "
                 f"Please verify with the client.",
                 STAMP))

    # Synthesised contacts from deals
    for r in src.execute("SELECT * FROM synthesised_deal_contacts"):
        r = dict(r)
        line1, line2 = split_address(r.get("home_address"))
        if r.get("date_of_birth") and not norm_date(r.get("date_of_birth")):
            dob_unparsed += 1
        cur = out.execute("""INSERT INTO crm_contact
            (created_at, updated_at, title, first_name, last_name,
             email, phone, date_of_birth,
             home_address_line1, home_address_line2,
             home_address_city, home_address_county, home_address_postcode,
             hubspot_id, user_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)""", (
            STAMP, STAMP,
            (r.get("title") or "")[:20],
            (r.get("first_name") or "")[:150],
            (r.get("last_name") or "")[:150],
            r.get("email"),
            (r.get("phone") or r.get("mobile") or "")[:32],
            norm_date(r.get("date_of_birth")),
            line1, line2, "", "",
            (r.get("home_postcode") or "")[:10],
        ))
        synth_contact_map[r["email"]] = cur.lastrowid
        for did in (r.get("source_deal_ids") or "").split(";"):
            synth_contact_map[f"__deal__{did.strip()}"] = cur.lastrowid
        out.execute("""INSERT INTO crm_note
            (created_at, updated_at, type, contact_id, content, datetime, hubspot_id)
            VALUES (?,?,'hubspot_migration_comment',?,?,?,NULL)""",
            (STAMP, STAMP, cur.lastrowid,
             f"Auto-created during HubSpot migration from Deal P1 fields on deal(s) {r.get('source_deal_ids')}.",
             STAMP))

    if dob_unparsed:
        print(f"  ⚠ {dob_unparsed} contacts had a DOB norm_date couldn't parse — "
              f"left NULL (values withheld; inspect raw_contacts directly)")


def load_m2m(src: sqlite3.Connection, out: sqlite3.Connection) -> None:
    for cid, linked_ids in src.execute("""
        SELECT contact_id, linked_org_ids FROM decisions_contact_orgs
        WHERE linked_org_ids IS NOT NULL
    """):
        django_contact = contact_id_map.get(cid)
        if django_contact is None:
            continue
        for oid in linked_ids.split(";"):
            oid = oid.strip()
            django_org = org_id_map.get(oid)
            if django_org:
                try:
                    out.execute("""INSERT INTO crm_contact_organisations
                        (contact_id, organisation_id) VALUES (?,?)""",
                        (django_contact, django_org))
                except sqlite3.IntegrityError:
                    pass

    for cid, in src.execute("""
        SELECT contact_id FROM decisions_contact_orgs
        WHERE action = 'keep_with_typed_new_org'
    """):
        django_contact = contact_id_map.get(cid)
        django_org = new_org_by_source.get(("contact", cid))
        if django_contact and django_org:
            try:
                out.execute("""INSERT INTO crm_contact_organisations
                    (contact_id, organisation_id) VALUES (?,?)""",
                    (django_contact, django_org))
            except sqlite3.IntegrityError:
                pass


def load_deals_and_children(src: sqlite3.Connection, out: sqlite3.Connection) -> None:
    def deal_org_lookup(dim: str, raw_value: str | None) -> int | None:
        if not raw_value:
            return None
        row = src.execute(
            "SELECT action, matched_org_id FROM decisions_deal_orgs "
            "WHERE dimension = ? AND raw_value = ?",
            (dim, raw_value),
        ).fetchone()
        if not row:
            return None
        if row["action"] == "auto_matched" and row["matched_org_id"]:
            return org_id_map.get(row["matched_org_id"])
        if row["action"] == "created_new_org":
            return new_org_by_source.get((f"deal_{dim}", raw_value))
        return None

    for r in src.execute("SELECT * FROM deals_for_migration"):
        r = dict(r)
        did = r["Record ID"]

        customer_django_id = None
        assoc_ids = r.get("Associated Contact IDs")
        if assoc_ids:
            first_cid = assoc_ids.split(";")[0].strip()
            customer_django_id = contact_id_map.get(first_cid)
        if customer_django_id is None:
            customer_django_id = synth_contact_map.get(f"__deal__{did}")

        organisation_django_id = None
        pids = r.get("Associated Company IDs (Primary)")
        if pids:
            organisation_django_id = org_id_map.get(pids.split(";")[0].strip())

        introducer_django_id = deal_org_lookup("introducer", r.get("Introducer"))
        owner_django_id = user_id_map.get(r.get("Deal owner"))

        # Source: HubSpot's pick-list wins. Where it's blank (300 in-scope
        # deals) and the deal's Introducer resolved to a real external org
        # (not MediFinance itself, not junk like "N/A"), the deal evidently
        # came via that introducer — recorded in source_inference_audit.csv.
        raw_source = (r.get("Source") or "").strip()
        source = SOURCE_CODES.get(raw_source) if raw_source else None
        if raw_source and source is None and raw_source not in _unknown_sources:
            _unknown_sources.add(raw_source)
            print(f"  ⚠ unknown deal Source value {raw_source!r} — left NULL "
                  f"(new HubSpot pick-list value? add to SOURCE_CODES)")
        if source is not None:
            source_counts["from Source"] += 1
        elif (introducer_django_id is not None
              and introducer_django_id != org_id_map.get(MEDIFINANCE_ORG_HS_ID)):
            source = "introducer"
            source_counts["inferred from Introducer"] += 1
            source_audit.append((did, r.get("Deal Name") or "", (r.get("Introducer") or "").strip()))
        else:
            source_counts["blank"] += 1

        # Projected end of the finance term: HubSpot's explicit "End date"
        # wins; else Close Date + Term — the same arithmetic the stage
        # derivation uses for its settlement check.
        term_end = as_date(r.get("End date"))
        if term_end is not None:
            term_end_counts["from End date"] += 1
        else:
            close_d = as_date(r.get("Close Date") or r.get("Last Modified Date"))
            months = parse_term_months(r.get("Term"))
            term_end = add_months(close_d, months) if close_d and months else None
            term_end_counts["from Close Date + Term" if term_end else "no term data"] += 1

        pipeline = (r.get("Pipeline") or "").strip()

        cur = out.execute("""INSERT INTO crm_deal
            (created_at, updated_at, name, type, source, source_identifier,
             owner_id, customer_id,
             organisation_id, introducer_id,
             first_payment_date, term_end_date, mf_invoice_number,
             selected_quote_id, selected_proposal_id, hubspot_id,
             created_via_api_key_id)
            VALUES (?,?,?,?,?,'',?,?,?,?,?,?,?,?,?,?,NULL)""", (
            norm_dt(r.get("Create Date")), norm_dt(r.get("Last Modified Date")),
            (r.get("Deal Name") or f"Deal {did}")[:255],
            PIPELINE_TYPE.get(pipeline), source,
            owner_django_id, customer_django_id,
            organisation_django_id, introducer_django_id,
            # "Salesman Commission" and "Doc Fee" are NOT carried: commission
            # and document fee live on Quote now (see field_map.md) — port
            # them to a quote per deal when the migration is redone.
            None,
            term_end.isoformat() if term_end else None,
            (r.get("Invoice No") or "").strip()[:32],
            None, None, did,
        ))
        deal_id_map[did] = cur.lastrowid

        # Participation. Asset Finance is the only pipeline where a supplier
        # genuinely applies; every other pipeline's participation is
        # supplier-less by assumption, with any HubSpot supplier text kept in
        # the description so nothing is silently dropped.
        supplier = (r.get("Supplier") or "").strip()
        amount = as_decimal(r.get("Funded Amount")) or as_decimal(r.get("Amount"))
        # "Invoice No" is MF's own commission invoice (MF#### refs), not the
        # supplier's — it lives on Deal.mf_invoice_number, so blank here.
        supplier_org_id, description, needed = None, "", True
        if pipeline == "Asset Finance" and supplier:
            supplier_org_id = deal_org_lookup("supplier", supplier)
            if supplier_org_id is None:
                description = (f"HubSpot listed supplier as `{supplier}` "
                               "but no matching organisation was migrated — "
                               "add the supplier when known.")
                participation_counts["supplier unmatched, with note"] += 1
            else:
                participation_counts["with supplier"] += 1
        elif pipeline == "Asset Finance" and amount is not None:
            description = ("HubSpot Asset Finance deal had no Supplier "
                           "recorded; amount migrated without a supplier — "
                           "add the supplier when known.")
            participation_counts["orphan with note (Asset Finance)"] += 1
        elif supplier:
            description = (f"HubSpot listed supplier as `{supplier}` — "
                           "suppliers don't apply to this pipeline; kept "
                           "for reference.")
            participation_counts["orphan, supplier kept in note"] += 1
        elif amount is not None:
            participation_counts["orphan"] += 1
        else:
            needed = False
            participation_counts["no supplier, no amount"] += 1
        if needed:
            out.execute("""INSERT INTO crm_participation
                (created_at, updated_at, deal_id, organisation_id,
                 amount, description, invoice_number, invoice, invoice_contact_id)
                VALUES (?,?,?,?,?,?,?,NULL,NULL)""",
                (STAMP, STAMP, cur.lastrowid, supplier_org_id,
                 amount or "0.00", description, ""))

        # Proposal — lender is required, so skip if we can't resolve
        funder = r.get("Funder")
        if funder and funder.strip():
            lender_org_id = deal_org_lookup("funder", funder)
            if lender_org_id:
                out.execute("""INSERT INTO crm_proposal
                    (created_at, updated_at, deal_id, lender_id,
                     proposal_number, status, contact_id)
                    VALUES (?,?,?,?,?,?,NULL)""",
                    (STAMP, STAMP, cur.lastrowid, lender_org_id,
                     (r.get("Funder Ref:") or "")[:100], "submitted"))

        # Reconstructed stage history (Phase 2J rules)
        for stage_name, occurred_at, note in derive_stage_events(r):
            out.execute("""INSERT INTO crm_stage
                (created_at, updated_at, deal_id, name, occurred_at, note)
                VALUES (?,?,?,?,?,?)""",
                (STAMP, STAMP, cur.lastrowid, stage_name, occurred_at, note))

    print("deal term ends: "
          + ", ".join(f"{n} {src}" for src, n in sorted(term_end_counts.items())))
    print("participations: "
          + ", ".join(f"{n} {kind}" for kind, n in sorted(participation_counts.items())))
    print("deal sources: "
          + ", ".join(f"{n} {src}" for src, n in sorted(source_counts.items())))


# ─── Org merge decisions (org_merge_candidates_decisions.csv) ─────────────
#
# Actions per row: keep_separate (or blank), drop, merge_into <target>,
# split_into <A + B [+ C]>. Targets are a source_id or an org name; resolved
# within the cluster first, then globally, else a new canonical org is
# created with that name (covers "merge all these misspellings into X").
# The row whose merge_into target resolves to itself is the canonical marker.

MERGE_CSV = HERE / "org_merge_candidates_decisions.csv"
merge_warnings: list[str] = []


HUBSPOT_PORTAL_ID = "3378161"


def load_hubspot_notes_and_emails(src: sqlite3.Connection, out: sqlite3.Connection) -> None:
    """Port HubSpot note engagements (content is in the export) and email
    engagements (IDs only — become links into the HubSpot timeline) as Note
    rows. One row per engagement, attached to every associated migrated
    record via the contact/organisation/deal FKs; `hubspot_id` (unique)
    carries the engagement ID.

    The export semicolon-joins both the note bodies ("Associated Note") and
    their IDs ("Associated Note IDs"). Bodies containing ';' break the
    pairing — when the two lists disagree in length, the whole blob becomes
    one combined note keyed by the first ID rather than guessing.
    """
    # (source view, id map, Note FK column, HubSpot object-type code, anchor rank)
    SOURCES = [
        ("deals_for_migration",    deal_id_map,    "deal_id",         "0-3", 0),
        ("contacts_for_migration", contact_id_map, "contact_id",      "0-1", 1),
        ("companies_for_migration", org_id_map,    "organisation_id", "0-2", 2),
    ]
    notes: dict[str, dict] = {}   # engagement id -> {content, fks, anchor}
    emails: dict[str, dict] = {}
    combined = 0

    for view, id_map, fk, code, rank in SOURCES:
        for r in src.execute(f'SELECT * FROM "{view}"'):
            r = dict(r)
            rec_id = r["Record ID"]
            pk = id_map.get(rec_id)
            if pk is None:   # dropped contact / unmigrated record
                continue

            note_ids = [i.strip() for i in (r.get("Associated Note IDs") or "").split(";") if i.strip()]
            bodies = [b.strip() for b in (r.get("Associated Note") or "").split(";")]
            if note_ids:
                if len(bodies) == len(note_ids):
                    pairs = zip(note_ids, bodies)
                else:
                    pairs = [(note_ids[0], (r.get("Associated Note") or "").strip())]
                    combined += 1 if len(note_ids) > 1 else 0
                for eng_id, body in pairs:
                    entry = notes.setdefault(eng_id, {"content": "", "fks": {}, "anchor": None})
                    entry["fks"][fk] = pk
                    if body and len(body) > len(entry["content"]):
                        entry["content"] = body
                    if entry["anchor"] is None or rank < entry["anchor"][2]:
                        entry["anchor"] = (code, rec_id, rank)

            for eng_id in [i.strip() for i in (r.get("Associated Email IDs") or "").split(";") if i.strip()]:
                entry = emails.setdefault(eng_id, {"fks": {}, "anchor": None})
                entry["fks"][fk] = pk
                if entry["anchor"] is None or rank < entry["anchor"][2]:
                    entry["anchor"] = (code, rec_id, rank)

            # The record-level free-text `Notes` property (deals + contacts)
            prop = (r.get("Notes") or "").strip()
            if prop:
                out.execute("""INSERT INTO crm_note
                    (created_at, updated_at, type, contact_id, organisation_id,
                     deal_id, content, datetime, hubspot_id)
                    VALUES (?,?,'hubspot_note',?,?,?,?,?,?)""",
                    (STAMP, STAMP,
                     pk if fk == "contact_id" else None,
                     pk if fk == "organisation_id" else None,
                     pk if fk == "deal_id" else None,
                     prop, norm_dt(r.get("Last Modified Date")),
                     f"notes-prop-{code}-{rec_id}"))

    def hubspot_url(anchor: tuple, eng_id: str) -> str:
        code, rec_id, _ = anchor
        return (f"https://app.hubspot.com/contacts/{HUBSPOT_PORTAL_ID}"
                f"/record/{code}/{rec_id}/?engagement={eng_id}")

    for eng_id, e in notes.items():
        content = e["content"] or f"[Note logged in HubSpot]({hubspot_url(e['anchor'], eng_id)})"
        out.execute("""INSERT INTO crm_note
            (created_at, updated_at, type, contact_id, organisation_id, deal_id,
             content, datetime, hubspot_id)
            VALUES (?,?,'hubspot_note',?,?,?,?,?,?)""",
            (STAMP, STAMP, e["fks"].get("contact_id"), e["fks"].get("organisation_id"),
             e["fks"].get("deal_id"), content, STAMP, eng_id))

    for eng_id, e in emails.items():
        content = f"[Email logged in HubSpot]({hubspot_url(e['anchor'], eng_id)})"
        out.execute("""INSERT INTO crm_note
            (created_at, updated_at, type, contact_id, organisation_id, deal_id,
             content, datetime, hubspot_id)
            VALUES (?,?,'hubspot_email',?,?,?,?,?,?)""",
            (STAMP, STAMP, e["fks"].get("contact_id"), e["fks"].get("organisation_id"),
             e["fks"].get("deal_id"), content, STAMP, eng_id))

    print(f"hubspot engagements: {len(notes)} notes, {len(emails)} email links"
          + (f" ({combined} multi-note blobs kept combined — ';' in a body)" if combined else ""))


def _decision_org_django_id(source: str, source_id: str) -> int | None:
    if source == "existing_org":
        return org_id_map.get(source_id)
    if source == "created_from_contact":
        return new_org_by_source.get(("contact", source_id))
    if source.startswith("created_from_deal_"):
        dim = source[len("created_from_deal_"):]
        return new_org_by_source.get((f"deal_{dim}", source_id))
    return None


def apply_org_merge_decisions(src: sqlite3.Connection, out: sqlite3.Connection) -> None:
    with open(MERGE_CSV, newline="") as f:
        rows = list(csv.DictReader(f))
    clusters: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        r["action"] = (r.get("action") or "").strip()
        clusters[r["cluster_id"]].append(r)

    created_canonicals: dict[str, int] = {}  # lowercased name -> django id

    def create_canonical(name: str) -> int:
        key = name.lower()
        if key not in created_canonicals:
            cur = out.execute("""INSERT INTO crm_organisation
                (created_at, updated_at, name, legal_name, trading_name,
                 companies_house_number, address_line1, address_line2,
                 address_city, address_county, address_postcode,
                 url, email, phone, hubspot_id)
                VALUES (?,?,?,'','','','','','','','','','','',NULL)""",
                (STAMP, STAMP, name[:255]))
            out.execute("""INSERT INTO crm_note
                (created_at, updated_at, type, organisation_id, content, datetime, hubspot_id)
                VALUES (?,?,'hubspot_migration_comment',?,?,?,NULL)""",
                (STAMP, STAMP, cur.lastrowid,
                 "Created as the canonical record for a duplicate-spelling "
                 "cluster during HubSpot migration (org merge review).", STAMP))
            created_canonicals[key] = cur.lastrowid
        return created_canonicals[key]

    def near_id(a: str, b: str) -> bool:
        """True if two numeric ids differ by at most one digit (typo tolerance)."""
        return (a.isdigit() and b.isdigit() and len(a) == len(b)
                and sum(x != y for x, y in zip(a, b)) <= 1)

    def cluster_canonical(cluster: list[dict]) -> int | None:
        """The row whose merge_into target is itself (by source_id or name)."""
        canon = []
        for c in cluster:
            m = re.match(r"merge_into[_ ]+(.+)$", c["action"])
            if not m:
                continue
            t = m.group(1).strip()
            if t == c["source_id"] or t == c["name"].strip() or near_id(t, c["source_id"]):
                canon.append(_decision_org_django_id(c["source"], c["source_id"]))
        canon = [c for c in canon if c]
        return canon[0] if len(canon) == 1 else None

    def resurrect_org(oid: str) -> int | None:
        """Create an org from a raw_companies row that was excluded as an
        orphan but has now been chosen as a cluster's canonical record."""
        r = src.execute('SELECT * FROM raw_companies WHERE "Record ID"=?', (oid,)).fetchone()
        if not r:
            return None
        r = dict(r)
        cur = out.execute("""INSERT INTO crm_organisation
            (created_at, updated_at, name, legal_name, trading_name,
             companies_house_number, address_line1, address_line2,
             address_city, address_county, address_postcode,
             url, email, phone, hubspot_id)
            VALUES (?,?,?,'','','',?,?,?,?,?,?,'',?,?)""", (
            norm_dt(r.get("Create Date")), norm_dt(r.get("Last Modified Date")),
            (r.get("Company name") or "")[:255],
            (r.get("Street Address") or "")[:255],
            (r.get("Street Address 2") or "")[:255],
            (r.get("City") or "")[:100],
            (r.get("State/Region") or "")[:100],
            (r.get("Postal Code") or "")[:10],
            (r.get("Website URL") or "")[:255],
            (r.get("Phone Number") or "")[:32],
            oid))
        out.execute("""INSERT INTO crm_note
            (created_at, updated_at, type, organisation_id, content, datetime, hubspot_id)
            VALUES (?,?,'hubspot_migration_comment',?,?,?,NULL)""",
            (STAMP, STAMP, cur.lastrowid,
             "Originally excluded as a zero-signal orphan company, then chosen "
             "as a cluster's canonical record in the org merge review.", STAMP))
        org_id_map[oid] = cur.lastrowid
        return cur.lastrowid

    def resolve_target(row: dict, target: str, allow_create: bool) -> int | None:
        cluster = clusters[row["cluster_id"]]
        for c in cluster:
            if c["source_id"] == target:
                return _decision_org_django_id(c["source"], c["source_id"])
        for c in cluster:
            if c["name"].strip().lower() == target.lower():
                return _decision_org_django_id(c["source"], c["source_id"])
        if target in org_id_map:
            return org_id_map[target]
        hits = out.execute("SELECT id FROM crm_organisation WHERE lower(name)=lower(?)",
                           (target,)).fetchall()
        if len(hits) == 1:
            return hits[0][0]
        if target.isdigit():
            resurrected = resurrect_org(target)
            if resurrected:
                return resurrected
            for c in cluster:  # tolerate one-digit typos of a cluster member's id
                if near_id(target, c["source_id"]):
                    return _decision_org_django_id(c["source"], c["source_id"])
            fallback = cluster_canonical(cluster)
            merge_warnings.append(
                f"cluster {row['cluster_id']}: target id {target} not found; "
                + ("fell back to cluster canonical." if fallback else "row left unmerged."))
            return fallback
        if allow_create:
            return create_canonical(target)
        return None

    merge_map: dict[int, int] = {}
    split_map: dict[int, list[int]] = {}
    drop_set: set[int] = set()
    org_names: dict[int, str] = {}

    for r in rows:
        a = r["action"]
        if a in ("", "keep_separate"):
            continue
        own = _decision_org_django_id(r["source"], r["source_id"])
        if own is None:
            merge_warnings.append(
                f"cluster {r['cluster_id']}: no migrated org found for "
                f"{r['source']} / {r['source_id']} — action '{a}' skipped.")
            continue
        org_names[own] = r["name"]
        if a == "drop":
            drop_set.add(own)
            continue
        m = re.match(r"(merge_into|split_into)[_ ]+(.+)$", a)
        if not m:
            merge_warnings.append(f"cluster {r['cluster_id']}: unrecognised action '{a}'.")
            continue
        if m.group(1) == "merge_into":
            tid = resolve_target(r, m.group(2).strip(), allow_create=True)
            if tid and tid != own:
                merge_map[own] = tid
        else:
            tids = []
            for part in m.group(2).split(" + "):
                tid = resolve_target(r, part.strip(), allow_create=True)
                if tid:
                    tids.append(tid)
                else:
                    merge_warnings.append(
                        f"cluster {r['cluster_id']}: split target '{part.strip()}' "
                        f"of '{r['name']}' not resolved — omitted.")
            if tids:
                split_map[own] = tids

    def final_target(oid: int) -> int:
        seen = {oid}
        while oid in merge_map:
            oid = merge_map[oid]
            if oid in seen:
                break
            seen.add(oid)
        return oid

    def repoint(old: int, new: int) -> None:
        out.execute("UPDATE OR IGNORE crm_contact_organisations SET organisation_id=? "
                    "WHERE organisation_id=?", (new, old))
        out.execute("DELETE FROM crm_contact_organisations WHERE organisation_id=?", (old,))
        for table, col in (("crm_deal", "organisation_id"), ("crm_deal", "introducer_id"),
                           ("crm_participation", "organisation_id"),
                           ("crm_proposal", "lender_id"), ("crm_stage", "organisation_id"),
                           ("crm_note", "organisation_id")):
            out.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new, old))
        out.execute("""INSERT INTO crm_note
            (created_at, updated_at, type, organisation_id, content, datetime, hubspot_id)
            VALUES (?,?,'hubspot_migration_comment',?,?,?,NULL)""",
            (STAMP, STAMP, new,
             f"Merged in duplicate spelling `{org_names.get(old, '?')}` during "
             f"HubSpot migration (org merge review).", STAMP))
        # Keep the merged org's owner if the survivor doesn't have one.
        out.execute("""UPDATE crm_organisation
            SET owner_id = COALESCE(owner_id,
                (SELECT owner_id FROM crm_organisation WHERE id=?))
            WHERE id=?""", (old, new))
        out.execute("DELETE FROM crm_organisation WHERE id=?", (old,))

    def split(old: int, targets: list[int]) -> None:
        for pid, deal_id, amount, inv in out.execute(
                "SELECT id, deal_id, amount, invoice_number FROM crm_participation "
                "WHERE organisation_id=?", (old,)).fetchall():
            total = float(amount or 0)
            share = round(total / len(targets), 2)
            first_share = round(total - share * (len(targets) - 1), 2)
            for i, t in enumerate(targets):
                out.execute("""INSERT INTO crm_participation
                    (created_at, updated_at, deal_id, organisation_id,
                     amount, description, invoice_number, invoice, invoice_contact_id)
                    VALUES (?,?,?,?,?,?,?,NULL,NULL)""",
                    (STAMP, STAMP, deal_id, t,
                     f"{(first_share if i == 0 else share):.2f}",
                     f"HubSpot listed supplier as `{org_names.get(old, '?')}`; amount "
                     f"split equally across the {len(targets)} organisations during "
                     f"migration — actual split unknown.", inv))
            out.execute("DELETE FROM crm_participation WHERE id=?", (pid,))
        for pid, deal_id, pn, status in out.execute(
                "SELECT id, deal_id, proposal_number, status FROM crm_proposal "
                "WHERE lender_id=?", (old,)).fetchall():
            for t in targets:
                out.execute("""INSERT INTO crm_proposal
                    (created_at, updated_at, deal_id, lender_id,
                     proposal_number, status, contact_id)
                    VALUES (?,?,?,?,?,?,NULL)""",
                    (STAMP, STAMP, deal_id, t, pn, status))
            out.execute("DELETE FROM crm_proposal WHERE id=?", (pid,))
        for (cid,) in out.execute("SELECT contact_id FROM crm_contact_organisations "
                                  "WHERE organisation_id=?", (old,)).fetchall():
            for t in targets:
                try:
                    out.execute("INSERT INTO crm_contact_organisations "
                                "(contact_id, organisation_id) VALUES (?,?)", (cid, t))
                except sqlite3.IntegrityError:
                    pass
        out.execute("DELETE FROM crm_contact_organisations WHERE organisation_id=?", (old,))
        # single-slot FKs can only take one org — first-listed wins
        for table, col in (("crm_deal", "organisation_id"), ("crm_deal", "introducer_id"),
                           ("crm_stage", "organisation_id"), ("crm_note", "organisation_id")):
            out.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (targets[0], old))
        out.execute("DELETE FROM crm_organisation WHERE id=?", (old,))

    def drop(old: int) -> None:
        out.execute("DELETE FROM crm_contact_organisations WHERE organisation_id=?", (old,))
        for table, col in (("crm_deal", "organisation_id"), ("crm_deal", "introducer_id"),
                           ("crm_participation", "organisation_id"),
                           ("crm_stage", "organisation_id")):
            out.execute(f"UPDATE {table} SET {col}=NULL WHERE {col}=?", (old,))
        out.execute("DELETE FROM crm_proposal WHERE lender_id=?", (old,))
        # Notes shared with a kept deal/contact survive with the org detached;
        # org-only notes go with the org.
        out.execute("""UPDATE crm_note SET organisation_id=NULL
            WHERE organisation_id=? AND (deal_id IS NOT NULL OR contact_id IS NOT NULL)""", (old,))
        out.execute("DELETE FROM crm_note WHERE organisation_id=?", (old,))
        out.execute("DELETE FROM crm_organisation WHERE id=?", (old,))

    for old in drop_set:
        drop(old)
    for old, targets in split_map.items():
        split(old, [final_target(t) for t in targets])
    for old in merge_map:
        repoint(old, final_target(old))

    print(f"org merge decisions: {len(merge_map)} merged, {len(split_map)} split, "
          f"{len(drop_set)} dropped, {len(created_canonicals)} canonical orgs created")
    for w in merge_warnings:
        print(f"  ⚠ {w}")


def derive_org_sectors(src: sqlite3.Connection, out: sqlite3.Connection) -> None:
    """Populate crm_organisation.sector from HubSpot evidence, best tier wins:
      A) majority of the org's deals' Sector (ties → most recent deal),
      B) company Industry, only where auto-enrichment proved trustworthy,
      C) unanimous Sector across the org's linked contacts.
    Must run after apply_org_merge_decisions so deal/contact evidence has been
    repointed onto surviving orgs; writes sector_derivation_audit.csv."""

    def sector_code(label: str, source: str) -> str | None:
        code = SECTOR_CODES.get(label)
        if code is None:
            print(f"  ⚠ unknown {source} Sector value {label!r} — ignored "
                  f"(new HubSpot pick-list value? add to SECTOR_CODES)")
        return code

    deal_sector: dict[str, str] = {}
    for r in src.execute('SELECT "Record ID", "Sector" FROM raw_deals '
                         'WHERE "Sector" IS NOT NULL AND "Sector" <> \'\''):
        code = sector_code(r[1], "deal")
        if code:
            deal_sector[r[0]] = code
    contact_sector: dict[str, str] = {}
    for r in src.execute('SELECT "Record ID", "Sector" FROM raw_contacts '
                         'WHERE "Sector" IS NOT NULL AND "Sector" <> \'\''):
        code = sector_code(r[1], "contact")
        if code:
            contact_sector[r[0]] = code
    company_industry = {r[0]: r[1] for r in src.execute(
        'SELECT "Record ID", "Industry" FROM raw_companies '
        'WHERE "Industry" IS NOT NULL AND "Industry" <> \'\'')}

    # Tier A — org → sector → [deal count, latest deal created_at]
    votes: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(lambda: [0, ""]))
    for org_id, hs_id, created in out.execute(
            "SELECT organisation_id, hubspot_id, created_at FROM crm_deal "
            "WHERE organisation_id IS NOT NULL AND hubspot_id IS NOT NULL"):
        code = deal_sector.get(hs_id)
        if code:
            v = votes[org_id][code]
            v[0] += 1
            v[1] = max(v[1], created or "")

    assigned: dict[int, str] = {}
    audit: list[tuple[int, str, str, str]] = []
    conflicts = 0
    for org_id, by_code in votes.items():
        ranked = sorted(by_code.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)
        code = ranked[0][0]
        assigned[org_id] = code
        detail = ", ".join(f"{c}×{v[0]}" for c, v in ranked)
        audit.append((org_id, "deals", code, detail))
        if len(by_code) > 1:
            conflicts += 1
            out.execute("""INSERT INTO crm_note
                (created_at, updated_at, type, organisation_id, content, datetime, hubspot_id)
                VALUES (?,?,'hubspot_migration_comment',?,?,?,NULL)""",
                (STAMP, STAMP, org_id,
                 f"HubSpot deals disagreed on this organisation's sector ({detail}); "
                 f"kept `{code}` (majority, ties broken by most recent deal) during migration.",
                 STAMP))

    # Tier B — Industry, only for orgs with no deal evidence
    tier_b = 0
    for org_id, hs_id in out.execute(
            "SELECT id, hubspot_id FROM crm_organisation WHERE hubspot_id IS NOT NULL"):
        if org_id in assigned:
            continue
        industry = company_industry.get(hs_id)
        code = INDUSTRY_TO_SECTOR.get(industry)
        if code:
            assigned[org_id] = code
            tier_b += 1
            audit.append((org_id, "industry", code, f"Industry={industry}"))

    # Tier C — unanimous contact sectors, for orgs still unset
    org_contact_codes: dict[int, set[str]] = defaultdict(set)
    for org_id, hs_cid in out.execute(
            "SELECT co.organisation_id, c.hubspot_id FROM crm_contact_organisations co "
            "JOIN crm_contact c ON c.id = co.contact_id WHERE c.hubspot_id IS NOT NULL"):
        code = contact_sector.get(hs_cid)
        if code:
            org_contact_codes[org_id].add(code)
    tier_c = tier_c_conflicts = 0
    for org_id, codes in org_contact_codes.items():
        if org_id in assigned:
            continue
        if len(codes) == 1:
            code = next(iter(codes))
            assigned[org_id] = code
            tier_c += 1
            audit.append((org_id, "contacts", code, "unanimous contact sector"))
        else:
            tier_c_conflicts += 1

    for org_id, code in assigned.items():
        out.execute("UPDATE crm_organisation SET sector=? WHERE id=?", (code, org_id))

    names = {r[0]: r[1] for r in out.execute("SELECT id, name FROM crm_organisation")}
    with open(HERE / "sector_derivation_audit.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["org_id", "org_name", "tier", "sector", "evidence"])
        for org_id, tier, code, evidence in sorted(audit):
            w.writerow([org_id, names.get(org_id, ""), tier, code, evidence])

    total = out.execute("SELECT COUNT(*) FROM crm_organisation").fetchone()[0]
    print(f"org sectors: {len(votes)} from deals ({conflicts} conflicts resolved), "
          f"{tier_b} from company Industry, {tier_c} from contact sectors "
          f"({tier_c_conflicts} skipped as conflicting), {total - len(assigned)} unset")


def load_summary_note(out: sqlite3.Connection) -> None:
    orgs = out.execute("SELECT COUNT(*) FROM crm_organisation").fetchone()[0]
    contacts = out.execute("SELECT COUNT(*) FROM crm_contact").fetchone()[0]
    deals = out.execute("SELECT COUNT(*) FROM crm_deal").fetchone()[0]
    out.execute("""INSERT INTO crm_note
        (created_at, updated_at, type, content, datetime, hubspot_id)
        VALUES (?,?,'hubspot_migration_comment',?,?,NULL)""",
        (STAMP, STAMP,
         f"Preview build {STAMP[:10]}. {orgs} Organisations, {contacts} Contacts, "
         f"{deals} Deals loaded. Stage histories reconstructed via Phase 2J rules "
         f"(see stage_derivation_audit.csv); org merge review applied; user email "
         f"mapping (2I) and sensitive fields policy (2K) applied per decision CSVs. "
         f"All Phase 2 client decisions are resolved.", STAMP))


def main() -> int:
    if not TEMPLATE_DB.exists():
        raise SystemExit(f"template not found: {TEMPLATE_DB}")
    if OUT_DB.exists():
        OUT_DB.unlink()
    shutil.copy(TEMPLATE_DB, OUT_DB)

    src = sqlite3.connect(SRC_DB)
    src.row_factory = sqlite3.Row
    out = sqlite3.connect(OUT_DB)

    check_sensitive_policy()
    clear_target(out)
    load_users(src, out)
    load_organisations(src, out)
    load_contacts(src, out)
    load_m2m(src, out)
    load_deals_and_children(src, out)
    load_hubspot_notes_and_emails(src, out)
    apply_org_merge_decisions(src, out)
    derive_org_sectors(src, out)
    load_summary_note(out)
    out.commit()

    with open(HERE / "stage_derivation_audit.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pipeline", "hubspot_stage", "derived_current_stage", "deal_count"])
        for (pipeline, hs_stage, final), n in sorted(stage_audit.items(),
                                                     key=lambda kv: -kv[1]):
            w.writerow([pipeline, hs_stage, final, n])

    with open(HERE / "source_inference_audit.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hubspot_id", "deal_name", "introducer_text"])
        w.writerows(sorted(source_audit, key=lambda t: t[1].lower()))

    counts = {t: out.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("accounts_user","crm_organisation","crm_contact",
                        "crm_contact_organisations","crm_deal","crm_participation",
                        "crm_proposal","crm_stage","crm_note")}
    print(f"→ {OUT_DB.name}")
    for t, n in counts.items():
        print(f"  {n:>6}  {t}")
    owned = {t: out.execute(
        f"SELECT COUNT(*) FROM {t} WHERE owner_id IS NOT NULL").fetchone()[0]
        for t in ("crm_contact", "crm_organisation", "crm_deal")}
    print(f"  owners: {owned['crm_contact']} contacts, "
          f"{owned['crm_organisation']} organisations, {owned['crm_deal']} deals")

    out.close()
    src.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
