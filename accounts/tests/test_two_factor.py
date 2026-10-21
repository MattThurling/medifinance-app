"""Two-factor authentication: the code maths on User, the role-driven gate
in the middleware, and the enrol / verify / disable views."""

import time

import pyotp
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Role, User
from accounts.two_factor import SESSION_VERIFIED_KEY


def code_for(secret: str, *, offset_steps: int = 0) -> str:
    return pyotp.TOTP(secret).at(time.time() + 30 * offset_steps)


def enrol(user: User) -> str:
    """Enrol with a code from a few steps ago so the tests' later codes
    (previous / current / next step) all count as newer than the last one."""
    secret = User.generate_totp_secret()
    past = time.time() - 90
    assert user.confirm_totp(secret, pyotp.TOTP(secret).at(past), now=past)
    return secret


class UserTotpTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="a@example.com", password="x")

    def test_not_enabled_until_confirmed(self):
        self.assertFalse(self.user.two_factor_enabled)
        secret = User.generate_totp_secret()
        self.assertFalse(self.user.confirm_totp(secret, "000000"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.totp_secret, "")
        self.assertFalse(self.user.two_factor_enabled)

    def test_confirm_with_correct_code(self):
        secret = User.generate_totp_secret()
        self.assertTrue(self.user.confirm_totp(secret, code_for(secret)))
        self.user.refresh_from_db()
        self.assertTrue(self.user.two_factor_enabled)
        self.assertEqual(self.user.totp_secret, secret)

    def test_verify_accepts_current_and_adjacent_steps(self):
        secret = enrol(self.user)
        self.assertTrue(self.user.verify_totp(code_for(secret, offset_steps=-1)))
        # Each accepted step is later than the last, so these pass too.
        self.assertTrue(self.user.verify_totp(code_for(secret)))
        self.assertTrue(self.user.verify_totp(code_for(secret, offset_steps=1)))

    def test_verify_rejects_wrong_and_malformed_codes(self):
        secret = enrol(self.user)
        self.assertFalse(self.user.verify_totp("000000"))
        self.assertFalse(self.user.verify_totp(""))
        self.assertFalse(self.user.verify_totp("12345"))
        self.assertFalse(self.user.verify_totp("abcdef"))
        self.assertTrue(self.user.verify_totp(code_for(secret)))

    def test_verify_accepts_spaces_in_code(self):
        secret = enrol(self.user)
        code = code_for(secret)
        self.assertTrue(self.user.verify_totp(f"{code[:3]} {code[3:]}"))

    def test_code_cannot_be_replayed(self):
        secret = enrol(self.user)
        code = code_for(secret)
        self.assertTrue(self.user.verify_totp(code))
        self.assertFalse(self.user.verify_totp(code))
        # An older (still in-window) code is also refused after a newer one.
        self.assertFalse(self.user.verify_totp(code_for(secret, offset_steps=-1)))

    def test_verify_without_enrolment_is_false(self):
        self.assertFalse(self.user.verify_totp("123456"))

    def test_reset_clears_everything(self):
        enrol(self.user)
        self.user.reset_totp()
        self.user.refresh_from_db()
        self.assertFalse(self.user.two_factor_enabled)
        self.assertEqual(self.user.totp_secret, "")
        self.assertIsNone(self.user.totp_last_used_step)

    def test_provisioning_uri_names_issuer_and_account(self):
        secret = User.generate_totp_secret()
        uri = self.user.totp_provisioning_uri(secret)
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn("a%40example.com", uri)
        self.assertIn("issuer=Medifinance", uri)
        self.assertIn(f"secret={secret}", uri)


class RequiredRolesTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email="ad@example.com", password="x", role=Role.ADMIN)
        self.assoc = User.objects.create_user(email="as@example.com", password="x", role=Role.ASSOCIATE)
        self.cust = User.objects.create_user(email="cu@example.com", password="x", role=Role.CUSTOMER)

    @override_settings(TWO_FACTOR_REQUIRED_ROLES=[])
    def test_nobody_required_by_default(self):
        for u in (self.admin, self.assoc, self.cust):
            self.assertFalse(u.two_factor_required)
            self.assertFalse(u.two_factor_needed)

    @override_settings(TWO_FACTOR_REQUIRED_ROLES=["admin", "associate"])
    def test_listed_roles_are_required(self):
        self.assertTrue(self.admin.two_factor_required)
        self.assertTrue(self.assoc.two_factor_required)
        self.assertFalse(self.cust.two_factor_required)

    @override_settings(TWO_FACTOR_REQUIRED_ROLES=["all"])
    def test_all_keyword(self):
        for u in (self.admin, self.assoc, self.cust):
            self.assertTrue(u.two_factor_required)

    @override_settings(TWO_FACTOR_REQUIRED_ROLES=[])
    def test_opted_in_user_still_needs_a_code(self):
        enrol(self.assoc)
        self.assertFalse(self.assoc.two_factor_required)
        self.assertTrue(self.assoc.two_factor_needed)

    @override_settings(TWO_FACTOR_REQUIRED_ROLES=["admn"])
    def test_system_check_rejects_unknown_role(self):
        from accounts.two_factor import check_required_roles
        errors = check_required_roles(None)
        self.assertEqual([e.id for e in errors], ["accounts.E001"])

    @override_settings(TWO_FACTOR_REQUIRED_ROLES=["admin", "all"])
    def test_system_check_passes_valid_roles(self):
        from accounts.two_factor import check_required_roles
        self.assertEqual(check_required_roles(None), [])


