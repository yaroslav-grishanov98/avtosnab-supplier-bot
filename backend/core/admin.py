from django import forms
from django.contrib import admin
from django.contrib.auth.hashers import identify_hasher, make_password

from core.models import (
    OffSystemSupplier,
    Order,
    OrderItem,
    OrderTemplate,
    OrderTemplateItem,
    Receiving,
    ReceivingDocument,
    Unit,
    User,
    Venue,
    VenueCart,
    VenueCartItem,
    VenueSubdivision,
)


class UserAdminForm(forms.ModelForm):
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Оставьте пустым, чтобы не менять. Вводимое значение будет захешировано автоматически.",
    )

    class Meta:
        model = User
        fields = "__all__"

    def clean_password(self):
        value = self.cleaned_data.get("password")
        if not value:
            return self.instance.password
        try:
            identify_hasher(value)
        except ValueError:
            return make_password(value)
        return value


class TimestampedAdminMixin:
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Unit)
class UnitAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name", "kind", "ratio_to_base", "status", "updated_at")
    search_fields = ("code", "name")
    list_filter = ("kind", "status")
    ordering = ("code",)


@admin.register(User)
class UserAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    form = UserAdminForm
    list_display = ("email", "username", "full_name", "role", "venue", "status", "updated_at")
    search_fields = ("email", "username", "phone", "full_name", "external_auth_id", "venue__display_name")
    list_filter = ("role", "status")
    autocomplete_fields = ("venue",)
    ordering = ("email",)


@admin.register(Venue)
class VenueAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("display_name", "legal_name", "iiko_org_id", "status", "updated_at")
    search_fields = ("display_name", "legal_name", "iiko_org_id")
    list_filter = ("status",)
    ordering = ("display_name",)


@admin.register(VenueSubdivision)
class VenueSubdivisionAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "venue", "status", "updated_at")
    search_fields = ("name", "venue__display_name")
    list_filter = ("status",)
    ordering = ("venue", "name")


@admin.register(OffSystemSupplier)
class OffSystemSupplierAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("display_name", "venue", "phone", "status", "updated_at")
    search_fields = ("display_name", "venue__display_name", "phone")
    list_filter = ("status",)
    ordering = ("venue", "display_name")


class VenueCartItemInline(admin.TabularInline):
    model = VenueCartItem
    extra = 0
    fields = (
        "requested_name",
        "supplier_product",
        "off_system_supplier",
        "subdivision",
        "requested_qty",
        "requested_unit",
    )
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(VenueCart)
class VenueCartAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("user", "venue", "requested_delivery_date", "updated_at")
    search_fields = ("user__email", "venue__display_name", "comment")
    ordering = ("-updated_at",)
    inlines = (VenueCartItemInline,)


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    fields = (
        "requested_name",
        "supplier_product",
        "off_system_supplier",
        "subdivision",
        "product",
        "requested_qty",
        "requested_unit",
        "confirmed_qty",
        "confirmed_price",
        "status",
    )
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Order)
class OrderAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("order_number", "venue", "status", "requested_delivery_date", "submitted_at", "updated_at")
    search_fields = ("order_number", "venue__display_name", "comment")
    list_filter = ("status", "requested_delivery_date", "submitted_at")
    ordering = ("-created_at",)
    inlines = (OrderItemInline,)


@admin.register(OrderItem)
class OrderItemAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = (
        "requested_name",
        "order",
        "supplier_product",
        "requested_qty",
        "requested_unit",
        "status",
        "updated_at",
    )
    search_fields = (
        "requested_name",
        "order__order_number",
        "supplier_product__supplier_name",
        "product__canonical_name",
    )
    list_filter = ("status",)
    ordering = ("-created_at",)


class OrderTemplateItemInline(admin.TabularInline):
    model = OrderTemplateItem
    extra = 0
    fields = ("requested_name", "supplier_product", "off_system_supplier", "requested_qty", "requested_unit")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(OrderTemplate)
class OrderTemplateAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "venue", "status", "updated_at")
    search_fields = ("name", "venue__display_name")
    list_filter = ("status",)
    ordering = ("venue", "name")
    inlines = (OrderTemplateItemInline,)


class ReceivingDocumentInline(admin.TabularInline):
    model = ReceivingDocument
    extra = 0
    fields = (
        "received_name",
        "supplier_product",
        "product_mapping",
        "received_qty",
        "received_unit",
        "received_price",
        "status",
    )
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Receiving)
class ReceivingAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("venue", "supplier", "order", "status", "received_at", "updated_at")
    search_fields = ("venue__display_name", "supplier__display_name", "order__order_number", "comment")
    list_filter = ("status", "received_at")
    ordering = ("-created_at",)
    inlines = (ReceivingDocumentInline,)


@admin.register(ReceivingDocument)
class ReceivingDocumentAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = (
        "received_name",
        "receiving",
        "supplier_product",
        "received_qty",
        "received_unit",
        "status",
        "updated_at",
    )
    search_fields = ("received_name", "document_number", "supplier_product__supplier_name")
    list_filter = ("status", "document_date")
    ordering = ("-created_at",)
