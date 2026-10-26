"""Point the django.contrib.sites Site row at this environment's hostname.

Django's built-in password-reset view (django.contrib.auth.urls, wired in
medifinance/urls.py) builds the link in the email from get_current_site(),
i.e. the Site row for settings.SITE_ID. A freshly migrated database seeds
that row as "example.com", so reset links are dead until it's fixed.

Idempotent. Run once per environment after the first migrate (and again if the
hostname changes):

    python manage.py set_site_domain                      # first DJANGO_ALLOWED_HOSTS entry
    python manage.py set_site_domain --domain app.medifinance.co.uk
"""

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand, CommandError

SITE_NAME = "Medifinance"


class Command(BaseCommand):
    help = "Set the Site row's domain to this environment's hostname (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--domain",
            help="Hostname to store. Defaults to the first entry of DJANGO_ALLOWED_HOSTS.",
        )

    def handle(self, *args, **options):
        domain = (options.get("domain") or "").strip()
        if not domain:
            hosts = [h for h in settings.ALLOWED_HOSTS if h and h != "*" and not h.startswith(".")]
            if not hosts:
                raise CommandError("No usable DJANGO_ALLOWED_HOSTS entry; pass --domain.")
            domain = hosts[0]
        if "/" in domain or "://" in domain:
            raise CommandError(f"--domain must be a bare hostname, got {domain!r}.")

        site, created = Site.objects.update_or_create(
            pk=settings.SITE_ID, defaults={"domain": domain, "name": SITE_NAME}
        )
        Site.objects.clear_cache()
        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} Site {site.pk}: {site.domain} ({site.name})"))
