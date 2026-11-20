import logging

from django.conf import settings
from django.contrib import admin
from django.core.mail import send_mail
from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from core.models import User, UserRole
from suppliers.models import (
    BotPendingAction,
    BotUserBinding,
    BotUserState,
    Supplier,
    SupplierOwnershipRequest,
    SupplierPriceUpload,
    SupplierPriceUploadItem,
    SupplierProduct,
    SupplierProductImportItem,
    SupplierProductImportProcessingStatus,
    SupplierProductImportSession,
    SupplierProductImportSessionStatus,
    SupplierProductPrice,
    SupplierUser,
    SupplierUserInvite,
    SupplierUserInviteStatus,
    SupplierUserRole,
    UploadStatus,
    WorkflowEvent,
)


class TimestampedAdminMixin:
    readonly_fields = ("id", "created_at", "updated_at")


class SupplierProductImportItemInline(admin.TabularInline):
    model = SupplierProductImportItem
    extra = 0
    fields = ("row_number", "raw_name", "name", "action", "target_supplier_product")
    readonly_fields = fields
    can_delete = False
    show_change_link = False


def _import_session_complete_q() -> Q:
    """Сессия загружена на 100%: тот же расчёт, что у `percent` в API (rows_done/rows_total; при rows_total=0 —
    100% только у ready)."""
    return Q(rows_total__gt=0, rows_done__gte=F("rows_total")) | Q(
        rows_total=0, processing_status=SupplierProductImportProcessingStatus.READY
    )


class ImportSessionCompletenessFilter(admin.SimpleListFilter):
    title = "процент загрузки"
    parameter_name = "completeness"

    def lookups(self, request, model_admin):
        return [("full", "Загружено на 100%"), ("partial", "Не 100%")]

    def queryset(self, request, queryset):
        if self.value() == "full":
            return queryset.filter(_import_session_complete_q())
        if self.value() == "partial":
            return queryset.exclude(_import_session_complete_q())
        return queryset


@admin.register(SupplierProductImportSession)
class SupplierProductImportSessionAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "supplier",
        "status",
        "processing_status",
        "percent",
        "source_file_name",
        "content_sha256",
        "created_at",
    )
    list_filter = ("status", "processing_status", ImportSessionCompletenessFilter, "supplier")
    search_fields = ("id__exact", "supplier__display_name", "source_file_name", "content_sha256")
    ordering = ("-created_at",)
    inlines = [SupplierProductImportItemInline]
    actions = ["delete_duplicate_open_sessions", "delete_incomplete_open_sessions"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="% загрузки")
    def percent(self, obj) -> str:
        if not obj.rows_total:
            done = obj.processing_status == SupplierProductImportProcessingStatus.READY
            return "100%" if done else "0%"
        return f"{min(100, obj.rows_done * 100 // obj.rows_total)}%"

    @admin.action(description="Удалить дубли среди выбранных открытых (по хэшу файла; оставить одну)")
    def delete_duplicate_open_sessions(self, request, queryset):
        # Дубль = тот же поставщик и тот же sha256 файла (если хэша нет — то же имя файла). Из группы остаётся
        # одна сессия: предпочтительно загруженная на 100%, среди таких — самая свежая. Завершённые (committed)
        # сессии не трогаем.
        sessions = list(queryset.filter(status=SupplierProductImportSessionStatus.OPEN))
        complete_ids = set(
            SupplierProductImportSession.objects.filter(pk__in=[x.pk for x in sessions])
            .filter(_import_session_complete_q())
            .values_list("pk", flat=True)
        )
        groups = {}
        for session in sessions:
            key = (session.supplier_id, session.content_sha256 or f"name:{session.source_file_name}")
            groups.setdefault(key, []).append(session)
        delete_ids = []
        for group in groups.values():
            group.sort(key=lambda x: (x.pk in complete_ids, x.created_at), reverse=True)
            delete_ids.extend(x.pk for x in group[1:])
        deleted = self._delete_sessions(delete_ids)
        self.message_user(request, f"Удалено дублей: {deleted}. Осталось уникальных: {len(groups)}.")

    @admin.action(description="Удалить выбранные открытые сессии, загруженные не на 100%%")
    def delete_incomplete_open_sessions(self, request, queryset):
        ids = list(
            queryset.filter(status=SupplierProductImportSessionStatus.OPEN)
            .exclude(_import_session_complete_q())
            .values_list("pk", flat=True)
        )
        self.message_user(request, f"Удалено незавершённых: {self._delete_sessions(ids)}.")

    @staticmethod
    def _delete_sessions(ids) -> int:
        if not ids:
            return 0
        with transaction.atomic():
            SupplierProductImportSession.objects.filter(pk__in=ids).delete()
        return len(ids)


