from django.contrib import admin

from .models import Contact, Deal, Document, Organisation, Participation, Quote, Stage


@admin.register(Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    list_display = ("name", "companies_house_number", "hubspot_id", "created_at")
    search_fields = (
        "name",
        "legal_name",
        "trading_name",
        "companies_house_number",
        "hubspot_id",
    )
    readonly_fields = ("created_at", "updated_at")


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("__str__", "email", "user", "hubspot_id", "created_at")
    list_select_related = ("user",)
    list_filter = ("organisations",)
    search_fields = ("first_name", "last_name", "email", "hubspot_id", "organisations__name")
    autocomplete_fields = ("organisations", "user")
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


class DocumentInline(admin.TabularInline):
    model = Document
    extra = 0
    fields = ("name", "required", "status", "file", "uploaded_at", "uploaded_by")
    readonly_fields = ("uploaded_at", "uploaded_by")
    autocomplete_fields = ("uploaded_by",)


class ParticipationInline(admin.TabularInline):
    """Inline editor for Deal.participations — the sum of `amount` is the deal's funded amount."""

    model = Participation
    extra = 0
    fields = ("amount", "organisation")
    autocomplete_fields = ("organisation",)


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "customer", "hubspot_id", "created_at")
    list_select_related = ("owner", "customer", "organisation")
    list_filter = ("owner",)
    search_fields = (
        "name",
        "hubspot_id",
        "customer__first_name",
        "customer__last_name",
        "customer__email",
    )
    autocomplete_fields = ("owner", "customer", "organisation", "introducer")
    readonly_fields = ("created_at", "updated_at")
    inlines = [ParticipationInline, StageInline, QuoteInline, DocumentInline]
    fieldsets = (
        (None, {"fields": ("name", "owner", "customer", "organisation")}),
        ("Associations", {"fields": ("introducer",)}),
        (
            "Other financials",
            {
                "fields": (
                    "earnings",
                    "flat_fee",
                    "commission",
                    "document_fee",
                ),
                "description": (
                    "The deal's funded amount is the sum of its Participations "
                    "(edited inline below)."
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


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("name", "deal", "required", "status", "uploaded_at")
    list_select_related = ("deal",)
    list_filter = ("status", "required")
    search_fields = ("name", "deal__name")
    autocomplete_fields = ("deal", "uploaded_by")
    readonly_fields = ("uploaded_at", "created_at", "updated_at")
