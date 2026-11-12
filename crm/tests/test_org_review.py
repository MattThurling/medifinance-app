"""Customer-provided organisation details and their staff review
(crm.org_review): creating the company from the portal, the hybrid edit
rules, the staff review actions, duplicate swapping and the signature block."""

from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from crm import org_review
from crm.forms import CustomerCompanyCreateForm, DealForm
from crm.models import Deal, Note, Organisation, OrganisationChange, SignatureRequest
from crm.portal_checklist import outstanding_items

from .factories import (
    make_admin,
    make_associate,
    make_co_applicant,
    make_contact,
    make_customer,
    make_customer_with_deal,
    make_deal,
    make_organisation,
    make_participation,
    make_signature_request,
    make_unverified_organisation,
)

HTMX = {"HTTP_HX_REQUEST": "true"}


def create_data(**overrides):
    data = {
        "name": "Bright Smiles Ltd",
        "trading_name": "",
        "companies_house_number": "1234567",
        "address_line1": "1 High Street",
        "address_line2": "",
        "address_city": "Bristol",
        "address_county": "",
        "address_postcode": "BS1 1AA",
        "phone": "0117 496 0000",
        "email": "",
        "url": "",
    }
    data.update(overrides)
    return {f"company-{k}": v for k, v in data.items()}


def edit_data(org, **overrides):
    p = f"org{org.pk}"
    names = org_review.CUSTOMER_FIELDS
    data = {f"{p}-{name}": getattr(org, name) for name in names}
    data[f"{p}-expected_updated_at"] = org.updated_at.isoformat()
    data.update({f"{p}-{k}": v for k, v in overrides.items()})
    return data


def deal_without_organisation(**deal_extra):
    """A partner-API style application: a customer and a deal, no company."""
    user = make_customer()
    contact = make_contact(user=user)
    deal = make_deal(customer=contact, **deal_extra)
    Deal.objects.filter(pk=deal.pk).update(organisation=None)
    deal.refresh_from_db()
    return user, deal


