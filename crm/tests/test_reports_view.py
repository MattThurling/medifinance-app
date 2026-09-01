"""The Reports page and the dashboard's stats block: context wiring, filter
parsing, chart payload embedding, and role branches. The numbers themselves
are covered in test_stats.py."""

from django.test import TestCase
from django.urls import reverse

from crm.models import Deal, Stage

from .factories import (
    make_admin,
    make_associate,
    make_customer_with_deal,
    make_deal,
    make_participation,
)


class ReportsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate(first_name="Rae", last_name="Reporter")
        cls.deal = make_deal(owner=cls.staff, source=Deal.Source.INTRODUCER)
        make_participation(cls.deal, amount="2500")
        Stage.objects.create(deal=cls.deal, name=Stage.Name.DEAL_LIVE)

    def setUp(self):
        self.client.force_login(self.staff)

    def get(self, **params):
        return self.client.get(reverse("crm:reports"), params)

    def test_renders_with_all_sections(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        ctx = response.context
        for key in ("headline", "pipeline", "series", "owners", "sources", "conversion", "velocity", "charts"):
            self.assertIn(key, ctx, key)
        self.assertEqual(ctx["params"].period, "12m")
        self.assertEqual(ctx["headline"]["funded"]["value"], 2500)
        # Chart payloads are embedded as json_script blocks for charts.js.
        for chart_id in ("chart-pipeline", "chart-series", "chart-sources", "chart-conversion"):
            self.assertContains(response, f'id="{chart_id}-data"')
        self.assertContains(response, "vendor/chart.umd.min.js")
        self.assertContains(response, "js/charts.js")

    def test_filters_are_parsed_and_reflected(self):
        response = self.get(period="30d", type="asset_finance", source="introducer", owner="me")
        p = response.context["params"]
        self.assertEqual((p.period, p.type, p.source, p.owner), ("30d", "asset_finance", "introducer", "me"))
        self.assertEqual(p.user_id, self.staff.pk)
        self.assertEqual(response.context["source_filter"], "introducer")
        self.assertEqual(response.context["type_filter"], "asset_finance")
        self.assertEqual(response.context["list_qs"], "&owner=me&type=asset_finance&source=introducer")
        self.assertContains(response, '<option value="30d" selected>')

    def test_bad_filters_fall_back(self):
        response = self.get(period="nope", type="nope", source="nope", owner="nope")
        p = response.context["params"]
        self.assertEqual((p.period, p.type, p.source, p.owner), ("12m", "", "", ""))
        self.assertEqual(response.context["list_qs"], "")

    def test_my_stats_highlights_current_user(self):
        response = self.get(owner="me")
        me = [o for o in response.context["owners"] if o["is_me"]]
        self.assertEqual(len(me), 1)
        self.assertEqual(me[0]["name"], "Rae Reporter")
        self.assertContains(response, "badge-primary")
        self.assertNotContains(response, "My stats")  # already filtered to me

    def test_owner_choices_come_from_mixin(self):
        self.assertIn(self.staff, list(self.get().context["owner_choices"]))


class DashboardStatsTests(TestCase):
    def test_staff_gets_stats_and_report_link(self):
        staff = make_associate()
        deal = make_deal(owner=staff)
        make_participation(deal, amount="100")
        Stage.objects.create(deal=deal, name=Stage.Name.DEAL_LIVE)
        self.client.force_login(staff)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        stats = response.context["stats"]
        self.assertEqual(stats["headline"]["live_count"]["value"], 1)
        self.assertEqual(stats["headline"]["funded"]["value"], 100)
        self.assertContains(response, reverse("crm:reports"))
        self.assertContains(response, 'id="chart-pipeline-data"')
        self.assertContains(response, 'id="chart-series-data"')
        self.assertContains(response, "vendor/chart.umd.min.js")
        # The record-count cards still link to the lists.
        self.assertContains(response, reverse("crm:contact_list"))
        self.assertEqual(response.context["deals_count"], 1)

    def test_admin_still_sees_api_toggle(self):
        self.client.force_login(make_admin())
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, reverse("toggle_api_access"))
        self.assertIn("stats", response.context)

    def test_customer_dashboard_is_unchanged(self):
        user, deal = make_customer_with_deal()
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("stats", response.context)
        self.assertEqual(list(response.context["customer_deals"]), [deal])
        self.assertNotContains(response, "vendor/chart.umd.min.js")
        self.assertNotContains(response, reverse("crm:reports"))
