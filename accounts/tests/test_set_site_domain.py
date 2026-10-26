from io import StringIO

from django.contrib.sites.models import Site
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings


class SetSiteDomainTests(TestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("set_site_domain", *args, stdout=out)
        return out.getvalue()

    def test_updates_the_default_example_com_row(self):
        self.assertEqual(Site.objects.get(pk=1).domain, "example.com")
        out = self._run("--domain", "app.medifinance.co.uk")
        site = Site.objects.get(pk=1)
        self.assertEqual((site.domain, site.name), ("app.medifinance.co.uk", "Medifinance"))
        self.assertIn("Updated", out)

    def test_creates_the_row_when_missing(self):
        Site.objects.all().delete()
        out = self._run("--domain", "app.medifinance.co.uk")
        self.assertEqual(Site.objects.get(pk=1).domain, "app.medifinance.co.uk")
        self.assertIn("Created", out)

    def test_is_idempotent(self):
        self._run("--domain", "app.medifinance.co.uk")
        self._run("--domain", "app.medifinance.co.uk")
        self.assertEqual(Site.objects.count(), 1)

    @override_settings(ALLOWED_HOSTS=["app-dev.medifinance.co.uk", "localhost"])
    def test_defaults_to_first_allowed_host(self):
        self._run()
        self.assertEqual(Site.objects.get(pk=1).domain, "app-dev.medifinance.co.uk")

    @override_settings(ALLOWED_HOSTS=["*"])
    def test_refuses_without_a_usable_host(self):
        with self.assertRaises(CommandError):
            self._run()

    def test_rejects_a_url(self):
        with self.assertRaises(CommandError):
            self._run("--domain", "https://app.medifinance.co.uk")
