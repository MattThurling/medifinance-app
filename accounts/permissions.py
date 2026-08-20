"""Role-based permission helpers.

Two layers exist in the app:

* **Role** — coarse (Admin / Associate / Customer). Use the decorators / mixins
  in this module to gate views by role.
* **Object permissions** (django-guardian) — fine-grained per-row. Wire up in
  signals or in the views that create the objects (e.g. assign the deal owner
  change/view perms on their deal).
"""

from __future__ import annotations

from functools import wraps

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest

from .models import Role


def _has_role(user, roles: set[str]) -> bool:
    return user.is_authenticated and getattr(user, "role", None) in roles


def role_required(*roles: str):
    """Function-view decorator: require the user to hold one of `roles`."""
    allowed = set(roles)

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request: HttpRequest, *args, **kwargs):
            if not _has_role(request.user, allowed):
                raise PermissionDenied
            return view_func(request, *args, **kwargs)
        return _wrapped

    return decorator


def admin_required(view_func):
    return role_required(Role.ADMIN)(view_func)


def staff_required(view_func):
    """Admin OR Associate — anyone who can use the internal UI."""
    return role_required(Role.ADMIN, Role.ASSOCIATE)(view_func)


class RoleRequiredMixin(LoginRequiredMixin):
    """CBV mixin. Set `required_roles = {Role.ADMIN, ...}` on the view."""

    required_roles: set[str] = set()

    def dispatch(self, request, *args, **kwargs):
        if not _has_role(request.user, self.required_roles):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class AdminRequiredMixin(RoleRequiredMixin):
    required_roles = {Role.ADMIN}


class StaffRequiredMixin(RoleRequiredMixin):
    required_roles = {Role.ADMIN, Role.ASSOCIATE}


class FinanceRequiredMixin(StaffRequiredMixin):
    """Staff who also hold `User.is_finance` — gates the Xero integration.
    Strict: admins without the flag are refused too."""

    def dispatch(self, request, *args, **kwargs):
        if not getattr(request.user, "is_finance_member", False):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)
