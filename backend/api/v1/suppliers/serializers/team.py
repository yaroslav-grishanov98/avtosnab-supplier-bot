"""Supplier team: members, invites, invite links, join requests, account transfer."""

from typing import Optional

from django.conf import settings
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.models import EntityStatus
from suppliers.models import (
    SupplierJoinRequestStatus,
    SupplierUserInviteStatus,
    SupplierUserRole,
)


class SupplierAccountTransferInitiateSerializer(serializers.Serializer):
    target_email = serializers.EmailField(help_text="Email реального поставщика, которому передаётся аккаунт.")
    initiated_by_user_id = serializers.UUIDField(
        required=False,
        help_text="ID инициатора передачи. Не нужен при вызове с JWT — берётся из токена автоматически.",
    )


class SupplierAccountTransferClaimSerializer(serializers.Serializer):
    new_password = serializers.CharField(
        min_length=8,
        trim_whitespace=False,
        write_only=True,
        help_text="Пароль для нового владельца, минимум 8 символов.",
    )
    confirm_password = serializers.CharField(
        min_length=8, trim_whitespace=False, write_only=True, help_text="Повтор пароля — должен совпадать."
    )

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "Пароли не совпадают."})
        return attrs


class SupplierUserSerializer(serializers.Serializer):
    """2026-08-01: "кто сейчас имеет доступ к компании" — список активных
    SupplierUser, аналог диалога "Поделиться доступом" в Google Docs."""

    id = serializers.UUIDField(read_only=True, help_text="ID членства (SupplierUser), используется для revoke.")
    user_id = serializers.UUIDField(source="user.id", read_only=True, help_text="ID core.User.")
    email = serializers.EmailField(source="user.email", read_only=True, help_text="Email участника.")
    full_name = serializers.CharField(source="user.full_name", read_only=True, help_text="ФИО участника.")
    role = serializers.ChoiceField(
        choices=SupplierUserRole.choices,
        read_only=True,
        help_text=(
            "owner / manager — права одинаковые, owner нужен только для правила «у компании ровно один активный owner»."
        ),
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Когда получил доступ.")


class SupplierUserRoleChangeSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=SupplierUserRole.choices, help_text="Новая роль участника.")


class SupplierUserInviteSerializer(serializers.Serializer):
    """2026-08-03: "кто ещё не принял приглашение" — pending email-invites,
    отдельно от join-requests/pending (заявки от людей, нашедших компанию
    самостоятельно по ИНН, не приглашённых владельцем)."""

    id = serializers.UUIDField(read_only=True, help_text="ID приглашения.")
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID поставщика.")
    target_email = serializers.EmailField(read_only=True, help_text="Email, на который отправлено приглашение.")
    role = serializers.ChoiceField(
        choices=SupplierUserRole.choices,
        read_only=True,
        help_text="Роль, которую получит приглашённый после принятия.",
    )
    status = serializers.ChoiceField(
        choices=SupplierUserInviteStatus.choices,
        read_only=True,
        help_text=(
            "pending — ожидает принятия. claimed — уже принято. cancelled — отменено (на этот email "
            "создано более новое приглашение). expired — истёк срок."
        ),
    )
    invited_by_email = serializers.SerializerMethodField(help_text="Email пригласившего.")
    expires_at = serializers.DateTimeField(read_only=True, help_text="Срок действия приглашения.")
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания приглашения.")

    @extend_schema_field(serializers.EmailField(allow_null=True))
    def get_invited_by_email(self, obj) -> Optional[str]:
        return obj.invited_by.email if obj.invited_by else None


class SupplierUserInviteCreateSerializer(serializers.Serializer):
    target_email = serializers.EmailField(help_text="Email сотрудника, которого приглашаем в кабинет компании.")
    role = serializers.ChoiceField(
        choices=SupplierUserRole.choices,
        required=False,
        default=SupplierUserRole.MANAGER,
        help_text=(
            "Роль, которую получит приглашённый после принятия. owner — компания может иметь только "
            "одного активного владельца, поэтому принятие такого приглашения деактивирует текущего."
        ),
    )