class CreateCompanyTests(TestCase):
    def setUp(self):
        self.user, self.deal = deal_without_organisation(
            company_type=Deal.CompanyType.LIMITED_COMPANY,
        )
        self.url = reverse("crm:my_deal_company_add", args=[self.deal.pk])
        self.page = reverse("crm:my_deal_detail", args=[self.deal.pk])
        self.client.force_login(self.user)

    def test_page_offers_the_form_and_the_checklist_asks_for_it(self):
        page = self.client.get(self.page)
        self.assertContains(page, self.url)
        self.assertContains(page, "Tell us about your company")
        self.assertContains(page, 'href="#company"')

    def test_create_links_the_deal_and_flags_the_organisation_unverified(self):
        response = self.client.post(self.url, create_data(), **HTMX)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Refresh"], "true")

        self.deal.refresh_from_db()
        org = self.deal.organisation
        self.assertEqual(org.name, "Bright Smiles Ltd")
        self.assertEqual(org.companies_house_number, "01234567")  # zero-padded
        self.assertEqual(org.submitted_by, self.user)
        self.assertEqual(org.owner, self.deal.owner)
        self.assertTrue(org.is_unverified)
        self.assertIn(org, self.deal.customer.organisations.all())

        note = Note.objects.get(type=Note.Type.CUSTOMER_UPDATE)
        self.assertEqual((note.deal, note.organisation, note.author), (self.deal, org, self.user))

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, settings.NOTIFY_EMAILS)
        self.assertIn("Bright Smiles Ltd", mail.outbox[0].subject)
        self.assertNotIn("1 High Street", mail.outbox[0].body)  # labels, not values

        page = self.client.get(self.page)
        self.assertContains(page, "Bright Smiles Ltd")
        self.assertContains(page, "Being checked")
        self.assertNotContains(page, "Tell us about your company")
        # With a company in place the customer can add co-applicants.
        self.assertContains(page, reverse("crm:my_deal_applicant_add", args=[self.deal.pk]))

    def test_second_submit_does_not_create_a_second_organisation(self):
        self.client.post(self.url, create_data(), **HTMX)
        self.client.post(self.url, create_data(name="Other Name Ltd"), **HTMX)
        self.assertEqual(Organisation.objects.filter(submitted_by=self.user).count(), 1)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.organisation.name, "Bright Smiles Ltd")
        with self.assertRaises(org_review.AlreadyHasOrganisation):
            org_review.create_customer_organisation(self.deal, {"name": "X"}, by_user=self.user)

    def test_invalid_post_returns_the_form_with_errors(self):
        response = self.client.post(self.url, create_data(name=""), **HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")
        self.assertFalse(Organisation.objects.filter(submitted_by=self.user).exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_non_htmx_post_redirects_to_the_company_card(self):
        response = self.client.post(self.url, create_data())
        self.assertRedirects(response, self.page + "#company", fetch_redirect_response=False)
        self.assertTrue(Organisation.objects.filter(submitted_by=self.user).exists())

    def test_co_applicant_and_strangers_cannot_create(self):
        org = make_organisation()
        Deal.objects.filter(pk=self.deal.pk).update(organisation=org)
        self.deal.refresh_from_db()
        co = make_co_applicant(self.deal, with_user=True)
        Deal.objects.filter(pk=self.deal.pk).update(organisation=None)

        self.client.force_login(co.user)
        self.assertEqual(self.client.post(self.url, create_data(), **HTMX).status_code, 403)
        self.assertNotContains(self.client.get(self.page), self.url)
        self.assertNotIn(
            "Tell us about your company",
            [item.label for item in outstanding_items(Deal.objects.get(pk=self.deal.pk), user=co.user)],
        )

        self.client.force_login(make_customer())
        self.assertEqual(self.client.post(self.url, create_data(), **HTMX).status_code, 404)
        self.assertFalse(Organisation.objects.filter(submitted_by__isnull=False).exists())

    def test_staff_only_fields_are_ignored_on_create(self):
        self.client.post(
            self.url,
            {**create_data(), "company-legal_name": "FORGED LTD", "company-sector": "dental",
             "company-verified_at": "2020-01-01"},
            **HTMX,
        )
        org = Organisation.objects.get(submitted_by=self.user)
        self.assertEqual(org.legal_name, "")
        self.assertIsNone(org.sector)
        self.assertIsNone(org.verified_at)


class CompaniesHouseNumberRuleTests(TestCase):
    def _form(self, company_type, **overrides):
        _user, deal = deal_without_organisation(company_type=company_type)
        return CustomerCompanyCreateForm(create_data(**overrides), deal=deal, prefix="company")

    def test_number_is_never_required(self):
        for company_type in (
            Deal.CompanyType.LIMITED_COMPANY, Deal.CompanyType.PARTNERSHIP,
            Deal.CompanyType.OTHER, "",
        ):
            form = self._form(company_type, companies_house_number="")
            self.assertFalse(form.fields["companies_house_number"].required)
            self.assertTrue(form.is_valid(), form.errors)

    def test_sole_trader_is_not_asked_for_one(self):
        form = self._form(Deal.CompanyType.SOLE_TRADER, companies_house_number="")
        self.assertNotIn("companies_house_number", form.fields)
        self.assertEqual(form.fields["name"].label, "Business or trading name")
        self.assertTrue(form.is_valid(), form.errors)

    def test_number_is_normalised_and_format_checked(self):
        form = self._form(Deal.CompanyType.PARTNERSHIP, companies_house_number=" oc 123456 ")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["companies_house_number"], "OC123456")
        form = self._form(Deal.CompanyType.LIMITED_COMPANY, companies_house_number="ABC")
        self.assertFalse(form.is_valid())


class EditRulesTests(TestCase):
    """Who gets which of the three edit modes, and what each records."""

    def setUp(self):
        self.user, self.deal = make_customer_with_deal()
        self.org = self.deal.organisation  # staff-created, so verified
        self.url = reverse("crm:my_company_edit", args=[self.org.pk])
        self.client.force_login(self.user)

    def _post(self, **overrides):
        self.org.refresh_from_db()
        return self.client.post(self.url, edit_data(self.org, **overrides), **HTMX)

    def test_submitter_edits_their_unverified_organisation_freely(self):
        org = make_unverified_organisation(self.deal)
        mail.outbox.clear()
        response = self.client.post(
            reverse("crm:my_company_edit", args=[org.pk]),
            edit_data(org, name="Corrected Name Ltd", phone="0117 111 1111"),
            **HTMX,
        )
        self.assertEqual(response.status_code, 200)
        org.refresh_from_db()
        self.assertEqual(org.name, "Corrected Name Ltd")
        self.assertEqual(org.phone, "0117 111 1111")
        self.assertFalse(OrganisationChange.objects.exists())
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(org.is_unverified)

    def test_another_customer_on_an_unverified_organisation_is_held(self):
        """Staff can attach an unverified organisation to a second deal; that
        deal's customer must not be able to rename it."""
        org = make_unverified_organisation(self.deal)
        other_user = make_customer()
        make_deal(customer=make_contact(user=other_user, organisation=org), organisation=org)
        self.client.force_login(other_user)
        self.client.post(
            reverse("crm:my_company_edit", args=[org.pk]),
            edit_data(org, name="Hijacked Ltd"),
            **HTMX,
        )
        name = org.name
        org.refresh_from_db()
        self.assertEqual(org.name, name)
        self.assertEqual(OrganisationChange.objects.get().submitted_by, other_user)

    def test_contact_details_apply_and_queue_for_review(self):
        old_phone = self.org.phone
        self._post(phone="0117 222 2222")
        self.org.refresh_from_db()
        self.assertEqual(self.org.phone, "0117 222 2222")
        change = OrganisationChange.objects.get()
        self.assertTrue(change.applied)
        self.assertTrue(change.is_pending)
        self.assertEqual(change.changes, {"phone": {"old": old_phone, "new": "0117 222 2222"}})
        self.assertEqual(len(mail.outbox), 0)  # the dashboard queue covers these

    def test_stacked_contact_edits_merge_and_keep_the_first_old_value(self):
        old_phone = self.org.phone
        self._post(phone="0117 222 2222")
        self._post(phone="0117 333 3333", email="office@example.com")
        change = OrganisationChange.objects.get()
        self.assertEqual(change.changes["phone"], {"old": old_phone, "new": "0117 333 3333"})
        self.assertEqual(change.changes["email"]["new"], "office@example.com")
        # Putting everything back leaves nothing to review.
        self._post(phone=old_phone, email="")
        self.assertFalse(OrganisationChange.objects.exists())

    def test_identity_fields_are_held_and_the_team_is_emailed(self):
        name = self.org.name
        response = self._post(name="New Name Ltd")
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, name)
        change = OrganisationChange.objects.get()
        self.assertFalse(change.applied)
        self.assertEqual(change.changes, {"name": {"old": name, "new": "New Name Ltd"}})
        self.assertContains(response, "New Name Ltd")
        self.assertContains(response, "Being checked")

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, settings.NOTIFY_EMAILS)
        self.assertIn("Name", mail.outbox[0].body)
        self.assertNotIn("New Name Ltd", mail.outbox[0].body)
        note = Note.objects.filter(type=Note.Type.CUSTOMER_UPDATE).latest("pk")
        self.assertIn("awaiting review", note.content)
        self.assertNotIn("New Name Ltd", note.content)

    def test_edit_form_shows_the_pending_request_so_resaving_keeps_it(self):
        self._post(name="New Name Ltd")
        form = self.client.get(self.url, **HTMX)
        self.assertContains(form, 'value="New Name Ltd"')
        # Saving again with an unrelated change must not withdraw the request.
        self._post(name="New Name Ltd", phone="0117 444 4444")
        proposal = OrganisationChange.objects.get(applied=False)
        self.assertEqual(proposal.changes["name"]["new"], "New Name Ltd")
        # Setting the name back to the live one withdraws it.
        self._post(name=self.org.name)
        self.assertFalse(OrganisationChange.objects.filter(applied=False).exists())

    def test_a_proposal_makes_a_stale_second_tab_conflict(self):
        stale = edit_data(self.org, phone="0117 555 5555")
        self._post(name="New Name Ltd")
        response = self.client.post(self.url, stale, **HTMX)
        self.assertContains(response, "Someone else changed these details")
        self.org.refresh_from_db()
        self.assertNotEqual(self.org.phone, "0117 555 5555")

    def test_a_proposal_is_shown_only_to_the_customer_who_made_it(self):
        self._post(name="Secret New Name Ltd")
        other_user = make_customer()
        other_deal = make_deal(
            customer=make_contact(user=other_user, organisation=self.org), organisation=self.org,
        )
        self.client.force_login(other_user)
        page = self.client.get(reverse("crm:my_deal_detail", args=[other_deal.pk]))
        self.assertNotContains(page, "Secret New Name Ltd")
        self.assertNotContains(page, "Being checked")

    def test_verification_mid_edit_switches_the_rules(self):
        org = make_unverified_organisation(self.deal)
        stale = edit_data(org, name="Late Rename Ltd")
        org_review.verify(org, by=make_associate())
        response = self.client.post(reverse("crm:my_company_edit", args=[org.pk]), stale, **HTMX)
        self.assertContains(response, "Someone else changed these details")
        name = org.name
        org.refresh_from_db()
        self.assertEqual(org.name, name)


