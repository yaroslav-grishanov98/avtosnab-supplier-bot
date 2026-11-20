"""Response serializers of the accounting integration API. All fields are output-only; no secrets are ever listed."""

from rest_framework import serializers

from accounting.models import Provider, SyncKind, SyncStatus, SyncTrigger
from accounting.status import ConnectionState


class VenueIntegrationStatusSerializer(serializers.Serializer):
    venue_id = serializers.UUIDField(read_only=True, help_text="Заведение, для которого посчитан статус.")
    state = serializers.ChoiceField(
        choices=ConnectionState.choices,
        read_only=True,
        help_text=(
            "Состояние интеграции. not_connected — подключения нет; needs_department_link — подключение есть и каталог "
            "загружен, но администратор не назначил заведению подразделение учётной системы; first_sync_pending — "
            "данных ещё нет, ждём первую синхронизацию; active — остатки свежие; stale — остатки устарели; "
            "needs_attention — подключение остановлено и требует действий администратора (например, пароль не "
            "принят); disconnected — подключение отключено."
        ),
    )
    reason_code = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text=(
            "Машинный код причины для состояний, кроме active и not_connected "
            "(например no_department_assigned, stock_stale, auth_error). null, если причины нет."
        ),
    )
    connection_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="Действующее подключение заведения. null, если его нет."
    )
    provider = serializers.ChoiceField(
        choices=Provider.choices,
        read_only=True,
        allow_null=True,
        help_text="Учётная система подключения: iiko или storehouse. null, если подключения нет.",
    )
    last_success_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="Когда сохранены последние остатки заведения. null, если остатков ещё не было.",
    )
    stale_since = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="С какого момента остатки считаются устаревшими. null, если они не устарели.",
    )
    warehouses_count = serializers.IntegerField(
        read_only=True, help_text="Сколько складов учётной системы относится к заведению."
    )
    can_manage = serializers.BooleanField(
        read_only=True,
        help_text="Может ли пользователь управлять подключением (владелец заведения или администратор платформы).",
    )


class SyncJobSerializer(serializers.Serializer):
    job_id = serializers.UUIDField(read_only=True, help_text="Идентификатор задачи синхронизации.")
    connection_id = serializers.UUIDField(read_only=True, help_text="Подключение, которое синхронизирует задача.")
    kind = serializers.ChoiceField(
        choices=SyncKind.choices,
        read_only=True,
        help_text="Что читает задача: catalog — справочники, stock — остатки, consumption — расход.",
    )
    trigger = serializers.ChoiceField(
        choices=SyncTrigger.choices,
        read_only=True,
        help_text="Кто запустил задачу: schedule — расписание, manual — пользователь, connect — первое подключение.",
    )
    status = serializers.ChoiceField(
        choices=SyncStatus.choices,
        read_only=True,
        help_text=(
            "Состояние задачи: queued — в очереди; running — выполняется; success — готово; partial — готово "
            "частично; failed — не выполнена; skipped — пропущена."
        ),
    )
    error_code = serializers.CharField(
        read_only=True, help_text="Машинный код ошибки или причины пропуска. Пустая строка, если всё хорошо."
    )
    queued_at = serializers.DateTimeField(read_only=True, help_text="Когда задача поставлена в очередь.")
    started_at = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="Когда задача начала выполняться. null, если ещё не начала."
    )
    finished_at = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="Когда задача завершилась. null, если ещё идёт."
    )


class ErrorSerializer(serializers.Serializer):
    detail = serializers.CharField(read_only=True, help_text="Описание ошибки на русском языке.")


class ThrottledErrorSerializer(ErrorSerializer):
    retry_after = serializers.IntegerField(
        read_only=True,
        help_text="Через сколько секунд можно повторить запрос (то же значение в заголовке Retry-After).",
    )
