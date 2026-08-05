"""Contact list: sortable columns and the owner filter — and that they
compose with search."""

from django.test import TestCase
from django.urls import reverse

from .factories import make_associate, make_contact


def _names(response):
    return [c.last_name for c in response.context["object_list"]]


class ContactListTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()

    def setUp(self):
        self.client.force_login(self.staff)

    def get(self, **params):
        return self.client.get(reverse("crm:contact_list"), params)


class SortTests(ContactListTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.able = make_contact(last_name="Able", email="zed@example.com")
        cls.baker = make_contact(last_name="Baker", email="ann@example.com")

    def test_default_sorts_by_name(self):
        self.assertEqual(_names(self.get()), ["Able", "Baker"])

    def test_sort_by_name_both_directions(self):
        self.assertEqual(_names(self.get(sort="name")), ["Able", "Baker"])
        self.assertEqual(_names(self.get(sort="-name")), ["Baker", "Able"])

    def test_sort_by_email(self):
        self.assertEqual(_names(self.get(sort="email")), ["Baker", "Able"])

    def test_sort_by_owner_both_directions(self):
        # Unowned coalesces to "" (as on the deal list): first asc, last desc.
        self.able.owner = make_associate(first_name="Anna")
        self.able.save()
        self.assertEqual(_names(self.get(sort="owner")), ["Baker", "Able"])
        self.assertEqual(_names(self.get(sort="-owner")), ["Able", "Baker"])

    def test_unknown_sort_falls_back_to_default(self):
        response = self.get(sort="bogus")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_names(response), ["Able", "Baker"])


class FilterTests(ContactListTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_staff = make_associate()
        cls.mine = make_contact(last_name="Mine", owner=cls.staff)
        cls.theirs = make_contact(last_name="Theirs", owner=cls.other_staff)
        cls.unowned = make_contact(last_name="Unowned")

    def test_owner_filter(self):
        self.assertEqual(_names(self.get(owner=self.other_staff.pk)), ["Theirs"])
        self.assertEqual(_names(self.get(owner="me")), ["Mine"])

    def test_unowned_filter(self):
        self.assertEqual(_names(self.get(owner="none")), ["Unowned"])

    def test_invalid_owner_ignored(self):
        self.assertEqual(len(_names(self.get(owner="garbage"))), 3)

    def test_search_filter_and_sort_compose(self):
        make_contact(last_name="Mine too", owner=self.staff)
        response = self.get(q="Mine", owner="me", sort="-name")
        self.assertEqual(_names(response), ["Mine too", "Mine"])
        # Header links keep the search/filter params.
        self.assertContains(response, "owner=me")
        self.assertContains(response, "q=Mine")

    def test_owner_column_rendered(self):
        make_contact(last_name="Named", owner=make_associate(first_name="Nora", last_name="Field"))
        self.assertContains(self.get(), "Nora Field")