class SupplierInviteLinkSerializer(serializers.Serializer):
    """2026-08-04: "Пригласите по ссылке" — многоразовая альтернатива
    SupplierUserInviteSerializer (тот привязан к конкретному email)."""

    id = serializers.UUIDField(read_only=True, help_text="ID ссылки-приглашения.")
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID поставщика.")
    token = serializers.CharField(read_only=True, help_text="Токен ссылки.")
    url = serializers.SerializerMethodField(help_text="Полный URL ссылки-приглашения для отправки.")
    role = serializers.ChoiceField(
        choices=SupplierUserRole.choices,
        read_only=True,
        help_text="Роль, которую получит присоединившийся по ссылке.",
    )
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.INACTIVE],
        read_only=True,
        help_text="active — ссылка рабочая, inactive — отключена.",
    )
    expires_at = serializers.DateTimeField(read_only=True, allow_null=True, help_text="null — без срока действия.")
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время создания ссылки.")

    @extend_schema_field(serializers.URLField())
    def get_url(self, obj) -> str:
        return f"{settings.SUPPLIER_INVITE_LINK_BASE_URL.rstrip('/')}/{obj.token}"


class SupplierInviteLinkUpdateSerializer(serializers.Serializer):
    """ "Настройки ссылки" — все поля опциональны, partial update."""

    role = serializers.ChoiceField(
        choices=SupplierUserRole.choices, required=False, help_text="Роль присоединившегося."
    )
    expires_at = serializers.DateTimeField(
        required=False, allow_null=True, help_text="null — снять срок действия (ссылка бессрочна)."
    )
    status = serializers.ChoiceField(
        choices=[EntityStatus.ACTIVE, EntityStatus.INACTIVE],
        required=False,
        help_text="active — включить, inactive — выключить ссылку.",
    )


_ALLOWED_JOIN_REQUEST_DOCUMENT_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}


_MAX_JOIN_REQUEST_DOCUMENT_SIZE = 20 * 1024 * 1024  # 20 МБ — по Figma-макету формы заявки на доступ (2026-08-03)


_MAX_JOIN_REQUEST_DOCUMENTS = 5


class SupplierJoinRequestCreateSerializer(serializers.Serializer):
    message = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Описание ситуации: кто вы в компании, почему запрашиваете доступ.",
    )
    phone = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Контактный телефон для этой заявки (может отличаться от телефона на аккаунте).",
    )
    email = serializers.EmailField(
        required=False,
        allow_blank=True,
        help_text="Контактный email для этой заявки (может отличаться от email на аккаунте).",
    )
    documents = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        write_only=True,
        help_text=(
            "Подтверждающие документы (скан доверенности, справка и т.п.) — PDF, JPEG, PNG или WebP, "
            f"до {_MAX_JOIN_REQUEST_DOCUMENTS} файлов, до 20 МБ каждый. Multipart-запрос."
        ),
    )

    def validate_documents(self, value):
        if len(value) > _MAX_JOIN_REQUEST_DOCUMENTS:
            raise serializers.ValidationError(f"Не больше {_MAX_JOIN_REQUEST_DOCUMENTS} файлов за раз.")
        for document_file in value:
            if document_file.content_type not in _ALLOWED_JOIN_REQUEST_DOCUMENT_CONTENT_TYPES:
                raise serializers.ValidationError("Допустимые форматы документов: PDF, JPEG, PNG, WebP.")
            if document_file.size > _MAX_JOIN_REQUEST_DOCUMENT_SIZE:
                raise serializers.ValidationError("Максимальный размер файла — 20 МБ.")
        return value


class SupplierJoinRequestSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID заявки.")
    supplier_id = serializers.UUIDField(read_only=True, help_text="ID поставщика, к которому запрашивается доступ.")
    supplier_display_name = serializers.CharField(
        read_only=True, source="supplier.display_name", help_text="Название поставщика."
    )
    requested_by_id = serializers.UUIDField(read_only=True, help_text="ID пользователя, подавшего заявку.")
    requested_by_full_name = serializers.CharField(
        read_only=True, source="requested_by.full_name", help_text="ФИО подавшего заявку."
    )
    requested_by_email = serializers.EmailField(
        read_only=True, source="requested_by.email", help_text="Email аккаунта подавшего заявку."
    )
    message = serializers.CharField(
        read_only=True, allow_null=True, help_text="Описание ситуации, указанное при подаче заявки."
    )
    phone = serializers.CharField(read_only=True, allow_null=True, help_text="Контактный телефон для этой заявки.")
    email = serializers.EmailField(read_only=True, allow_null=True, help_text="Контактный email для этой заявки.")
    documents = serializers.ListField(
        child=serializers.CharField(), read_only=True, help_text="Ссылки на подтверждающие документы."
    )
    status = serializers.ChoiceField(
        choices=SupplierJoinRequestStatus.choices,
        read_only=True,
        help_text="pending — на рассмотрении, approved — одобрена, rejected — отклонена.",
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="Дата и время подачи заявки.")
