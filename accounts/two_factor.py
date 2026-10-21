"""Two-factor authentication (TOTP) — session gate and middleware.

The user model owns the secret and the code checks (`User.verify_totp`,
`User.confirm_totp`). This module decides *when* a code is needed:

- `TWO_FACTOR_REQUIRED_ROLES` (settings, from the env) lists the roles that
  must use 2FA. A user in one of those roles who hasn't enrolled is sent to
  the setup page on their first request after logging in; everyone enrolled
  (required or opted in) is sent to the verify page once per login session.
- The session records which user pk has verified, so a session that changes
  user (Django's `login()` keeps the session when it's the same user) can't
  carry a verification across.
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.core.checks import Error, register
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

SESSION_VERIFIED_KEY = "two_factor_verified_user"
SESSION_PENDING_SECRET_KEY = "two_factor_pending_secret"
SESSION_ATTEMPTS_KEY = "two_factor_attempts"

# URL names reachable without having passed the second factor. Auth plumbing
# and the 2FA pages themselves; everything else behind a login is gated.
EXEMPT_URL_NAMES = frozenset({
    "login",
    "logout",
    "password_reset",
    "password_reset_done",
    "password_reset_confirm",
    "password_reset_complete",
    "two_factor_setup",
    "two_factor_verify",
    "consume_magic_link",
    "developers",
})


def is_verified(request) -> bool:
    return request.session.get(SESSION_VERIFIED_KEY) == request.user.pk


def mark_verified(request) -> None:
    request.session[SESSION_VERIFIED_KEY] = request.user.pk
    request.session.pop(SESSION_ATTEMPTS_KEY, None)
    request.session.pop(SESSION_PENDING_SECRET_KEY, None)
    # A fresh session key once the second factor is proven, same as login().
    request.session.cycle_key()


def record_failed_attempt(request) -> bool:
    """Count a wrong code. Returns True when the limit is hit — the caller
    should treat that as a forced logout (already done here)."""
    attempts = request.session.get(SESSION_ATTEMPTS_KEY, 0) + 1
    request.session[SESSION_ATTEMPTS_KEY] = attempts
    if attempts >= settings.TWO_FACTOR_MAX_ATTEMPTS:
        logout(request)
        messages.error(request, "Too many incorrect codes. Please sign in again.")
        return True
    return False


def safe_next(request, default: str = "/") -> str:
    """`?next=` from the query string or POST body, only if it's a local path."""
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return candidate
    return default


class TwoFactorMiddleware:
    """Send authenticated users who still owe a second factor to the setup
    or verify page. Runs as `process_view` so the resolved URL name is
    available for the exemption list."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        if not user.two_factor_needed or is_verified(request):
            return None
        match = request.resolver_match
        if match is not None and match.url_name in EXEMPT_URL_NAMES:
            return None

        target = reverse("two_factor_verify" if user.two_factor_enabled else "two_factor_setup")
        if request.method == "GET":
            target += "?" + urlencode({"next": request.get_full_path()})
        return redirect(target)


@register()
def check_required_roles(app_configs, **kwargs):
    """Fail startup if TWO_FACTOR_REQUIRED_ROLES names a role that doesn't
    exist — otherwise a typo would quietly exempt everyone."""
    from .models import Role

    valid = {r.value for r in Role} | {"all"}
    bad = [r for r in settings.TWO_FACTOR_REQUIRED_ROLES if r not in valid]
    if not bad:
        return []
    return [
        Error(
            f"TWO_FACTOR_REQUIRED_ROLES contains unknown role(s): {', '.join(bad)}.",
            hint=f"Valid values: {', '.join(sorted(valid))}.",
            id="accounts.E001",
        )
    ]
