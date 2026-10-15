"""Associates with no organisation see only their own deals.

(Associates linked to an organisation share deals with their colleagues —
see test_organisation_scoping.)

Admins see everything (including unowned deals). Contacts and organisations
are a shared address book — every staff member can list, search and edit
them — but the deals shown on their detail pages are scoped. The owner
field is admin-only: associates have it removed from the deal, contact and
organisation forms, and new records they create are owned by them.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from crm.access import visible_deals
from crm.models import Contact, Deal, Note, Organisation, Quote

from .factories import (
    make_admin,
    make_associate,
    make_contact,
    make_customer,
    make_deal,
    make_document,
    make_organisation,
    make_participation,
    make_proposal,
    make_quote,
    make_signature_request,
)


class ScopingTestCase(TestCase):
    """`own` belongs to the logged-in associate, `theirs` to a colleague,
    `unowned` to nobody (as the public API creates them)."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = make_admin()
        cls.associate = make_associate()
        cls.other = make_associate()
        cls.customer = make_customer()
        cls.own = make_deal(owner=cls.associate, name="Own deal")
        cls.theirs = make_deal(owner=cls.other, name="Their deal")
        cls.unowned = make_deal(name="Unowned deal")
        Deal.objects.filter(pk=cls.unowned.pk).update(owner=None)
        cls.unowned.refresh_from_db()

    def setUp(self):
        self.client.force_login(self.associate)


