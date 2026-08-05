"""The maturing-deals radar: `Deal.maturity_date` derivation and the
/deals/maturing/ worklist of live deals whose finance term ends soon."""

from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.models import Stage
from crm.pricing import add_months

from .factories import make_associate, make_deal, make_quote


def _names(response):
    return [d.name for d in response.context["object_list"]]


def _make_live(deal):
    Stage.objects.create(deal=deal, name=Stage.Name.DEAL_LIVE)
    return deal


def _live_deal(*, name, term_end_date=None, owner=None):
    return _make_live(make_deal(name=name, owner=owner, term_end_date=term_end_date))


class MaturityDateTests(TestCase):
    def test_explicit_term_end_date_wins(self):
        deal = make_deal(
            term_end_date=date(2030, 1, 15),
            first_payment_date=date(2026, 1, 1),
        )
        deal.selected_quote = make_quote(deal, term=12)
        self.assertEqual(deal.maturity_date, date(2030, 1, 15))

    def test_derived_from_first_payment_and_quote_term(self):
        deal = make_deal(first_payment_date=date(2026, 1, 31))
        deal.selected_quote = make_quote(deal, term=13)
        deal.save()
        # 12 months after the first payment, clamped to month end.
        self.assertEqual(deal.maturity_date, date(2027, 1, 31))

    def test_none_without_either_source(self):
        deal = make_deal()
        self.assertIsNone(deal.maturity_date)
        deal.first_payment_date = date(2026, 1, 1)  # no quote → still None
        self.assertIsNone(deal.maturity_date)


class MaturingListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.today = timezone.localdate()

    def setUp(self):
        self.client.force_login(self.staff)

    def get(self, **params):
        return self.client.get(reverse("crm:deal_maturing"), params)

    def test_includes_only_live_deals_inside_window(self):
        _live_deal(name="Soon", term_end_date=self.today + timedelta(days=60), owner=self.staff)
        _live_deal(name="Far", term_end_date=add_months(self.today, 30), owner=self.staff)
        make_deal(name="Not live", owner=self.staff,
                  term_end_date=self.today + timedelta(days=60))  # stays at Application
        _live_deal(name="No data", owner=self.staff)

        response = self.get()  # default window: 12 months
        self.assertEqual(_names(response), ["Soon"])
        self.assertEqual(response.context["no_maturity_count"], 1)

    def test_sorted_soonest_first_with_overdue_on_top(self):
        _live_deal(name="Overdue", term_end_date=self.today - timedelta(days=30), owner=self.staff)
        _live_deal(name="Next month", term_end_date=self.today + timedelta(days=30), owner=self.staff)
        _live_deal(name="Next week", term_end_date=self.today + timedelta(days=7), owner=self.staff)
        self.assertEqual(_names(self.get()), ["Overdue", "Next week", "Next month"])
        self.assertContains(self.get(), "Overdue</span>")

    def test_window_param(self):
        _live_deal(name="Month 2", term_end_date=add_months(self.today, 2), owner=self.staff)
        _live_deal(name="Month 9", term_end_date=add_months(self.today, 9), owner=self.staff)
        self.assertEqual(_names(self.get(window="3")), ["Month 2"])
        self.assertEqual(_names(self.get(window="12")), ["Month 2", "Month 9"])
        # Unknown values fall back to the 12-month default.
        self.assertEqual(_names(self.get(window="7")), ["Month 2", "Month 9"])
        self.assertEqual(_names(self.get(window="soon")), ["Month 2", "Month 9"])

    def test_composes_with_owner_filter(self):
        other = make_associate()
        _live_deal(name="Mine", term_end_date=self.today + timedelta(days=30), owner=self.staff)
        _live_deal(name="Theirs", term_end_date=self.today + timedelta(days=30), owner=other)
        self.assertEqual(_names(self.get(owner="me")), ["Mine"])
        self.assertEqual(_names(self.get(owner=other.pk)), ["Theirs"])

    def test_derived_maturity_appears_without_explicit_date(self):
        deal = make_deal(name="Quoted", owner=self.staff,
                         first_payment_date=self.today - timedelta(days=30))
        deal.selected_quote = make_quote(deal, term=6)
        deal.save()
        _make_live(deal)
        self.assertEqual(_names(self.get(window="6")), ["Quoted"])


class TermEndDateFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()

    def setUp(self):
        self.client.force_login(self.staff)

    def test_round_trips_through_deal_form(self):
        deal = make_deal(owner=self.staff)
        response = self.client.post(
            reverse("crm:deal_update", args=[deal.pk]),
            {"name": deal.name, "owner": self.staff.pk, "term_end_date": "2031-06-28"},
        )
        deal.refresh_from_db()
        self.assertRedirects(response, deal.get_absolute_url())
        self.assertEqual(deal.term_end_date, date(2031, 6, 28))

    def test_detail_shows_term_end(self):
        deal = make_deal(owner=self.staff, term_end_date=date(2031, 6, 28))
        self.assertContains(self.client.get(deal.get_absolute_url()), "28 Jun 2031")