class SupplierProductPriceInline(admin.TabularInline):
    model = SupplierProductPrice
    extra = 0
    fields = ("price", "currency", "status", "valid_from", "valid_to", "upload_item")
    # 2026-08-16: raw_id_fields — without it, Django renders upload_item as a
    # plain <select> populated with EVERY SupplierPriceUploadItem row in the
    # database (one full-table query per inline row, no pagination). Live
    # incident: the equivalent inline on SupplierPriceUploadAdmin (since
    # removed — see its own comment) did this 834 times on one page and
    # produced a 7MB+ response that exceeded nginx's client timeout.
    raw_id_fields = ("upload_item",)
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("-valid_from",)


@admin.register(Supplier)
class SupplierAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("display_name", "legal_name", "inn", "email", "phone", "status", "updated_at")
    search_fields = ("display_name", "legal_name", "inn", "email", "phone")
    list_filter = ("status",)
    ordering = ("display_name",)


@admin.register(SupplierUser)
class SupplierUserAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("supplier", "user", "role", "status", "updated_at")
    search_fields = ("supplier__display_name", "user__email", "user__full_name")
    list_filter = ("role", "status")
    ordering = ("supplier__display_name", "user__email")


@admin.register(BotUserBinding)
class BotUserBindingAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("supplier", "channel", "chat_id", "external_user_id", "username", "status", "updated_at")
    search_fields = ("supplier__display_name", "chat_id", "external_user_id", "username")
    list_filter = ("channel", "status")
    ordering = ("supplier__display_name", "channel", "chat_id")


@admin.register(BotPendingAction)
class BotPendingActionAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = (
        "platform",
        "chat_id",
        "telegram_user_id",
        "supplier",
        "action_type",
        "file_name",
        "status",
        "updated_at",
    )
    search_fields = ("chat_id", "telegram_user_id", "file_id", "file_name", "message_id", "supplier__display_name")
    list_filter = ("platform", "action_type", "status", "supplier")
    ordering = ("-created_at",)


@admin.register(WorkflowEvent)
class WorkflowEventAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = (
        "event_type",
        "user_id",
        "chat_id",
        "supplier",
        "upload_id",
        "state_before",
        "state_after",
        "created_at",
    )
    search_fields = ("event_type", "user_id", "chat_id", "error_message")
    list_filter = ("event_type", "state_after")
    ordering = ("-created_at",)


@admin.register(BotUserState)
class BotUserStateAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("platform", "chat_id", "telegram_user_id", "state", "updated_at")
    search_fields = ("chat_id", "telegram_user_id")
    list_filter = ("platform", "state")
    ordering = ("-updated_at",)


