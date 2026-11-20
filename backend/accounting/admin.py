from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from accounting.models import (
    AccountingConnection,
    AccountingConnectionEvent,
    AccountingDepartment,
    AccountingStockSnapshot,
    AccountingSyncJob,
    AccountingVenueLink,
    AccountingWarehouse,
    SyncKind,
)
from accounting.services import (
    Actor,
    ConnectionBusy,
    ConnectionConflict,
    ConnectionDisabled,
    DepartmentInUse,
    SyncThrottled,
    _check_access_and_capabilities,
    assign_department,
    check_connection_access,
    create_connection,
    disconnect,
    replace_credentials,
    request_manual_sync,
)
from core.models import EntityStatus, Venue


def _report_text(report) -> str:
    parts = [step.message for step in report.steps if step.status == "failed"]
    if report.hint:
        parts.append(report.hint)
    return " ".join(parts) or "Проверка подключения не пройдена."


class ConnectionAdminForm(forms.ModelForm):
    venue = forms.ModelChoiceField(
        queryset=Venue.objects.none(),
        required=False,
        label="Заведение",
        help_text="При создании будет добавлена ожидающая привязка к этому заведению.",
    )
    login = forms.CharField(
        required=False,
        label="Логин iiko",
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    password = forms.CharField(
        required=False,
        label="Пароль iiko",
        widget=forms.PasswordInput(render_value=False),
        help_text="На странице изменения оставьте пустым, чтобы не менять учётные данные.",
    )

    class Meta:
        model = AccountingConnection
        fields = ("provider", "base_url")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["venue"].queryset = Venue.objects.filter(status=EntityStatus.ACTIVE).order_by("display_name")
        self.verified_access = None
        if self.instance.pk:
            self.fields["login"].help_text = "Заполните логин вместе с новым паролем."

    @sensitive_variables()  # `login` and `password` are local variables here
    def clean(self):
        cleaned = super().clean()
        adding = not self.instance.pk
        venue = cleaned.get("venue")
        login = cleaned.get("login") or ""
        password = cleaned.get("password") or ""
        if adding and venue is None:
            self.add_error("venue", "Выберите заведение.")
        if adding and not login:
            self.add_error("login", "Введите логин iiko.")
        if adding and not password:
            self.add_error("password", "Введите пароль iiko.")
        if not adding and bool(login) != bool(password):
            field = "password" if login else "login"
            self.add_error(field, "Для замены учётных данных заполните логин и пароль вместе.")
        if self.errors or not password:
            return cleaned
        report, capabilities = _check_access_and_capabilities(
            cleaned.get("provider") or self.instance.provider,
            cleaned.get("base_url") or self.instance.base_url,
            login,
            password,
        )
        if not report.ok:
            self.add_error(None, _report_text(report))
        else:
            self.verified_access = (report, capabilities)
        return cleaned


class AccountingDepartmentInline(admin.TabularInline):
    model = AccountingDepartment
    fields = ("external_id", "name", "department_type", "venue", "last_seen_at")
    readonly_fields = ("external_id", "name", "department_type", "last_seen_at")
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class AccountingVenueLinkInline(admin.TabularInline):
    model = AccountingVenueLink
    fields = ("venue", "provider", "status", "created_at", "updated_at")
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class AccountingConnectionEventInline(admin.TabularInline):
    model = AccountingConnectionEvent
    fields = ("created_at", "actor_label", "action", "details")
    readonly_fields = fields
    extra = 0
    can_delete = False
    ordering = ("-created_at",)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AccountingConnection)
