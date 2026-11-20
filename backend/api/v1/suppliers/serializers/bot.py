"""Telegram bot user binding, pending actions and state."""

from rest_framework import serializers

from core.models import EntityStatus
from suppliers.models import (
    BotPendingAction,
    BotUserBinding,
    BotUserState,
    SourceChannel,
    Supplier,
)


class BotUserBindingResolveQuerySerializer(serializers.Serializer):
    platform = serializers.ChoiceField(
        choices=SourceChannel.values, required=False, help_text="Канал бота: telegram / max (либо укажите channel)."
    )
    channel = serializers.ChoiceField(
        choices=SourceChannel.values, required=False, help_text="Синоним platform, для совместимости с интеграциями."
    )
    telegram_user_id = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="ID пользователя в Telegram/Max."
    )
    chat_id = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="ID чата с ботом."
    )

    def validate(self, attrs):
        platform = attrs.get("platform") or attrs.get("channel")
        if not platform:
            raise serializers.ValidationError("platform is required.")
        if not attrs.get("chat_id") and not attrs.get("telegram_user_id"):
            raise serializers.ValidationError("chat_id or telegram_user_id is required.")
        attrs["platform"] = platform
        return attrs


class BotUserBindingResolveSerializer(serializers.ModelSerializer):
    supplier_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="ID выбранной компании, либо null сразу после привязки аккаунта.",
    )
    supplier_name = serializers.CharField(
        source="supplier.display_name",
        read_only=True,
        allow_null=True,
        help_text="Название выбранной компании, либо null.",
    )

    class Meta:
        model = BotUserBinding
        fields = (
            "id",
            "supplier_id",
            "supplier_name",
            "channel",
            "chat_id",
            "external_user_id",
            "username",
            "status",
        )
        extra_kwargs = {
            "id": {"help_text": "ID привязки бота к поставщику."},
            "channel": {"help_text": "Канал бота: telegram / max."},
            "chat_id": {"help_text": "ID чата с ботом."},
            "external_user_id": {"help_text": "ID пользователя в Telegram/Max."},
            "username": {"help_text": "Username пользователя в мессенджере, если есть."},
            "status": {"help_text": "Статус привязки: active / inactive."},
        }


class BotUserBindingEnvelopeSerializer(serializers.Serializer):
    exists = serializers.BooleanField(help_text="Найдена ли привязка бота по запросу.")
    binding = BotUserBindingResolveSerializer(allow_null=True, help_text="Найденная привязка, либо null.")


class BotUserBindingRegisterSerializer(serializers.Serializer):
    platform = serializers.ChoiceField(choices=SourceChannel.values, help_text="Канал бота: telegram / max.")
    telegram_user_id = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="ID пользователя в Telegram/Max."
    )
    chat_id = serializers.CharField(trim_whitespace=True, help_text="ID чата с ботом.")
    username = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Username пользователя в мессенджере."
    )
    first_name = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Имя пользователя."
    )
    last_name = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Фамилия пользователя."
    )
    link_token = serializers.CharField(
        trim_whitespace=True,
        help_text='Токен из deep-link кнопки "Присоединиться к Telegram-боту" в ЛК (start-параметр /start).',
    )


class BotPendingActionSerializer(serializers.ModelSerializer):
    supplier_id = serializers.UUIDField(read_only=True, allow_null=True, help_text="ID поставщика, если уже известен.")

    class Meta:
        model = BotPendingAction
        fields = (
            "id",
            "supplier_id",
            "platform",
            "telegram_user_id",
            "chat_id",
            "action_type",
            "file_id",
            "file_name",
            "mime_type",
            "message_id",
            "status",
            "raw_payload",
            "created_at",
            "updated_at",
            "expires_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")
        extra_kwargs = {
            "platform": {"help_text": "Канал бота: telegram / max."},
            "telegram_user_id": {"help_text": "ID пользователя в Telegram/Max."},
            "chat_id": {"help_text": "ID чата с ботом."},
            "action_type": {"help_text": "Тип ожидаемого действия, например upload_price."},
            "file_id": {"help_text": "ID файла в Telegram/Max, если действие про файл."},
            "file_name": {"help_text": "Имя файла, если действие про файл."},
            "mime_type": {"help_text": "MIME-тип файла, если действие про файл."},
            "message_id": {"help_text": "ID сообщения, к которому относится действие."},
            "status": {
                "help_text": "Статус действия: pending_registration / waiting_registration / ready_for_processing и т.п."
            },
            "raw_payload": {"help_text": "Произвольные данные действия."},
            "expires_at": {"help_text": "Когда действие перестаёт быть актуальным."},
        }


class BotPendingActionCreateSerializer(serializers.Serializer):
    platform = serializers.ChoiceField(choices=SourceChannel.values, help_text="Канал бота: telegram / max.")
    telegram_user_id = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="ID пользователя в Telegram/Max."
    )
    chat_id = serializers.CharField(trim_whitespace=True, help_text="ID чата с ботом.")
    action_type = serializers.CharField(
        trim_whitespace=True, max_length=64, help_text="Тип ожидаемого действия, например upload_price."
    )
    file_id = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="ID файла в Telegram/Max, если действие про файл.",
    )
    file_name = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Имя файла, если действие про файл."
    )
    mime_type = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="MIME-тип файла, если действие про файл."
    )
    message_id = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="ID сообщения, к которому относится действие."
    )
    status = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=64,
        help_text="Начальный статус действия. По умолчанию pending_registration.",
    )
    raw_payload = serializers.JSONField(required=False, help_text="Произвольные данные действия.")
    expires_at = serializers.DateTimeField(
        required=False, allow_null=True, help_text="Когда действие перестаёт быть актуальным."
    )


class BotPendingActionUpdateSerializer(serializers.Serializer):
    status = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, max_length=64, help_text="Новый статус действия."
    )
    supplier_id = serializers.PrimaryKeyRelatedField(
        queryset=Supplier.objects.filter(status=EntityStatus.ACTIVE),
        source="supplier",
        required=False,
        allow_null=True,
        help_text="ID поставщика, к которому теперь привязано действие.",
    )
    raw_payload = serializers.JSONField(required=False, help_text="Обновлённые произвольные данные действия.")


class BotUserStateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BotUserState
        fields = (
            "id",
            "platform",
            "telegram_user_id",
            "chat_id",
            "state",
            "payload",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")
        extra_kwargs = {
            "platform": {"help_text": "Канал бота: telegram / max."},
            "telegram_user_id": {"help_text": "ID пользователя в Telegram/Max."},
            "chat_id": {"help_text": "ID чата с ботом."},
            "state": {"help_text": "Текущее состояние диалога бота с пользователем."},
            "payload": {"help_text": "Данные текущего состояния (например, код ошибки регистрации)."},
        }


class BotUserStateUpdateSerializer(serializers.Serializer):
    platform = serializers.ChoiceField(choices=SourceChannel.values, help_text="Канал бота: telegram / max.")
    telegram_user_id = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="ID пользователя в Telegram/Max."
    )
    chat_id = serializers.CharField(trim_whitespace=True, help_text="ID чата с ботом.")
    state = serializers.ChoiceField(
        choices=(
            "start",
            "bot_link_invalid",
            "supplier_registered",
            "registration_cancelled",
            "pending_price_upload",
            "price_processing",
            "price_processed",
            "error",
            "idle",
            "awaiting_upload_confirmation",
            "registered",
        ),
        help_text="Новое состояние диалога бота с пользователем.",
    )
    payload = serializers.JSONField(required=False, help_text="Данные нового состояния.")