@override_settings(TWO_FACTOR_REQUIRED_ROLES=["admin"])
class MiddlewareTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email="ad@example.com", password="x", role=Role.ADMIN)
        self.assoc = User.objects.create_user(email="as@example.com", password="x", role=Role.ASSOCIATE)

    def test_anonymous_untouched(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertRedirects(resp, f"{reverse('login')}?next=/", fetch_redirect_response=False)

    def test_unrequired_role_passes_straight_through(self):
        self.client.login(username="as@example.com", password="x")
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_required_unenrolled_user_is_sent_to_setup(self):
        self.client.login(username="ad@example.com", password="x")
        resp = self.client.get(reverse("dashboard"))
        self.assertRedirects(resp, f"{reverse('two_factor_setup')}?next=%2F", fetch_redirect_response=False)
        # Same for the admin site and a POST (no next on a POST).
        resp = self.client.get("/admin/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith(reverse("two_factor_setup")))
        resp = self.client.post(reverse("toggle_api_access"))
        self.assertRedirects(resp, reverse("two_factor_setup"), fetch_redirect_response=False)

    def test_required_enrolled_user_is_sent_to_verify(self):
        enrol(self.admin)
        self.client.login(username="ad@example.com", password="x")
        resp = self.client.get(reverse("crm:deal_list"))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith(reverse("two_factor_verify")))
        self.assertIn("next=%2Fdeals%2F", resp.url)

    def test_opted_in_associate_is_sent_to_verify(self):
        enrol(self.assoc)
        self.client.login(username="as@example.com", password="x")
        resp = self.client.get(reverse("dashboard"))
        self.assertTrue(resp.url.startswith(reverse("two_factor_verify")))

    def test_exempt_pages_stay_reachable(self):
        self.client.login(username="ad@example.com", password="x")
        self.assertEqual(self.client.get(reverse("two_factor_setup")).status_code, 200)
        self.assertEqual(self.client.get(reverse("developers")).status_code, 200)
        resp = self.client.post(reverse("logout"))
        self.assertEqual(resp.status_code, 302)

    def test_verified_session_passes(self):
        enrol(self.admin)
        self.client.login(username="ad@example.com", password="x")
        session = self.client.session
        session[SESSION_VERIFIED_KEY] = self.admin.pk
        session.save()
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_verification_is_bound_to_the_user(self):
        enrol(self.admin)
        self.client.login(username="ad@example.com", password="x")
        session = self.client.session
        session[SESSION_VERIFIED_KEY] = self.assoc.pk  # someone else's
        session.save()
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 302)