@admin.register(SupplierPriceUpload)
class SupplierPriceUploadAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("supplier", "source_channel", "source_file_name", "status", "uploaded_at", "processed_at")
    search_fields = ("supplier__display_name", "source_file_name", "source_file_uri", "error_message")
    list_filter = ("source_channel", "status", "uploaded_at", "processed_at")
    readonly_fields = TimestampedAdminMixin.readonly_fields + ("uploaded_at",)
    ordering = ("-uploaded_at",)
    # 2026-08-16: SupplierPriceUploadItemInline dropped — even with
    # raw_id_fields (no more per-row full-table query), TabularInline still
    # renders every existing item as its own <tr> with no pagination. A
    # real 834-item upload produced a 7MB+ change page that took longer
    # than nginx's 90s client timeout to finish rendering (504, even though
    # gunicorn eventually completed within its own 120s) — live incident
    # blocked Витя from even opening the upload in /admin/. Items are still
    # browsable, paginated, via the separately registered
    # SupplierPriceUploadItemAdmin below.
    actions = ["reset_stuck_processing", "fast_delete_bypassing_orm_cascade"]

    # 2026-08-16: same incident, next layer — even with the inline gone, the
    # DELETE confirmation page (both the individual /delete/ view and the
    # "delete selected" bulk action) independently walks the whole cascade
    # via NestedObjects.collect() to build the "this will also delete..."
    # preview. Skipping just the per-object str() formatting (first attempt)
    # wasn't enough on real data (834 items + 834 prices) — collect() itself
    # is pure-Python graph-walking that got slow enough gunicorn's worker
    # didn't even respond to SIGABRT and needed SIGKILL (no traceback
    # logged — it wasn't blocked on I/O, it was CPU-bound). Below a
    # threshold, defer to Django's normal (accurate, per-object) preview —
    # above it, skip NestedObjects entirely and report counts from two
    # direct, targeted queries instead (the only two relations that matter
    # here). Actual deletion still goes through obj.delete() below this
    # threshold; above it, use fast_delete_bypassing_orm_cascade instead —
    # see its own docstring for why obj.delete() itself has the same
    # collect()-cost problem, not just the preview.
    DELETE_PREVIEW_MAX_RELATED_OBJECTS = 200

    def get_deleted_objects(self, objs, request):
        upload_ids = [obj.pk for obj in objs]
        if not upload_ids:
            return [], {}, set(), []
        item_count = SupplierPriceUploadItem.objects.filter(upload_id__in=upload_ids).count()
        if item_count <= self.DELETE_PREVIEW_MAX_RELATED_OBJECTS:
            return super().get_deleted_objects(objs, request)

        price_count = SupplierProductPrice.objects.filter(upload_item__upload_id__in=upload_ids).count()
        model_count = {
            SupplierPriceUploadItem._meta.verbose_name_plural: item_count,
            SupplierProductPrice._meta.verbose_name_plural: price_count,
        }
        to_delete = [
            f"{count} × {name} — точный обход графа и детальный список пропущены (слишком много "
            f"объектов, выше {self.DELETE_PREVIEW_MAX_RELATED_OBJECTS}). Используйте действие "
            f"«Быстро удалить» вместо стандартной кнопки Delete — она тоже упадёт по таймауту."
            for name, count in model_count.items()
        ]
        return to_delete, model_count, set(), []

    @admin.action(
        description=(
            "Быстро удалить (для крупных загрузок — минует построение ORM-графа каскада, "
            "полагается на ON DELETE CASCADE/SET NULL самой БД; без подтверждения — необратимо)"
        )
    )
    def fast_delete_bypassing_orm_cascade(self, request, queryset):
        # 2026-08-16: standard admin delete — both the confirmation preview
        # (get_deleted_objects above) and obj.delete() itself — walks the FK
        # graph in Python via Collector.collect(). For a real 834-item
        # upload that's slow enough to hang the whole gunicorn worker past
        # even its own 120s timeout, with no clean traceback (CPU-bound, not
        # blocked on I/O, so SIGABRT didn't interrupt it either).
        #
        # IMPORTANT: a first version of this tried a plain
        # `DELETE FROM supplier_price_uploads ...` assuming Postgres's own FK
        # constraints would cascade natively — wrong, and caught by
        # SupplierPriceUploadAdminFastDeleteTests before shipping. Django's
        # on_delete (CASCADE/SET_NULL/...) is enforced ENTIRELY at the ORM
        # level; the actual DB constraints it creates are plain NO ACTION
        # (confirmed via pg_constraint.confdeltype on this schema) — a raw
        # DELETE with no ORM involvement leaves every dependent row exactly
        # as it was, orphaned or pointing at a deleted id. So this manually
        # replicates the current on_delete graph for exactly these two
        # models instead (six statements, still ~6 queries total regardless
        # of row count, vs. one Python Collector.collect() call per
        # dependent object):
        #   supplier_products.last_upload        -> SET_NULL
        #   google_sheet_sync_logs.upload         -> SET_NULL
        #   product_mappings.source_upload_item   -> SET_NULL
        #   supplier_product_prices.upload_item   -> SET_NULL
        #   supplier_price_upload_items.upload    -> CASCADE
        # SupplierPriceUploadAdminFastDeleteTests.test_covers_every_fk_pointing_at_these_models
        # cross-checks this list against Django's model graph so schema
        # drift (a new FK added later) fails loudly instead of silently
        # leaving orphaned/stale references.
        upload_ids = list(queryset.values_list("pk", flat=True))
        if not upload_ids:
            self.message_user(request, "Нечего удалять — пустой выбор.")
            return
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE supplier_products SET last_upload_id = NULL WHERE last_upload_id = ANY(%s)",
                [upload_ids],
            )
            cursor.execute(
                "UPDATE google_sheet_sync_logs SET upload_id = NULL WHERE upload_id = ANY(%s)",
                [upload_ids],
            )
            cursor.execute(
                """
                UPDATE product_mappings SET source_upload_item_id = NULL
                WHERE source_upload_item_id IN (
                    SELECT id FROM supplier_price_upload_items WHERE upload_id = ANY(%s)
                )
                """,
                [upload_ids],
            )
            cursor.execute(
                """
                UPDATE supplier_product_prices SET upload_item_id = NULL
                WHERE upload_item_id IN (
                    SELECT id FROM supplier_price_upload_items WHERE upload_id = ANY(%s)
                )
                """,
                [upload_ids],
            )
            cursor.execute("DELETE FROM supplier_price_upload_items WHERE upload_id = ANY(%s)", [upload_ids])
            cursor.execute("DELETE FROM supplier_price_uploads WHERE id = ANY(%s)", [upload_ids])
        self.message_user(request, f"Удалено загрузок: {len(upload_ids)} (каскад реплицирован вручную).")

    @admin.action(
        description=(
            "Сбросить зависший 'Processing' в 'Failed' (для загрузок, у которых воркер, "
            "выполнявший нормализацию, упал/был убит по таймауту — status=processing "
            "остаётся навсегда, normalize() всегда отвечает 409, force=true не помогает: "
            "код разрешает force только для failed/applied/needs_review, не для processing)"
        )
    )
    def reset_stuck_processing(self, request, queryset):
        updated = 0
        for upload in queryset.filter(status=UploadStatus.PROCESSING):
            with transaction.atomic():
                locked = SupplierPriceUpload.objects.select_for_update().get(pk=upload.pk)
                if locked.status != UploadStatus.PROCESSING:
                    continue
                locked.status = UploadStatus.FAILED
                locked.error_message = (
                    "Сброшено вручную из админки 2026-08-15 — воркер нормализации аварийно "
                    "завершился (gunicorn worker timeout), запись осталась в processing."
                )
                raw_payload = locked.raw_payload or {}
                raw_payload["normalization"] = {
                    **raw_payload.get("normalization", {}),
                    "status": UploadStatus.FAILED,
                    "finished_at": timezone.now().isoformat(),
                    "error": "manually reset from stuck processing state (admin action)",
                }
                locked.raw_payload = raw_payload
                locked.save(update_fields=["status", "error_message", "raw_payload", "updated_at"])
                updated += 1
        self.message_user(request, f"Сброшено записей: {updated}. Теперь normalize(force=true) можно вызвать снова.")


