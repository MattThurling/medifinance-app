from django.contrib import admin

from .models import Contact, Deal, Organisation, Quote, Stage


@admin.register(Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    list_display = ("name", "hubspot_id", "created_at")
    search_fields = ("name", "hubspot_id")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("__str__", "email", "organisation", "user", "hubspot_id", "created_at")
    list_select_related = ("organisation", "user")
    list_filter = ("organisation",)
    search_fields = ("first_name", "last_name", "email", "hubspot_id", "organisation__name")
    autocomplete_fields = ("organisation", "user")
    readonly_fields = ("created_at", "updated_at")


class QuoteInline(admin.TabularInline):
    model = Quote
    extra = 0
    readonly_fields = ("created_at", "updated_at")
    fields = ("apr", "term", "monthly_payment", "created_at", "updated_at")


class StageInline(admin.TabularInline):
    model = Stage
    extra = 0
    fields = ("name", "occurred_at", "set_by", "note")
    autocomplete_fields = ("set_by",)
    ordering = ("-occurred_at",)


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "customer", "funded_amount", "hubspot_id", "created_at")
    list_select_related = ("owner", "customer", "customer__organisation")
    list_filter = ("owner",)
    search_fields = (
        "name",
        "hubspot_id",
        "customer__first_name",
        "customer__last_name",
        "customer__email",
    )
    autocomplete_fields = ("owner", "customer", "introducer", "equipment_supplier")
    readonly_fields = ("created_at", "updated_at")
    inlines = [StageInline, QuoteInline]
    fieldsets = (
        (None, {"fields": ("name", "owner", "customer")}),
        ("Associations", {"fields": ("introducer", "equipment_supplier")}),
        (
            "Financials",
            {
                "fields": (
                    "funded_amount",
                    "earnings",
                    "flat_fee",
                    "commission",
                    "document_fee",
                ),
            },
        ),
        ("HubSpot", {"fields": ("hubspot_id",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = ("__str__", "deal", "apr", "term", "monthly_payment", "created_at")
    list_select_related = ("deal",)
    search_fields = ("deal__name",)
    autocomplete_fields = ("deal",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(Stage)
class StageAdmin(admin.ModelAdmin):
    list_display = ("deal", "name", "occurred_at", "set_by")
    list_select_related = ("deal", "set_by")
    list_filter = ("name",)
    search_fields = ("deal__name",)
    autocomplete_fields = ("deal", "set_by")
    readonly_fields = ("created_at", "updated_at")
