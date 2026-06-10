from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.utils.translation import gettext_lazy as _

from .models import ApiKey, MagicLink, User


class EmailUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("email",)


class EmailUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    add_form = EmailUserCreationForm
    form = EmailUserChangeForm
    model = User

    ordering = ("email",)
    list_display = ("email", "full_name", "role", "is_active", "is_staff", "date_joined")
    list_filter = ("role", "is_active", "is_staff", "is_superuser")
    search_fields = ("email", "first_name", "last_name", "hubspot_id")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal info"), {"fields": ("first_name", "last_name")}),
        (_("Role"), {"fields": ("role",)}),
        (
            _("Permissions"),
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        (_("Important dates"), {"fields": ("last_login", "date_joined")}),
        (_("HubSpot"), {"fields": ("hubspot_id",)}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "role", "password1", "password2"),
            },
        ),
    )


@admin.register(MagicLink)
class MagicLinkAdmin(admin.ModelAdmin):
    list_display = ("user", "redirect_url", "created_at", "expires_at", "used_at")
    list_select_related = ("user", "created_by")
    list_filter = ("created_at", "expires_at", "used_at")
    search_fields = ("user__email", "token", "redirect_url")
    autocomplete_fields = ("user", "created_by")
    readonly_fields = ("token", "created_at", "used_at", "used_ip")


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("organisation", "name", "prefix", "is_active", "last_used_at", "created_at", "created_by")
    list_filter = ("is_active",)
    list_select_related = ("organisation", "created_by")
    search_fields = ("name", "prefix", "organisation__name")
    autocomplete_fields = ("organisation",)
    readonly_fields = ("prefix", "hashed_key", "created_at", "last_used_at", "created_by")
    fields = ("organisation", "name", "is_active", "prefix", "hashed_key",
              "created_at", "last_used_at", "created_by")

    def get_fields(self, request, obj=None):
        # On the create form, only the integrator's org + a label matter —
        # everything else is generated.
        if obj is None:
            return ("organisation", "name")
        return super().get_fields(request, obj)

    def save_model(self, request, obj, form, change):
        if change:
            return super().save_model(request, obj, form, change)
        # Create path: mint via ApiKey.issue() so the raw key is generated and
        # flashed once. We can't call super().save() — that would persist the
        # half-built `obj` first.
        new_obj, raw_key = ApiKey.issue(
            organisation=obj.organisation, name=obj.name, created_by=request.user,
        )
        # Mutate the in-flight obj so admin's redirect to the change page works.
        obj.pk = new_obj.pk
        obj.prefix = new_obj.prefix
        obj.hashed_key = new_obj.hashed_key
        obj.is_active = new_obj.is_active
        obj.created_at = new_obj.created_at
        obj.created_by = new_obj.created_by
        messages.warning(
            request,
            f"API key for {new_obj.organisation.name} ({new_obj.name}): "
            f"{raw_key} — copy it now, this is the only time it will be shown.",
        )