class VisibleDealsHelperTests(ScopingTestCase):
    def test_admin_sees_all_including_unowned(self):
        self.assertCountEqual(visible_deals(self.admin), [self.own, self.theirs, self.unowned])

    def test_associate_sees_only_own(self):
        self.assertCountEqual(visible_deals(self.associate), [self.own])

    def test_customer_sees_deals_they_are_customer_on(self):
        self.assertEqual(list(visible_deals(self.customer)), [])
        contact = self.own.customer
        contact.user = self.customer
        contact.save(update_fields=["user"])
        self.assertEqual(list(visible_deals(self.customer)), [self.own])

    def test_anonymous_sees_nothing(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertEqual(list(visible_deals(AnonymousUser())), [])


class DealListTests(ScopingTestCase):
    def _names(self, **params):
        response = self.client.get(reverse("crm:deal_list"), params)
        self.assertEqual(response.status_code, 200)
        return response, [d.name for d in response.context["object_list"]]

    def test_list_shows_only_own_deals(self):
        _, names = self._names()
        self.assertEqual(names, ["Own deal"])

    def test_owner_filter_is_ignored_for_associates(self):
        _, names = self._names(owner=str(self.other.pk))
        self.assertEqual(names, ["Own deal"])
        _, names = self._names(owner="none")
        self.assertEqual(names, ["Own deal"])

    def test_owner_dropdown_hidden_and_choices_empty(self):
        response, _ = self._names()
        self.assertTrue(response.context["owner_filter_locked"])
        self.assertEqual(list(response.context["owner_choices"]), [])
        self.assertNotContains(response, 'name="owner"')
        self.assertNotContains(response, self.other.full_name)

    def test_search_still_applies_within_own_deals(self):
        _, names = self._names(q="deal")
        self.assertEqual(names, ["Own deal"])
        _, names = self._names(q="Their")
        self.assertEqual(names, [])

    def test_admin_sees_everything_and_keeps_owner_filter(self):
        self.client.force_login(self.admin)
        response, names = self._names()
        self.assertCountEqual(names, ["Own deal", "Their deal", "Unowned deal"])
        self.assertFalse(response.context["owner_filter_locked"])
        self.assertContains(response, 'name="owner"')
        _, names = self._names(owner="none")
        self.assertEqual(names, ["Unowned deal"])

    def test_maturing_list_scoped_and_dropdown_hidden(self):
        response = self.client.get(reverse("crm:deal_maturing"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["owner_filter_locked"])
        self.assertNotContains(response, 'name="owner"')


class DealPageTests(ScopingTestCase):
    def test_other_and_unowned_deals_404(self):
        for name in ("deal_detail", "deal_overview", "deal_update", "deal_delete"):
            for deal in (self.theirs, self.unowned):
                with self.subTest(view=name, deal=deal.name):
                    response = self.client.get(reverse(f"crm:{name}", args=[deal.pk]))
                    self.assertEqual(response.status_code, 404)

    def test_own_deal_pages_render(self):
        for name in ("deal_detail", "deal_overview", "deal_update"):
            with self.subTest(view=name):
                response = self.client.get(reverse(f"crm:{name}", args=[self.own.pk]))
                self.assertEqual(response.status_code, 200)

    def test_post_to_other_deal_changes_nothing(self):
        response = self.client.post(
            reverse("crm:deal_update", args=[self.theirs.pk]),
            {"name": "Hijacked", "type": "", "source": ""},
        )
        self.assertEqual(response.status_code, 404)
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.name, "Their deal")

        response = self.client.post(reverse("crm:deal_delete", args=[self.theirs.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Deal.objects.filter(pk=self.theirs.pk).exists())

    def test_admin_can_open_unowned_deal(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("crm:deal_detail", args=[self.unowned.pk]))
        self.assertEqual(response.status_code, 200)

    def test_applicant_add_and_commission_request_scoped(self):
        contact = make_contact(organisation=self.theirs.organisation)
        response = self.client.post(
            reverse("crm:deal_applicant_add", args=[self.theirs.pk]), {"contact": contact.pk},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.theirs.co_applicants.exists())

        response = self.client.post(
            reverse("crm:deal_request_commission_invoice", args=[self.theirs.pk]),
        )
        self.assertEqual(response.status_code, 404)

    def test_portal_link_views_scoped(self):
        for name in ("deal_issue_portal_link", "deal_email_portal_link"):
            with self.subTest(view=name):
                response = self.client.post(reverse(f"crm:{name}", args=[self.theirs.pk]))
                self.assertEqual(response.status_code, 404)

    def test_note_on_other_deal_404s(self):
        response = self.client.post(
            reverse("crm:note_create"), {"deal": self.theirs.pk, "content": "psst"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Note.objects.filter(deal=self.theirs).exists())

    def test_note_on_any_contact_or_org_allowed(self):
        # The address book is shared, so notes on it are too.
        response = self.client.post(
            reverse("crm:note_create"),
            {"contact": self.theirs.customer.pk, "content": "hello"},
        )
        self.assertRedirects(response, self.theirs.customer.get_absolute_url())
        self.assertTrue(Note.objects.filter(contact=self.theirs.customer).exists())


@override_settings(DOCUSEAL_URL="http://sign.test", DOCUSEAL_API_TOKEN="tok")
class ChildCreateTests(ScopingTestCase):
    CHILD_CREATE_VIEWS = (
        "quote_create", "proposal_create", "participation_create",
        "document_create", "stage_create",
    )

    def test_child_create_for_other_deal_404s(self):
        for name in self.CHILD_CREATE_VIEWS:
            with self.subTest(view=name):
                url = reverse(f"crm:{name}") + f"?deal={self.theirs.pk}"
                self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(
            self.client.get(reverse("crm:deal_sign", args=[self.theirs.pk])).status_code, 404,
        )

    def test_child_create_for_own_deal_renders(self):
        for name in self.CHILD_CREATE_VIEWS:
            with self.subTest(view=name):
                url = reverse(f"crm:{name}") + f"?deal={self.own.pk}"
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_missing_or_garbage_deal_param_404s(self):
        self.assertEqual(self.client.get(reverse("crm:quote_create")).status_code, 404)
        self.assertEqual(self.client.get(reverse("crm:quote_create") + "?deal=abc").status_code, 404)

    def test_role_check_still_runs_before_the_lookup(self):
        """Regression guard: the deal lookup moved from setup() into
        dispatch(), after the role check. Anonymous and customer users must
        still get 403, not 404, even for a deal that exists."""
        urls = [reverse(f"crm:{n}") + f"?deal={self.own.pk}" for n in self.CHILD_CREATE_VIEWS]
        urls.append(reverse("crm:deal_sign", args=[self.own.pk]))
        self.client.logout()
        for url in urls:
            with self.subTest(url=url, who="anonymous"):
                self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.customer)
        for url in urls:
            with self.subTest(url=url, who="customer"):
                self.assertEqual(self.client.get(url).status_code, 403)


@override_settings(DOCUSEAL_URL="http://sign.test", DOCUSEAL_API_TOKEN="tok")
class ChildObjectTests(ScopingTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.their_quote = make_quote(cls.theirs)
        cls.their_proposal = make_proposal(cls.theirs)
        cls.their_participation = make_participation(cls.theirs)
        cls.their_document = make_document(cls.theirs)
        cls.their_sr = make_signature_request(cls.theirs)
        cls.own_quote = make_quote(cls.own)
        cls.own_proposal = make_proposal(cls.own)
        cls.own_participation = make_participation(cls.own)
        cls.own_document = make_document(cls.own)

    def test_get_views_on_other_deals_children_404(self):
        cases = [
            ("quote_update", self.their_quote),
            ("quote_delete", self.their_quote),
            ("proposal_update", self.their_proposal),
            ("participation_update", self.their_participation),
            ("participation_invoice_download", self.their_participation),
        ]
        for name, obj in cases:
            with self.subTest(view=name):
                response = self.client.get(reverse(f"crm:{name}", args=[obj.pk]))
                self.assertEqual(response.status_code, 404)

    def test_post_views_on_other_deals_children_404_and_change_nothing(self):
        cases = [
            ("quote_select", self.their_quote),
            ("quote_delete", self.their_quote),
            ("proposal_select", self.their_proposal),
            ("proposal_notify", self.their_proposal),
            ("participation_delete", self.their_participation),
            ("participation_request_invoice", self.their_participation),
            ("document_delete", self.their_document),
            ("signature_void", self.their_sr),
            ("signature_resend", self.their_sr),
        ]
        for name, obj in cases:
            with self.subTest(view=name):
                response = self.client.post(reverse(f"crm:{name}", args=[obj.pk]))
                self.assertEqual(response.status_code, 404)
        self.theirs.refresh_from_db()
        self.assertIsNone(self.theirs.selected_quote_id)
        self.assertIsNone(self.theirs.selected_proposal_id)
        self.assertTrue(Quote.objects.filter(pk=self.their_quote.pk).exists())
        self.assertTrue(self.theirs.participations.filter(pk=self.their_participation.pk).exists())
        self.assertTrue(self.theirs.documents.filter(pk=self.their_document.pk).exists())

    def test_own_children_are_reachable(self):
        for name, obj in [
            ("quote_update", self.own_quote),
            ("proposal_update", self.own_proposal),
            ("participation_update", self.own_participation),
        ]:
            with self.subTest(view=name):
                response = self.client.get(reverse(f"crm:{name}", args=[obj.pk]))
                self.assertEqual(response.status_code, 200)
        response = self.client.post(reverse("crm:quote_select", args=[self.own_quote.pk]))
        self.assertRedirects(response, self.own.get_absolute_url())
        self.own.refresh_from_db()
        self.assertEqual(self.own.selected_quote_id, self.own_quote.pk)

    def test_rate_options_ignore_other_deals_amount(self):
        """The rate-options endpoint narrows bands by the deal's funded
        amount. For a deal the associate can't see it must behave as if no
        deal was given (no amount filter), never read the other deal's figures."""
        from .factories import make_rate_band
        # Both fixture deals have 10,000 funded (one participation each), so
        # a 20k–30k band is outside their amount.
        band = make_rate_band(term_months=60, min_amount=20_000, max_amount=30_000)
        url = reverse("crm:quote_rate_options")

        own_resp = self.client.get(url, {"term": "60", "deal": self.own.pk})
        self.assertNotIn(band, own_resp.context["rates"])  # amount filter applied

        their_resp = self.client.get(url, {"term": "60", "deal": self.theirs.pk})
        self.assertIn(band, their_resp.context["rates"])  # as if no deal was given

    def test_document_download_and_upload_scoped(self):
        self.their_document.attach(SimpleUploadedFile("a.pdf", b"%PDF-A"), by=self.other)
        download = reverse("crm:document_download", args=[self.their_document.pk])
        upload = reverse("crm:document_upload", args=[self.own_document.pk])
        self.assertEqual(self.client.get(download).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("crm:document_upload", args=[self.their_document.pk]),
                {"file": SimpleUploadedFile("b.pdf", b"%PDF-B")},
            ).status_code,
            403,
        )
        response = self.client.post(upload, {"file": SimpleUploadedFile("c.pdf", b"%PDF-C")})
        self.assertRedirects(response, self.own.get_absolute_url())
        self.own_document.refresh_from_db()
        self.assertTrue(self.own_document.file)

    def test_signature_downloads_scoped(self):
        self.their_sr.signed_file.save("s.pdf", SimpleUploadedFile("s.pdf", b"%PDF"), save=True)
        self.their_sr.audit_log_file.save("a.pdf", SimpleUploadedFile("a.pdf", b"%PDF"), save=True)
        for name in ("signature_signed_download", "signature_audit_download"):
            with self.subTest(view=name):
                response = self.client.get(reverse(f"crm:{name}", args=[self.their_sr.pk]))
                self.assertEqual(response.status_code, 403)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("crm:signature_signed_download", args=[self.their_sr.pk]))
        self.assertEqual(response.status_code, 200)


class OwnerFieldTests(ScopingTestCase):
    CREATE_VIEWS = ("deal_create", "contact_create", "organisation_create")

    def test_associate_forms_have_no_owner_field(self):
        for name in self.CREATE_VIEWS:
            with self.subTest(view=name):
                response = self.client.get(reverse(f"crm:{name}"))
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("owner", response.context["form"].fields)
                self.assertTrue(response.context["owner_locked"])
                self.assertNotContains(response, 'name="owner"')
        # Edit forms too.
        response = self.client.get(reverse("crm:deal_update", args=[self.own.pk]))
        self.assertNotIn("owner", response.context["form"].fields)
        self.assertContains(response, f"Owner: {self.associate.full_name}")

    def test_create_assigns_associate_even_if_owner_forged(self):
        response = self.client.post(reverse("crm:contact_create"), {
            "first_name": "Forged", "last_name": "Owner", "owner": self.other.pk,
        })
        contact = Contact.objects.get(last_name="Owner")
        self.assertRedirects(response, contact.get_absolute_url())
        self.assertEqual(contact.owner, self.associate)

        response = self.client.post(reverse("crm:organisation_create"), {
            "name": "Forged Org", "owner": self.other.pk,
        })
        org = Organisation.objects.get(name="Forged Org")
        self.assertRedirects(response, org.get_absolute_url())
        self.assertEqual(org.owner, self.associate)

        response = self.client.post(reverse("crm:deal_create"), {
            "name": "Forged Deal", "type": "", "source": "", "owner": self.other.pk,
        })
        deal = Deal.objects.get(name="Forged Deal")
        self.assertRedirects(response, deal.get_absolute_url())
        self.assertEqual(deal.owner, self.associate)
        # ... and it's now in their list.
        self.assertIn(deal, visible_deals(self.associate))

    def test_edit_cannot_reassign_owner(self):
        contact = make_contact(owner=self.other)
        response = self.client.post(
            reverse("crm:contact_update", args=[contact.pk]),
            {"first_name": "Still", "last_name": "Theirs", "owner": self.associate.pk},
        )
        contact.refresh_from_db()
        self.assertRedirects(response, contact.get_absolute_url())
        self.assertEqual(contact.first_name, "Still")  # the edit itself went through
        self.assertEqual(contact.owner, self.other)   # but the owner didn't move

        response = self.client.post(
            reverse("crm:deal_update", args=[self.own.pk]),
            {"name": "Renamed", "type": "", "source": "", "owner": self.other.pk},
        )
        self.own.refresh_from_db()
        self.assertRedirects(response, self.own.get_absolute_url())
        self.assertEqual(self.own.name, "Renamed")
        self.assertEqual(self.own.owner, self.associate)

    def test_admin_keeps_owner_field_limited_to_staff(self):
        self.client.force_login(self.admin)
        for name in self.CREATE_VIEWS:
            with self.subTest(view=name):
                response = self.client.get(reverse(f"crm:{name}"))
                form = response.context["form"]
                self.assertIn("owner", form.fields)
                self.assertFalse(response.context["owner_locked"])
                staff = list(form.fields["owner"].queryset)
                self.assertIn(self.associate, staff)
                self.assertIn(self.admin, staff)
                self.assertNotIn(self.customer, staff)
        # A customer pk is rejected outright.
        response = self.client.post(reverse("crm:contact_create"), {
            "first_name": "Bad", "last_name": "Owner", "owner": self.customer.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "owner", [
            "Select a valid choice. That choice is not one of the available choices."
        ])


class AddressBookTests(ScopingTestCase):
    def test_contact_and_org_lists_show_everyones_records(self):
        response = self.client.get(reverse("crm:contact_list"))
        self.assertIn(self.theirs.customer, response.context["object_list"])
        self.assertFalse(response.context["owner_filter_locked"])
        response = self.client.get(reverse("crm:organisation_list"))
        self.assertIn(self.theirs.organisation, response.context["object_list"])

    def test_contact_and_org_pages_editable_but_related_deals_scoped(self):
        # Put one of our deals and one of theirs on the same contact + org.
        contact = self.theirs.customer
        org = self.theirs.organisation
        shared = make_deal(owner=self.associate, customer=contact, organisation=org, name="Shared")

        response = self.client.get(contact.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual([d.name for d in response.context["deals"]], ["Shared"])
        self.assertEqual(self.client.get(reverse("crm:contact_update", args=[contact.pk])).status_code, 200)

        response = self.client.get(org.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual([d.name for d in response.context["deals"]], ["Shared"])

        self.client.force_login(self.admin)
        response = self.client.get(org.get_absolute_url())
        self.assertCountEqual([d.name for d in response.context["deals"]], ["Shared", "Their deal"])
        self.assertIsNotNone(shared.pk)

    def test_search_endpoints_unscoped(self):
        response = self.client.get(reverse("crm:contact_search"), {"q": self.theirs.customer.last_name})
        self.assertContains(response, self.theirs.customer.last_name)
        response = self.client.get(reverse("crm:organisation_search"), {"q": self.theirs.organisation.name})
        self.assertContains(response, self.theirs.organisation.name)

    def test_contact_create_from_other_deal_ignores_the_deal(self):
        # `?deal=` on contact-create attaches a co-applicant; for a deal you
        # can't see it silently falls back to a plain contact form.
        response = self.client.get(reverse("crm:contact_create") + f"?deal={self.theirs.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["parent_deal"])
        response = self.client.get(reverse("crm:contact_create") + f"?deal={self.own.pk}")
        self.assertEqual(response.context["parent_deal"], self.own)


class ReportsAndDashboardTests(ScopingTestCase):
    def test_reports_forced_to_own_numbers(self):
        response = self.client.get(reverse("crm:reports"), {"owner": str(self.other.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["params"].owner, "me")
        self.assertEqual(response.context["params"].user_id, self.associate.pk)
        self.assertEqual(response.context["owners"], [])
        self.assertTrue(response.context["owner_filter_locked"])
        self.assertNotContains(response, "By owner")
        self.assertNotContains(response, 'name="owner"')
        self.assertNotContains(response, "My stats")
        self.assertNotContains(response, self.other.full_name)
        # Headline counts only our deal (all three were created "now").
        self.assertEqual(response.context["headline"]["new_deals"]["value"], 1)

    def test_admin_reports_unchanged(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("crm:reports"))
        self.assertEqual(response.context["params"].owner, "")
        self.assertContains(response, "By owner")
        self.assertTrue(response.context["owners"])
        self.assertEqual(response.context["headline"]["new_deals"]["value"], 3)

    def test_dashboard_deal_count_is_own(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["deals_count"], 1)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["deals_count"], 3)


class StaffApiTests(ScopingTestCase):
    def test_list_and_detail_scoped(self):
        response = self.client.get(reverse("crm_api:deal_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([d["name"] for d in response.json()["deals"]], ["Own deal"])
        self.assertEqual(
            self.client.get(reverse("crm_api:deal_detail", args=[self.theirs.pk])).status_code, 404,
        )
        self.assertEqual(
            self.client.get(reverse("crm_api:deal_detail", args=[self.own.pk])).status_code, 200,
        )

    def test_admin_sees_all(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("crm_api:deal_list"))
        self.assertCountEqual(
            [d["name"] for d in response.json()["deals"]],
            ["Own deal", "Their deal", "Unowned deal"],
        )
        self.assertEqual(
            self.client.get(reverse("crm_api:deal_detail", args=[self.unowned.pk])).status_code, 200,
        )