@admin.register(SupplierPriceUploadItem)
class SupplierPriceUploadItemAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("raw_name", "upload", "supplier_product", "parsed_price", "parsed_unit", "status", "updated_at")
    search_fields = ("raw_name", "normalized_name", "upload__supplier__display_name", "supplier_product__supplier_name")
    list_filter = ("status", "parsed_unit")
    ordering = ("-created_at",)


@admin.register(SupplierProduct)
class SupplierProductAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("supplier_name", "supplier", "normalized_name", "product", "unit", "status", "updated_at")
    search_fields = (
        "supplier_name",
        "normalized_name",
        "supplier_sku",
        "supplier__display_name",
        "product__canonical_name",
    )
    list_filter = ("supplier", "unit", "status")
    ordering = ("supplier__display_name", "normalized_name")
    inlines = (SupplierProductPriceInline,)
    actions = ["delete_products_completely"]

    @admin.action(description="Удалить выбранные товары полностью (с ценами, избранным и привязками к заведениям)")
    def delete_products_completely(self, request, queryset):
        # Обычное удаление упирается в RESTRICT (цены, цены заведений, привязки к заведениям, избранное) --
        # здесь зависимости удаляются явно и безвозвратно; только по запросу из админки.
        from catalog.models import SupplierProductVenuePrice, VenueSupplierProduct, WishlistItem

        ids = list(queryset.values_list("pk", flat=True))
        with transaction.atomic():
            for model in (SupplierProductPrice, SupplierProductVenuePrice, VenueSupplierProduct, WishlistItem):
                model.objects.filter(supplier_product_id__in=ids).delete()
            SupplierProduct.objects.filter(pk__in=ids).delete()
        self.message_user(request, f"Удалено товаров: {len(ids)}.")


