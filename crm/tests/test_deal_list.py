"""Deal list: last-activity annotation/labels, sortable columns, and
stage/type/owner filters — and that they all compose with search + pagination."""

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from crm.models import Deal, Note, Stage

from .factories import (
    make_associate,
    make_deal,
    make_document,
    make_participation,
    make_proposal,
    make_quote,
)


def _names(response):
    return [d.name for d in response.context["object_list"]]


class DealListTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()

    def setUp(self):
        self.client.force_login(self.staff)

    def get(self, **params):
        return self.client.get(reverse("crm:deal_list"), params)


class LastActivityTests(DealListTestCase):
    def test_fresh_deal_shows_initial_stage(self):
        # The post_save signal seeds an Application stage on every new deal.
        deal = make_deal(owner=self.staff)
        row = self.get().context["object_list"][0]
        self.assertEqual(row.pk, deal.pk)
        self.assertEqual(row.last_activity_label, "Stage: Application")
        self.assertEqual(row.last_activity_at, row.last_stage_at)

    def test_falls_back_to_deal_created(self):
        deal = make_deal(owner=self.staff)
        deal.stage_events.all().delete()
        row = self.get().context["object_list"][0]
        self.assertEqual(row.last_activity_label, "Deal created")
        self.assertEqual(row.last_activity_at, deal.created_at)

    def test_latest_source_wins(self):
        deal = make_deal(owner=self.staff)
        Note.objects.create(
            deal=deal,
            type=Note.Type.ADMIN_COMMENT,
            content="chased the customer",
            datetime=timezone.now() + timedelta(hours=1),
        )
        row = self.get().context["object_list"][0]
        self.assertEqual(row.last_activity_label, "Note added")
        self.assertEqual(row.last_activity_at, row.last_note_at)

        Stage.objects.create(
            deal=deal,
            name=Stage.Name.DEAL_LIVE,
            occurred_at=timezone.now() + timedelta(hours=2),
        )
        row = self.get().context["object_list"][0]
        self.assertEqual(row.last_activity_label, "Stage: Deal Live")

    def test_requested_document_does_not_count(self):
        deal = make_deal(owner=self.staff)
        doc = make_document(deal)  # status=REQUESTED, uploaded_at=None
        row = self.get().context["object_list"][0]
        self.assertIsNone(row.last_document_at)
        self.assertNotEqual(row.last_activity_label, "Document uploaded")

        doc.status = doc.Status.PROVIDED
        doc.uploaded_at = timezone.now() + timedelta(hours=1)
        doc.save()
        row = self.get().context["object_list"][0]
        self.assertEqual(row.last_activity_label, "Document uploaded")
        self.assertEqual(row.last_activity_at, doc.uploaded_at)


class SortTests(DealListTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.alpha = make_deal(owner=cls.staff, name="Alpha")
        cls.beta = make_deal(owner=cls.staff, name="Beta")
        make_participation(cls.beta, amount="5000.00")

    def test_default_is_newest_created_first(self):
        self.assertEqual(_names(self.get()), ["Beta", "Alpha"])

    def test_sort_by_name_both_directions(self):
        self.assertEqual(_names(self.get(sort="name")), ["Alpha", "Beta"])
        self.assertEqual(_names(self.get(sort="-name")), ["Beta", "Alpha"])

    def test_sort_by_funded_puts_unfunded_last_both_directions(self):
        self.assertEqual(_names(self.get(sort="funded")), ["Beta", "Alpha"])
        self.assertEqual(_names(self.get(sort="-funded")), ["Beta", "Alpha"])

    def test_sort_by_activity(self):
        Note.objects.create(
            deal=self.alpha,
            type=Note.Type.ADMIN_COMMENT,
            content="ping",
            datetime=timezone.now() + timedelta(hours=1),
        )
        self.assertEqual(_names(self.get(sort="-activity")), ["Alpha", "Beta"])
        self.assertEqual(_names(self.get(sort="activity")), ["Beta", "Alpha"])

    def test_sort_by_type_puts_untyped_last_both_directions(self):
        Deal.objects.filter(pk=self.beta.pk).update(type=Deal.Type.ASSET_FINANCE)
        self.assertEqual(_names(self.get(sort="type")), ["Beta", "Alpha"])
        self.assertEqual(_names(self.get(sort="-type")), ["Beta", "Alpha"])

    def test_unknown_sort_falls_back_to_default(self):
        response = self.get(sort="bogus")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_names(response), ["Beta", "Alpha"])


