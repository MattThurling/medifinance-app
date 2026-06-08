"""Load BNP's rate sheet into RateBand rows.

Idempotent: keyed on (organisation, term_months, min_amount, max_amount), so
re-running refreshes the yield + reactivates rather than duplicating. The
band's effective_from is only set on first create (preserved on update).

Run locally:   python manage.py load_bnp_rates
Run on dev:    as a Cloud Run job using the deployed image (see DEPLOYMENT.md).
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from crm.models import Organisation, RateBand

ORG_NAME = "BNP"

TERMS = [12, 24, 36, 48, 60, 72, 84]

# (min_amount, max_amount, [yield per term, aligned with TERMS])
BANDS = [
    (1000, 14999, [Decimal("15.65"), Decimal("14.65"), Decimal("8.15"), Decimal("8.15"), Decimal("8.15"), Decimal("8.15"), Decimal("8.15")]),
    (15000, 49999, [Decimal("15.50"), Decimal("14.50"), Decimal("7.75"), Decimal("7.75"), Decimal("7.75"), Decimal("7.75"), Decimal("7.75")]),
    (50000, 250000, [Decimal("15.50"), Decimal("14.50"), Decimal("7.75"), Decimal("7.75"), Decimal("7.75"), Decimal("7.75"), Decimal("7.75")]),
]


class Command(BaseCommand):
    help = "Load BNP's rate bands (idempotent)."

    def handle(self, *args, **options):
        matches = list(Organisation.objects.filter(name__iexact=ORG_NAME))
        if not matches:
            near = list(
                Organisation.objects.filter(name__icontains="BNP").values_list("name", flat=True)[:10]
            )
            hint = f" Did you mean one of: {near}?" if near else ""
            raise CommandError(f"No organisation named {ORG_NAME!r}.{hint}")
        if len(matches) > 1:
            raise CommandError(
                f"{len(matches)} organisations named {ORG_NAME!r} "
                f"(pks {[o.pk for o in matches]}). Resolve the duplicate first."
            )
        org = matches[0]

        created = updated = 0
        for min_amount, max_amount, yields in BANDS:
            for term, y in zip(TERMS, yields):
                _, was_created = RateBand.objects.update_or_create(
                    organisation=org,
                    term_months=term,
                    min_amount=min_amount,
                    max_amount=max_amount,
                    defaults={"yield_percent": y, "is_active": True},
                )
                created += int(was_created)
                updated += int(not was_created)

        self.stdout.write(
            self.style.SUCCESS(
                f"BNP rates loaded for org pk={org.pk} ({org.name}): "
                f"{created} created, {updated} updated, "
                f"{RateBand.objects.filter(organisation=org).count()} total bands."
            )
        )