class AccountingConnectionAdmin(admin.ModelAdmin):
    form = ConnectionAdminForm
    list_display = (
        "linked_venues",
        "provider",
        "host",
        "status",
        "last_success_at",
        "consecutive_failures",
    )
    list_filter = ("provider", "status")
    search_fields = ("host", "venue_links__venue__display_name")
    ordering = ("-created_at",)
    inlines = (AccountingDepartmentInline, AccountingVenueLinkInline, AccountingConnectionEventInline)
    actions = ("check_access_action", "sync_stock_action", "sync_catalog_action", "disconnect_action")

    safe_readonly_fields = (
        "id",
        "host",
        "status",
        "error_code",
        "stale_reason",
        "server_version",
        "capabilities",
        "sync_interval_minutes",
        "last_attempt_at",
        "last_success_at",
        "next_sync_at",
        "catalog_synced_at",
        "consecutive_failures",
        "created_by",
        "created_at",
        "updated_at",
    )
    readonly_fields = safe_readonly_fields

    def get_fields(self, request, obj=None):
        if obj is None:
            return ("venue", "provider", "base_url", "login", "password", *self.safe_readonly_fields)
        return ("provider", "base_url", "login", "password", *self.safe_readonly_fields)

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return self.safe_readonly_fields
        return ("provider", "base_url", *self.safe_readonly_fields)

    def get_inlines(self, request, obj):
        return [] if obj is None else list(super().get_inlines(request, obj))

    @admin.display(description="Заведения")
    def linked_venues(self, obj):
        return ", ".join(obj.venue_links.values_list("venue__display_name", flat=True)) or "—"

    @sensitive_variables()  # `data` holds the plain login and password; error reports must not print them
    def save_model(self, request, obj, form, change):
        data = form.cleaned_data
        actor = Actor(request.user.get_username())
        if not change:
            created = create_connection(
                venue=data["venue"],
                actor=actor,
                provider=data["provider"],
                base_url=data["base_url"],
                login=data["login"],
                password=data["password"],
                _verified=form.verified_access,
            )
            obj.__dict__.update(created.__dict__)
            return
        if data.get("password"):
            replace_credentials(
                obj,
                actor,
                data["login"],
                data["password"],
                _verified=form.verified_access,
            )

    def save_formset(self, request, form, formset, change):
        if formset.model is not AccountingDepartment:
            return super().save_formset(request, form, formset, change)
        # Populate Django admin's changed_objects/new_objects bookkeeping, but
        # keep the actual write in the lifecycle service below.
        formset.save(commit=False)
        actor = Actor(request.user.get_username())
        for department_form in formset.forms:
            if department_form.cleaned_data and "venue" in department_form.changed_data:
                try:
                    assign_department(
                        form.instance,
                        department_form.instance.external_id,
                        department_form.cleaned_data["venue"],
                        actor,
                    )
                except (ConnectionBusy, ConnectionConflict, ConnectionDisabled, DepartmentInUse) as exc:
                    self.message_user(request, f"{department_form.instance.name}: {exc}", messages.ERROR)

    def has_delete_permission(self, request, obj=None):
        return False

    # The iiko login is a credential too (Review Focus 5): error reports must not print it from the POST table.
    @method_decorator(sensitive_post_parameters("login", "password"))
    def add_view(self, request, form_url="", extra_context=None):
        return super().add_view(request, form_url, extra_context)

    @method_decorator(sensitive_post_parameters("login", "password"))
    def change_view(self, request, object_id, form_url="", extra_context=None):
        return super().change_view(request, object_id, form_url, extra_context)

    @admin.action(description="Проверить доступ")
    def check_access_action(self, request, queryset):
        for connection in queryset:
            report = check_connection_access(connection)
            level = messages.SUCCESS if report.ok else messages.ERROR
            self.message_user(
                request, f"{connection}: {_report_text(report) if not report.ok else 'доступ есть.'}", level
            )

    def _sync(self, request, queryset, kind):
        actor = Actor(request.user.get_username())
        for connection in queryset:
            try:
                job = request_manual_sync(connection, actor, kind)
            except SyncThrottled as exc:
                self.message_user(
                    request,
                    f"{connection}: повторите через {exc.retry_after} с.",
                    messages.WARNING,
                )
            except ConnectionDisabled as exc:
                self.message_user(request, f"{connection}: {exc}", messages.ERROR)
            else:
                self.message_user(request, f"{connection}: задача {job.pk} поставлена в очередь.", messages.SUCCESS)

    @admin.action(description="Синхронизировать остатки")
    def sync_stock_action(self, request, queryset):
        self._sync(request, queryset, SyncKind.STOCK)

    @admin.action(description="Обновить каталог")
    def sync_catalog_action(self, request, queryset):
        self._sync(request, queryset, SyncKind.CATALOG)

    @admin.action(description="Отключить")
    def disconnect_action(self, request, queryset):
        actor = Actor(request.user.get_username())
        for connection in queryset:
            disconnect(connection, actor)
        self.message_user(request, "Выбранные подключения отключены.", messages.SUCCESS)


class ReadOnlyAccountingAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.has_perm(f"{self.opts.app_label}.view_{self.opts.model_name}")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AccountingWarehouse)
class AccountingWarehouseAdmin(ReadOnlyAccountingAdmin):
    list_display = ("name", "connection", "venue", "external_id", "is_present_in_source", "last_seen_at")
    list_filter = ("connection", "venue", "is_present_in_source")
    search_fields = ("name", "external_id")
    readonly_fields = tuple(field.name for field in AccountingWarehouse._meta.fields)


@admin.register(AccountingStockSnapshot)
class AccountingStockSnapshotAdmin(ReadOnlyAccountingAdmin):
    list_display = ("warehouse", "connection", "venue", "taken_at", "status", "item_count", "error_count")
    list_filter = ("connection", "venue", "status")
    readonly_fields = tuple(field.name for field in AccountingStockSnapshot._meta.fields)


@admin.register(AccountingSyncJob)
class AccountingSyncJobAdmin(ReadOnlyAccountingAdmin):
    list_display = ("job_id", "connection", "kind", "trigger", "status", "queued_at", "finished_at")
    list_filter = ("connection", "kind", "trigger", "status")
    readonly_fields = tuple(field.name for field in AccountingSyncJob._meta.fields)
