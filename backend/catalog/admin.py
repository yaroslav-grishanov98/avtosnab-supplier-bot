from django.contrib import admin
from django.db import transaction
from django.utils import timezone

from catalog.models import (
    Brand,
    GoogleSheetSyncLog,
    MasterProductImportUpload,
    MasterProductImportUploadStatus,
    Product,
    ProductCategory,
    ProductEmbedding,
    ProductMapping,
    VenueProductMatrixItem,
    VenueSupplierProduct,
)


class TimestampedAdminMixin:
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Brand)
class BrandAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("canonical_name", "normalized_name", "status", "updated_at")
    search_fields = ("canonical_name", "normalized_name")
    list_filter = ("status",)
    ordering = ("canonical_name",)


@admin.register(ProductCategory)
class ProductCategoryAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "slug", "parent", "status", "updated_at")
    search_fields = ("name", "slug", "parent__name")
    list_filter = ("status", "parent")
    ordering = ("name",)


@admin.register(Product)
class ProductAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("canonical_name", "normalized_name", "brand", "category", "base_unit", "status", "updated_at")
    search_fields = ("canonical_name", "normalized_name", "enriched_text", "brand__canonical_name", "category__name")
    list_filter = ("brand", "category", "base_unit", "status")
    ordering = ("canonical_name",)


@admin.register(ProductEmbedding)
class ProductEmbeddingAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("product", "provider", "model", "external_vector_id", "status", "updated_at")
    search_fields = ("product__canonical_name", "model", "source_text", "external_vector_id")
    list_filter = ("provider", "status")
    readonly_fields = TimestampedAdminMixin.readonly_fields + ("embedding",)
    ordering = ("product__canonical_name", "provider", "model")


@admin.register(ProductMapping)
class ProductMappingAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = (
        "supplier_product",
        "product",
        "venue",
        "external_system",
        "scope",
        "status",
        "confidence",
        "updated_at",
    )
    search_fields = (
        "supplier_product__supplier_name",
        "product__canonical_name",
        "venue__display_name",
        "external_product_id",
        "external_product_name",
    )
    list_filter = ("external_system", "scope", "status", "suggested_by")
    ordering = ("-created_at",)


@admin.register(VenueSupplierProduct)
class VenueSupplierProductAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("venue", "supplier_product", "role", "is_favorite", "is_available", "status", "updated_at")
    search_fields = ("venue__display_name", "supplier_product__supplier_name", "custom_name")
    list_filter = ("role", "is_favorite", "is_available", "status")
    ordering = ("venue__display_name", "supplier_product__supplier_name")


@admin.register(VenueProductMatrixItem)
class VenueProductMatrixItemAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("requested_name", "venue", "requested_unit_label", "supplier_product", "status", "updated_at")
    search_fields = ("requested_name", "venue__display_name", "supplier_product__supplier_name")
    list_filter = ("status",)
    ordering = ("venue__display_name", "requested_name")


@admin.register(GoogleSheetSyncLog)
class GoogleSheetSyncLogAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("sheet_name", "spreadsheet_id", "status", "trigger", "rows_written", "started_at", "finished_at")
    search_fields = ("spreadsheet_id", "sheet_name", "n8n_execution_id", "error_message")
    list_filter = ("status", "trigger", "sheet_name")
    ordering = ("-started_at",)


@admin.register(MasterProductImportUpload)
class MasterProductImportUploadAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("id", "owner", "status", "rows_done", "rows_total", "created_at")
    list_filter = ("status",)
    search_fields = ("owner__email", "owner__full_name")
    readonly_fields = ("id", "created_at", "updated_at", "result")
    actions = ["reset_stuck_processing"]

    @admin.action(description="Сбросить зависший processing в failed (воркер упал посреди обработки)")
    def reset_stuck_processing(self, request, queryset):
        updated = 0
        for upload in queryset.filter(status=MasterProductImportUploadStatus.PROCESSING):
            with transaction.atomic():
                locked = MasterProductImportUpload.objects.select_for_update().get(pk=upload.pk)
                if locked.status != MasterProductImportUploadStatus.PROCESSING:
                    continue
                locked.status = MasterProductImportUploadStatus.FAILED
                locked.error_message = (
                    "Сброшено вручную из админки — воркер обработки аварийно завершился "
                    "(таймаут/падение), запись осталась в processing."
                )
                locked.processed_at = timezone.now()
                locked.save(update_fields=["status", "error_message", "processed_at", "updated_at"])
                updated += 1
        self.message_user(request, f"Сброшено записей: {updated}.")