class StaffReviewTests(TestCase):
    def setUp(self):
        self.user, self.deal = make_customer_with_deal()
        self.staff = self.deal.owner
        self.org = self.deal.organisation
        self.client.force_login(self.staff)

    def _customer_edit(self, **values):
        self.org.refresh_from_db()
        data = {name: getattr(self.org, name) for name in org_review.CUSTOMER_FIELDS}
        data.update(values)
        return org_review.record_customer_edit(self.org.pk, data, by_user=self.user)

    def test_verify_marks_the_organisation_and_clears_the_queue(self):
        org = make_unverified_organisation(self.deal)
        self.assertIn(org, org_review.needs_review(self.staff))
        page = self.client.get(self.deal.get_absolute_url())
        self.assertContains(page, "Customer-entered organisation details to review")
        self.assertContains(page, "unverified")

        response = self.client.post(
            reverse("crm:organisation_verify", args=[org.pk]),
            {"next": self.deal.get_absolute_url()},
        )
        self.assertRedirects(response, self.deal.get_absolute_url(), fetch_redirect_response=False)
        org.refresh_from_db()
        self.assertFalse(org.is_unverified)
        self.assertEqual(org.verified_by, self.staff)
        self.assertNotIn(org, org_review.needs_review(self.staff))
        self.assertNotContains(
            self.client.get(self.deal.get_absolute_url()),
            "Customer-entered organisation details to review",
        )

    def test_approving_a_proposal_applies_it(self):
        self._customer_edit(name="Approved Name Ltd", companies_house_number="07654321")
        change = OrganisationChange.objects.get()
        page = self.client.get(self.org.get_absolute_url())
        self.assertContains(page, "Approved Name Ltd")
        self.assertContains(page, "not saved")

        self.client.post(reverse("crm:organisation_change_approve", args=[change.pk]))
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, "Approved Name Ltd")
        self.assertEqual(self.org.companies_house_number, "07654321")
        change.refresh_from_db()
        self.assertEqual(change.status, OrganisationChange.Status.APPROVED)
        self.assertEqual(change.reviewed_by, self.staff)
        self.assertNotIn(self.org, org_review.needs_review(self.staff))

    def test_rejecting_a_proposal_discards_it(self):
        name = self.org.name
        self._customer_edit(name="Rejected Name Ltd")
        change = OrganisationChange.objects.get()
        self.client.post(reverse("crm:organisation_change_reject", args=[change.pk]))
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, name)
        change.refresh_from_db()
        self.assertEqual(change.status, OrganisationChange.Status.REJECTED)

    def test_approving_an_applied_change_keeps_it(self):
        self._customer_edit(phone="0117 666 6666")
        change = OrganisationChange.objects.get()
        self.client.post(reverse("crm:organisation_change_approve", args=[change.pk]))
        self.org.refresh_from_db()
        self.assertEqual(self.org.phone, "0117 666 6666")

    def test_rejecting_an_applied_change_reverts_only_untouched_fields(self):
        old_phone, old_email = self.org.phone, self.org.email
        self._customer_edit(phone="0117 777 7777", email="customer@example.com")
        # Staff have since corrected the email themselves — that must survive.
        Organisation.objects.filter(pk=self.org.pk).update(email="staff@example.com")
        change = OrganisationChange.objects.get()
        self.client.post(reverse("crm:organisation_change_reject", args=[change.pk]))
        self.org.refresh_from_db()
        self.assertEqual(self.org.phone, old_phone)
        self.assertEqual(self.org.email, "staff@example.com")
        self.assertNotEqual(self.org.email, old_email)

    def test_reviewing_twice_is_harmless(self):
        self._customer_edit(name="Once Ltd")
        change = OrganisationChange.objects.get()
        self.client.post(reverse("crm:organisation_change_approve", args=[change.pk]))
        Organisation.objects.filter(pk=self.org.pk).update(name="Staff Fixed Ltd")
        self.client.post(reverse("crm:organisation_change_approve", args=[change.pk]))
        self.client.post(reverse("crm:organisation_change_reject", args=[change.pk]))
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, "Staff Fixed Ltd")

    def test_staff_who_cannot_see_the_organisation_cannot_act(self):
        Organisation.objects.filter(pk=self.org.pk).update(owner=self.staff)
        org = make_unverified_organisation(self.deal)
        self._customer_edit(name="Hidden Ltd")
        change = OrganisationChange.objects.get()
        Organisation.objects.filter(pk__in=[org.pk, self.org.pk]).update(owner=self.staff)

        outsider = make_associate()
        self.client.force_login(outsider)
        self.assertNotIn(org, org_review.needs_review(outsider))
        self.assertEqual(
            self.client.post(reverse("crm:organisation_verify", args=[org.pk])).status_code, 404,
        )
        self.assertEqual(
            self.client.post(
                reverse("crm:organisation_change_approve", args=[change.pk])
            ).status_code,
            404,
        )
        org.refresh_from_db()
        self.assertTrue(org.is_unverified)

        # An admin sees everything.
        self.client.force_login(make_admin())
        self.client.post(reverse("crm:organisation_verify", args=[org.pk]))
        org.refresh_from_db()
        self.assertFalse(org.is_unverified)

    def test_customers_cannot_reach_the_review_actions(self):
        org = make_unverified_organisation(self.deal)
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.post(reverse("crm:organisation_verify", args=[org.pk])).status_code, 403,
        )
        org.refresh_from_db()
        self.assertTrue(org.is_unverified)

    def test_dashboard_and_list_show_the_queue(self):
        dashboard = self.client.get(reverse("dashboard"))
        self.assertNotContains(dashboard, "awaiting review")
        org = make_unverified_organisation(self.deal)
        dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(dashboard, "1 organisation")
        self.assertContains(dashboard, "awaiting review")

        make_organisation(name="Unrelated Verified Org")
        listing = self.client.get(reverse("crm:organisation_list"), {"review": "1"})
        self.assertContains(listing, org.name)
        self.assertNotContains(listing, "Unrelated Verified Org")

    def test_stale_deal_form_cannot_blank_a_customer_created_organisation(self):
        Deal.objects.filter(pk=self.deal.pk).update(organisation=None)
        stale_deal = Deal.objects.get(pk=self.deal.pk)
        initial_form = DealForm(instance=stale_deal, current_user=self.staff)
        data = {
            name: (value.pk if hasattr(value, "pk") else value)
            for name, value in initial_form.initial.items() if value is not None
        }
        org = make_unverified_organisation(self.deal)

        form = DealForm(data, instance=stale_deal, current_user=self.staff)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.organisation, org)