class FilterTests(DealListTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_staff = make_associate()
        cls.mine = make_deal(owner=cls.staff, name="Mine")
        cls.theirs = make_deal(owner=cls.other_staff, name="Theirs")
        Stage.objects.create(deal=cls.mine, name=Stage.Name.DEAL_LIVE)

    def test_stage_filter(self):
        self.assertEqual(_names(self.get(stage="deal_live")), ["Mine"])
        self.assertEqual(_names(self.get(stage="application")), ["Theirs"])

    def test_invalid_stage_ignored(self):
        self.assertEqual(len(_names(self.get(stage="garbage"))), 2)

    def test_owner_filter(self):
        self.assertEqual(_names(self.get(owner=self.other_staff.pk)), ["Theirs"])
        self.assertEqual(_names(self.get(owner="me")), ["Mine"])

    def test_unowned_filter(self):
        unowned = make_deal(name="Unowned")
        unowned.owner = None
        unowned.save()
        self.assertEqual(_names(self.get(owner="none")), ["Unowned"])

    def test_search_filter_and_sort_compose(self):
        make_deal(owner=self.staff, name="Mine too")
        response = self.get(q="Mine", stage="application", sort="name")
        self.assertEqual(_names(response), ["Mine too"])
        # Header links keep the search/filter params.
        self.assertContains(response, "stage=application")
        self.assertContains(response, "q=Mine")


class TypeFilterTests(DealListTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.asset = make_deal(owner=cls.staff, name="Asset", type=Deal.Type.ASSET_FINANCE)
        cls.commercial = make_deal(owner=cls.staff, name="Commercial", type=Deal.Type.COMMERCIAL_FINANCE)
        cls.null_type = make_deal(owner=cls.staff, name="Null")
        cls.blank_type = make_deal(owner=cls.staff, name="Blank", type="")

    def test_type_filter(self):
        self.assertEqual(_names(self.get(type="asset_finance")), ["Asset"])
        self.assertEqual(_names(self.get(type="commercial_finance")), ["Commercial"])

    def test_none_matches_null_and_blank(self):
        self.assertEqual(_names(self.get(type="none")), ["Blank", "Null"])

    def test_source_filter(self):
        Deal.objects.filter(pk=self.asset.pk).update(source=Deal.Source.INTRODUCER)
        Deal.objects.filter(pk=self.blank_type.pk).update(source="")
        self.assertEqual(_names(self.get(source="introducer")), ["Asset"])
        self.assertEqual(sorted(_names(self.get(source="none"))), ["Blank", "Commercial", "Null"])
        self.assertEqual(sorted(_names(self.get(source="bogus"))), ["Asset", "Blank", "Commercial", "Null"])
        # Composes with the type filter.
        self.assertEqual(_names(self.get(type="asset_finance", source="introducer")), ["Asset"])
        self.assertEqual(_names(self.get(type="commercial_finance", source="introducer")), [])

    def test_unknown_type_ignored(self):
        self.assertEqual(len(_names(self.get(type="garbage"))), 4)

    def test_composes_with_search_and_owner(self):
        make_deal(name="Asset too", type=Deal.Type.ASSET_FINANCE)
        response = self.get(q="Asset", type="asset_finance", owner="me")
        self.assertEqual(_names(response), ["Asset"])
        # Header links keep the filter params.
        self.assertContains(response, "type=asset_finance")

    def test_type_column_shows_display_label(self):
        self.assertContains(self.get(), "Commercial Finance")


class QueryCountTests(DealListTestCase):
    def test_no_per_row_queries(self):
        for _ in range(3):
            deal = make_deal(owner=self.staff)
            make_quote(deal)
            make_proposal(deal)
            make_participation(deal)

        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.get().status_code, 200)
            return len(ctx)

        baseline = count_queries()
        for _ in range(5):
            deal = make_deal(owner=self.staff)
            make_quote(deal)
            make_participation(deal)
        self.assertEqual(count_queries(), baseline)
