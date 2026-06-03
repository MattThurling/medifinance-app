"""Import HubSpot data from CSVs in an ``import/`` directory.

Contract: docs/HUBSPOT_IMPORT.md

Each row is upserted by ``hubspot_id`` — re-running is safe and idempotent.
The whole import runs in a single transaction; any data error rolls everything
back so partial imports don't leave the DB in a half-state.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterator

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Role, User
from crm.models import Contact, Deal, Organisation


class Command(BaseCommand):
    help = "Import HubSpot data from CSVs in an import/ directory."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dir",
            default=str(settings.BASE_DIR / "import"),
            help="Directory containing the CSV files (default: ./import).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and report counts, then roll back without committing.",
        )

    def handle(self, *args, dir, dry_run, **options):  # noqa: A002 - django uses `dir`
        base = Path(dir)
        if not base.is_dir():
            raise CommandError(f"Directory not found: {base}")

        self.stdout.write(self.style.MIGRATE_HEADING(f"Importing from {base}"))

        with transaction.atomic():
            self._import_users(base / "users.csv")
            self._import_organisations(base / "organisations.csv")
            self._import_contacts(base / "contacts.csv")
            self._import_deals(base / "deals.csv")

            if dry_run:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("\nDry run — all changes rolled back."))
            else:
                self.stdout.write(self.style.SUCCESS("\nImport committed."))

    # ----- helpers ----------------------------------------------------------

    def _read_csv(self, path: Path) -> Iterator[tuple[int, dict[str, str]]]:
        """Yield ``(row_number, cleaned_dict)``. Row 1 is the header, data starts at 2."""
        if not path.exists():
            self.stdout.write(self.style.WARNING(f"  Skipping {path.name}: file not found"))
            return
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for line_no, raw in enumerate(csv.DictReader(fh), start=2):
                cleaned = {
                    (k or "").strip(): (v or "").strip()
                    for k, v in raw.items()
                    if k
                }
                yield line_no, cleaned

    def _require(self, row: dict[str, str], cols: list[str], file: str, line: int) -> None:
        missing = [c for c in cols if not row.get(c)]
        if missing:
            raise CommandError(f"{file}:{line} missing required column(s): {', '.join(missing)}")

    def _defaults_from(self, row: dict[str, str], cols: list[str]) -> dict[str, Any]:
        """Build a defaults dict, skipping empty cells so update_or_create doesn't blank existing fields."""
        return {c: row[c] for c in cols if row.get(c)}

    def _warn_skip(self, file: str, line: int, reason: str) -> None:
        """Log that a row was skipped due to a missing FK target. Doesn't abort."""
        self.stderr.write(self.style.WARNING(f"  {file}:{line} skipped — {reason}"))

    def _print_counts(self, label: str, created: int, updated: int, skipped: int) -> None:
        msg = f"  {label} {created} created, {updated} updated"
        if skipped:
            msg += f", {skipped} skipped"
        self.stdout.write(msg)

    # ----- per-model imports ------------------------------------------------

    def _import_users(self, path: Path) -> None:
        created = updated = 0
        for line, row in self._read_csv(path):
            self._require(row, ["hubspot_id", "email"], path.name, line)

            defaults = self._defaults_from(row, ["email", "first_name", "last_name"])
            if role := row.get("role"):
                if role not in Role.values:
                    raise CommandError(
                        f"{path.name}:{line} unknown role {role!r}. "
                        f"Expected one of: {', '.join(Role.values)}"
                    )
                defaults["role"] = role

            user, was_created = User.objects.update_or_create(
                hubspot_id=row["hubspot_id"],
                defaults=defaults,
            )
            if was_created:
                user.set_unusable_password()
                user.save(update_fields=["password"])
                created += 1
            else:
                updated += 1

        self._print_counts("users:        ", created, updated, 0)

    def _import_organisations(self, path: Path) -> None:
        created = updated = 0
        for line, row in self._read_csv(path):
            self._require(row, ["hubspot_id", "name"], path.name, line)
            defaults = {"name": row["name"]}
            # Optional columns — picked up only if present + non-empty in the CSV
            # (re-imports won't blank an existing value with a missing column).
            for col in ("legal_name", "trading_name", "companies_house_number"):
                if row.get(col):
                    defaults[col] = row[col].strip()
            _, was_created = Organisation.objects.update_or_create(
                hubspot_id=row["hubspot_id"],
                defaults=defaults,
            )
            created += int(was_created)
            updated += int(not was_created)
        self._print_counts("organisations:", created, updated, 0)

    def _import_contacts(self, path: Path) -> None:
        created = updated = skipped = 0
        for line, row in self._read_csv(path):
            self._require(row, ["hubspot_id", "organisation_hubspot_id"], path.name, line)

            org_hsid = row["organisation_hubspot_id"]
            try:
                org = Organisation.objects.get(hubspot_id=org_hsid)
            except Organisation.DoesNotExist:
                self._warn_skip(path.name, line, f"no organisation with hubspot_id={org_hsid!r}")
                skipped += 1
                continue

            defaults = self._defaults_from(row, ["first_name", "last_name", "email", "phone"])

            contact, was_created = Contact.objects.update_or_create(
                hubspot_id=row["hubspot_id"],
                defaults=defaults,
            )
            # Contact↔Organisation is a M2M; add() is idempotent on re-imports.
            contact.organisations.add(org)
            created += int(was_created)
            updated += int(not was_created)
        self._print_counts("contacts:     ", created, updated, skipped)

    def _import_deals(self, path: Path) -> None:
        created = updated = skipped = 0
        for line, row in self._read_csv(path):
            self._require(
                row,
                ["hubspot_id", "name", "owner_hubspot_id", "customer_hubspot_id"],
                path.name,
                line,
            )

            owner_hsid = row["owner_hubspot_id"]
            try:
                owner = User.objects.get(hubspot_id=owner_hsid)
            except User.DoesNotExist:
                self._warn_skip(path.name, line, f"no user with hubspot_id={owner_hsid!r}")
                skipped += 1
                continue

            customer_hsid = row["customer_hubspot_id"]
            try:
                customer = Contact.objects.get(hubspot_id=customer_hsid)
            except Contact.DoesNotExist:
                self._warn_skip(path.name, line, f"no contact with hubspot_id={customer_hsid!r}")
                skipped += 1
                continue

            # Seed Deal.organisation from the customer's first known organisation,
            # if any. Staff can change it later from the deal form.
            customer_org = customer.organisations.first()
            _, was_created = Deal.objects.update_or_create(
                hubspot_id=row["hubspot_id"],
                defaults={
                    "name": row["name"],
                    "owner": owner,
                    "customer": customer,
                    "organisation": customer_org,
                },
            )
            created += int(was_created)
            updated += int(not was_created)
        self._print_counts("deals:        ", created, updated, skipped)