class DuplicateSwapTests(TestCase):
    def setUp(self):
        self.user, self.deal = make_customer_with_deal()
        self.staff = self.deal.owner
        self.existing = make_organisation(
            name="Bright Smiles Limited", companies_house_number="1234567",
            address_line1="Old Address", phone="0117 000 0000",
        )
        self.dup = make_unverified_organisation(
            self.deal, name="Bright Smiles Ltd", companies_house_number="01234567",
            address_line1="1 High Street", phone="0117 000 0000",
        )
        self.client.force_login(self.staff)

    def _swap(self, target, **extra):
        return self.client.post(
            reverse("crm:organisation_use_existing", args=[self.dup.pk]),
            {"target": target.pk, **extra},
        )

    def test_duplicates_match_on_number_or_similar_name(self):
        by_name = make_organisation(name="Brite Smiles")
        make_organisation(name="Something Else Entirely")
        found = {o.pk: o.match_reason for o in org_review.likely_duplicates(self.dup)}
        self.assertEqual(found[self.existing.pk], "Same Companies House number")
        self.assertNotIn(by_name.pk, found)  # too different
        close = make_organisation(name="Bright Smile")
        found = {o.pk: o.match_reason for o in org_review.likely_duplicates(self.dup)}
        self.assertEqual(found[close.pk], "Similar name")
        self.assertEqual(len(found), 2)

        page = self.client.get(self.deal.get_absolute_url())
        self.assertContains(page, "Possible duplicates already in the CRM")
        self.assertContains(page, "Bright Smiles Limited")

    def test_swap_moves_everything_and_deletes_the_duplicate(self):
        dup_pk = self.dup.pk
        response = self._swap(self.existing, next=self.deal.get_absolute_url())
        self.assertRedirects(response, self.deal.get_absolute_url(), fetch_redirect_response=False)

        self.assertFalse(Organisation.objects.filter(pk=dup_pk).exists())
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.organisation, self.existing)
        self.assertIn(self.existing, self.deal.customer.organisations.all())
        # The customer's "added their company" note survives on the target.
        self.assertTrue(
            Note.objects.filter(type=Note.Type.CUSTOMER_UPDATE, organisation=self.existing).exists()
        )
        # Nothing on the existing record changed; the customer's differing
        # contact details wait as a proposal (phone was identical).
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.address_line1, "Old Address")
        change = OrganisationChange.objects.get(organisation=self.existing)
        self.assertFalse(change.applied)
        self.assertEqual(change.submitted_by, self.user)
        self.assertEqual(set(change.changes), {"address_line1"})
        self.assertIn(self.existing, org_review.needs_review(self.staff))

    def test_swap_from_the_duplicates_own_page_lands_on_the_target(self):
        response = self._swap(self.existing, next=self.dup.get_absolute_url())
        self.assertRedirects(response, self.existing.get_absolute_url(), fetch_redirect_response=False)

    def test_swap_refuses_an_organisation_that_is_not_a_likely_match(self):
        unrelated = make_organisation(name="Completely Different Dental Lab")
        response = self._swap(unrelated)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Organisation.objects.filter(pk=self.dup.pk).exists())
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.organisation, self.dup)

    def test_swap_refuses_a_duplicate_that_is_in_real_use(self):
        make_participation(make_deal(), organisation=self.dup)
        with self.assertRaises(org_review.SwapNotAllowed):
            org_review.swap_to_existing(self.dup, self.existing, by=self.staff)
        self.assertTrue(Organisation.objects.filter(pk=self.dup.pk).exists())

    def test_a_verified_organisation_cannot_be_swapped_away(self):
        org_review.verify(self.dup, by=self.staff)
        with self.assertRaises(org_review.SwapNotAllowed):
            org_review.swap_to_existing(self.dup, self.existing, by=self.staff)