@admin.register(SupplierProductPrice)
class SupplierProductPriceAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("supplier_product", "price", "currency", "status", "valid_from", "valid_to")
    search_fields = ("supplier_product__supplier_name", "supplier_product__normalized_name")
    list_filter = ("currency", "status", "valid_from", "valid_to")
    ordering = ("-valid_from",)


logger = logging.getLogger(__name__)


def _send_claim_decision_email(obj):
    if obj.status == "approved":
        subject = f"[АвтоСнаб] Заявка одобрена — {obj.supplier.display_name}"
        message = (
            f"Здравствуйте, {obj.full_name}!\n\n"
            f"Ваша заявка на компанию «{obj.supplier.display_name}» одобрена.\n"
            f"Мы свяжемся с вами для передачи доступа.\n\n"
            f"— Команда АвтоСнаб"
        )
    elif obj.status == "rejected":
        subject = f"[АвтоСнаб] Заявка отклонена — {obj.supplier.display_name}"
        message = (
            f"Здравствуйте, {obj.full_name}!\n\n"
            f"К сожалению, ваша заявка на компанию «{obj.supplier.display_name}» отклонена.\n"
            f"Если у вас есть вопросы — напишите нам в ответ на это письмо.\n\n"
            f"— Команда АвтоСнаб"
        )
    else:
        return
    try:
        send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL,
            [obj.email],
            fail_silently=True,
        )
    except Exception:
        logger.exception("Failed to send claim decision email to %s", obj.email)


_CLAIM_ROLE_TO_SUPPLIER_ROLE = {
    "owner": SupplierUserRole.OWNER,
    "add_company": SupplierUserRole.OWNER,
    "employee": SupplierUserRole.MANAGER,
}


def _provision_supplier_user(obj):
    supplier_role = _CLAIM_ROLE_TO_SUPPLIER_ROLE.get(obj.role_requested)
    if not supplier_role:
        return  # support disputes don't grant access automatically
    # role в lookup, не только defaults (2026-08-06) — email уникален по
    # (email, role), не глобально: отдельный venue_user-аккаунт с тем же
    # email не должен мешать созданию/поиску supplier-аккаунта.
    user, _ = User.objects.get_or_create(
        email=obj.email,
        role=UserRole.SUPPLIER,
        defaults={"full_name": obj.full_name},
    )
    SupplierUser.objects.get_or_create(
        supplier=obj.supplier,
        user=user,
        defaults={"role": supplier_role, "status": "active"},
    )


@admin.action(description="Одобрить выбранные заявки")
def approve_ownership_requests(modeladmin, request, queryset):
    for obj in queryset.exclude(status="approved"):
        obj.status = "approved"
        obj.save(update_fields=["status", "updated_at"])
        _provision_supplier_user(obj)
        _send_claim_decision_email(obj)


@admin.action(description="Отклонить выбранные заявки")
def reject_ownership_requests(modeladmin, request, queryset):
    for obj in queryset.exclude(status="rejected"):
        obj.status = "rejected"
        obj.save(update_fields=["status", "updated_at"])
        _send_claim_decision_email(obj)


@admin.register(SupplierOwnershipRequest)
class SupplierOwnershipRequestAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("supplier", "full_name", "email", "inn", "role_requested", "status", "created_at")
    search_fields = ("supplier__display_name", "full_name", "email", "inn", "phone")
    list_filter = ("status", "role_requested")
    ordering = ("-created_at",)
    readonly_fields = TimestampedAdminMixin.readonly_fields
    actions = [approve_ownership_requests, reject_ownership_requests]


@admin.action(description="Отменить выбранные приглашения")
def cancel_supplier_user_invites(modeladmin, request, queryset):
    queryset.filter(status=SupplierUserInviteStatus.PENDING).update(status=SupplierUserInviteStatus.CANCELLED)


@admin.register(SupplierUserInvite)
class SupplierUserInviteAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    """2026-08-07: не было ни одной ручки/UI, чтобы отменить уже отправленное
    приглашение (только замена новым приглашением на тот же email тихо
    отменяет старое, см. invite_supplier_user) — Витя уткнулся в это, отменяя
    приглашение вручную через админку. status=cancelled на модели уже
    существовал, просто ничего его так не выставляло."""

    list_display = ("supplier", "target_email", "role", "status", "expires_at", "created_at")
    search_fields = ("supplier__display_name", "target_email")
    list_filter = ("status", "role")
    ordering = ("-created_at",)
    readonly_fields = TimestampedAdminMixin.readonly_fields
    actions = [cancel_supplier_user_invites]