@override_settings(TWO_FACTOR_REQUIRED_ROLES=["admin"])
class SetupViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email="ad@example.com", password="x", role=Role.ADMIN)
        self.client.login(username="ad@example.com", password="x")

    def test_get_shows_qr_and_manual_key(self):
        resp = self.client.get(reverse("two_factor_setup"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "<svg")
        secret = self.client.session["two_factor_pending_secret"]
        self.assertContains(resp, secret[:4])
        # The same secret is reused on reload so a slow scan still works.
        self.client.get(reverse("two_factor_setup"))
        self.assertEqual(self.client.session["two_factor_pending_secret"], secret)

    def test_wrong_code_does_not_enrol(self):
        self.client.get(reverse("two_factor_setup"))
        resp = self.client.post(reverse("two_factor_setup"), {"code": "000000"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "didn&#x27;t match")
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.two_factor_enabled)

    def test_correct_code_enrols_verifies_and_redirects_to_next(self):
        self.client.get(reverse("two_factor_setup"))
        secret = self.client.session["two_factor_pending_secret"]
        resp = self.client.post(
            reverse("two_factor_setup"), {"code": code_for(secret), "next": "/deals/"},
        )
        self.assertRedirects(resp, "/deals/", fetch_redirect_response=False)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.two_factor_enabled)
        self.assertEqual(self.client.session[SESSION_VERIFIED_KEY], self.admin.pk)
        self.assertNotIn("two_factor_pending_secret", self.client.session)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_offsite_next_is_ignored(self):
        self.client.get(reverse("two_factor_setup"))
        secret = self.client.session["two_factor_pending_secret"]
        resp = self.client.post(
            reverse("two_factor_setup"), {"code": code_for(secret), "next": "https://evil.example/"},
        )
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)

    def test_too_many_wrong_codes_logs_out(self):
        self.client.get(reverse("two_factor_setup"))
        with self.settings(TWO_FACTOR_MAX_ATTEMPTS=3):
            for _ in range(2):
                self.client.post(reverse("two_factor_setup"), {"code": "000000"})
            resp = self.client.post(reverse("two_factor_setup"), {"code": "000000"})
        self.assertRedirects(resp, reverse("login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_enrolled_but_unverified_cannot_reach_setup(self):
        enrol(self.admin)
        resp = self.client.get(reverse("two_factor_setup"))
        self.assertTrue(resp.url.startswith(reverse("two_factor_verify")))


@override_settings(TWO_FACTOR_REQUIRED_ROLES=["admin"])
class VerifyViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email="ad@example.com", password="x", role=Role.ADMIN)
        self.secret = enrol(self.admin)
        self.client.login(username="ad@example.com", password="x")

    def test_unenrolled_user_is_sent_to_setup(self):
        self.admin.reset_totp()
        resp = self.client.get(reverse("two_factor_verify"))
        self.assertRedirects(resp, reverse("two_factor_setup"), fetch_redirect_response=False)

    def test_get_renders(self):
        resp = self.client.get(reverse("two_factor_verify"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Enter your code")

    def test_wrong_code_stays_locked(self):
        resp = self.client.post(reverse("two_factor_verify"), {"code": "000000"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(SESSION_VERIFIED_KEY, self.client.session)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)

    def test_correct_code_unlocks_and_follows_next(self):
        resp = self.client.post(
            reverse("two_factor_verify"), {"code": code_for(self.secret), "next": "/deals/"},
        )
        self.assertRedirects(resp, "/deals/", fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_already_verified_skips_the_page(self):
        self.client.post(reverse("two_factor_verify"), {"code": code_for(self.secret)})
        resp = self.client.get(reverse("two_factor_verify"))
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)

    def test_fresh_login_needs_a_new_code(self):
        self.client.post(reverse("two_factor_verify"), {"code": code_for(self.secret)})
        self.client.post(reverse("logout"))
        self.client.login(username="ad@example.com", password="x")
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)

    def test_too_many_wrong_codes_logs_out(self):
        with self.settings(TWO_FACTOR_MAX_ATTEMPTS=2):
            self.client.post(reverse("two_factor_verify"), {"code": "000000"})
            resp = self.client.post(reverse("two_factor_verify"), {"code": "000000"})
        self.assertRedirects(resp, reverse("login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)


@override_settings(TWO_FACTOR_REQUIRED_ROLES=["admin"])
class SecuritySettingsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email="ad@example.com", password="x", role=Role.ADMIN)
        self.assoc = User.objects.create_user(email="as@example.com", password="x", role=Role.ASSOCIATE)

    def _login_verified(self, user, secret):
        self.client.login(username=user.email, password="x")
        self.client.post(reverse("two_factor_verify"), {"code": code_for(secret)})

    def test_page_offers_setup_when_off(self):
        self.client.login(username="as@example.com", password="x")
        resp = self.client.get(reverse("two_factor_settings"))
        self.assertContains(resp, reverse("two_factor_setup"))
        self.assertNotContains(resp, reverse("two_factor_disable"))

    def test_optional_user_can_turn_off_with_a_code(self):
        secret = enrol(self.assoc)
        self._login_verified(self.assoc, secret)
        resp = self.client.get(reverse("two_factor_settings"))
        self.assertContains(resp, reverse("two_factor_disable"))
        resp = self.client.post(
            reverse("two_factor_disable"), {"code": code_for(secret, offset_steps=1)},
        )
        self.assertRedirects(resp, reverse("two_factor_settings"), fetch_redirect_response=False)
        self.assoc.refresh_from_db()
        self.assertFalse(self.assoc.two_factor_enabled)

    def test_turn_off_needs_a_valid_code(self):
        secret = enrol(self.assoc)
        self._login_verified(self.assoc, secret)
        self.client.post(reverse("two_factor_disable"), {"code": "000000"})
        self.assoc.refresh_from_db()
        self.assertTrue(self.assoc.two_factor_enabled)

    def test_required_role_cannot_turn_off(self):
        secret = enrol(self.admin)
        self._login_verified(self.admin, secret)
        resp = self.client.get(reverse("two_factor_settings"))
        self.assertContains(resp, "required")
        self.assertNotContains(resp, reverse("two_factor_disable"))
        resp = self.client.post(reverse("two_factor_disable"), {"code": code_for(secret, offset_steps=1)})
        self.assertEqual(resp.status_code, 403)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.two_factor_enabled)

    def test_admin_action_resets(self):
        from django.contrib.admin.sites import site
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage

        enrol(self.assoc)
        request = RequestFactory().post("/admin/accounts/user/")
        request.user = self.admin
        request.session = {}
        request._messages = FallbackStorage(request)
        site._registry[User].reset_two_factor(request, User.objects.filter(pk=self.assoc.pk))
        self.assoc.refresh_from_db()
        self.assertFalse(self.assoc.two_factor_enabled)