@override_settings(DOCUSEAL_URL="http://sign.test", DOCUSEAL_API_TOKEN="tok")
class SignatureBlockTests(TestCase):
    def setUp(self):
        self.user, self.deal = make_customer_with_deal()
        self.staff = self.deal.owner
        self.org = make_unverified_organisation(self.deal)
        self.client.force_login(self.staff)
        self.url = reverse("crm:deal_sign", args=[self.deal.pk])

    def test_new_request_is_blocked_until_verified(self):
        for response in (self.client.get(self.url), self.client.post(self.url, {})):
            self.assertRedirects(
                response, self.deal.get_absolute_url() + "#organisation-review",
                fetch_redirect_response=False,
            )
        self.assertFalse(SignatureRequest.objects.exists())
        page = self.client.get(self.deal.get_absolute_url())
        self.assertContains(page, "New signature requests are blocked")
        self.assertNotContains(page, f'href="{self.url}"')

    def test_resend_is_blocked_until_verified(self):
        sr = make_signature_request(self.deal)
        response = self.client.post(reverse("crm:signature_resend", args=[sr.pk]), follow=True)
        self.assertContains(response, "verified yet")
        self.assertEqual(SignatureRequest.objects.count(), 1)

    def test_verifying_lifts_the_block(self):
        org_review.verify(self.org, by=self.staff)
        page = self.client.get(self.deal.get_absolute_url())
        self.assertContains(page, f'href="{self.url}"')
        self.assertNotContains(page, "New signature requests are blocked")
