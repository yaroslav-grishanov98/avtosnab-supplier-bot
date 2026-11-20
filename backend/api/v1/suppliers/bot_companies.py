"""Company management for a bot identity linked to a cabinet account."""

import json

from django.db import transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import serializers, status
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.permissions import HasServiceToken
from api.v1.suppliers.serializers import (
    BotUserBindingResolveSerializer,
    SupplierCreateSerializer,
    SupplierDetailSerializer,
)
from api.v1.suppliers.services import create_supplier_by_owner
from core.models import EntityStatus, UserRole
from suppliers.models import BotUserBinding, BotUserState, SourceChannel, SupplierUser, SupplierUserRole


class BotCompanyIdentitySerializer(serializers.Serializer):
    platform = serializers.ChoiceField(choices=[SourceChannel.TELEGRAM, SourceChannel.MAX])
    telegram_user_id = serializers.CharField(trim_whitespace=True)
    chat_id = serializers.CharField(trim_whitespace=True)


class BotCompanyCreateSerializer(BotCompanyIdentitySerializer):
    company = SupplierCreateSerializer()


class BotCompanySelectSerializer(BotCompanyIdentitySerializer):
    supplier_id = serializers.UUIDField()


class BotCompanyListSerializer(serializers.Serializer):
    companies = SupplierDetailSerializer(many=True)
    selected_supplier_id = serializers.UUIDField(allow_null=True)


class BotCompanyResultSerializer(serializers.Serializer):
    company = SupplierDetailSerializer()
    binding = BotUserBindingResolveSerializer()


class BotCompanyError(APIException):
    def __init__(self, code, message, http_status):
        self.status_code = http_status
        super().__init__({"code": code, "message": message})


def _binding(data, *, lock=False):
    # Match both identifiers: a chat match must never override a different user.
    queryset = BotUserBinding.objects.select_related("user").filter(
        channel=data["platform"],
        chat_id=data["chat_id"],
        external_user_id=data["telegram_user_id"],
        status=EntityStatus.ACTIVE,
    )
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    binding = queryset.first()
    if binding is None:
        raise BotCompanyError("BOT_USER_BINDING_NOT_FOUND", "Active bot binding was not found.", 404)
    if binding.user_id is None:
        raise BotCompanyError("BOT_ACCOUNT_NOT_LINKED", "Link the bot through your cabinet account again.", 409)
    if binding.user.status != EntityStatus.ACTIVE:
        raise PermissionDenied("The linked account is inactive.")
    return binding


def _memberships(binding):
    return SupplierUser.objects.filter(
        user_id=binding.user_id, status=EntityStatus.ACTIVE, supplier__status=EntityStatus.ACTIVE
    ).select_related("supplier")


def _select_company(binding, supplier):
    binding.supplier = supplier
    binding.save(update_fields=["supplier", "updated_at"])
    state, _ = BotUserState.objects.select_for_update().get_or_create(
        platform=binding.channel,
        chat_id=binding.chat_id,
        defaults={"telegram_user_id": binding.external_user_id},
    )
    payload = state.payload
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    state.payload = {**(payload if isinstance(payload, dict) else {}), "selected_company_id": str(supplier.id)}
    state.telegram_user_id = binding.external_user_id
    state.save(update_fields=["payload", "telegram_user_id", "updated_at"])


_ERRORS = {
    400: OpenApiResponse(description="Invalid identity or company data (including duplicate company)."),
    403: OpenApiResponse(description="Invalid service token, inactive account, or account cannot create companies."),
    404: OpenApiResponse(description="BOT_USER_BINDING_NOT_FOUND or BOT_COMPANY_NOT_AVAILABLE."),
    409: OpenApiResponse(description="BOT_ACCOUNT_NOT_LINKED: reconnect through the cabinet account."),
}


@extend_schema_view(
    get=extend_schema(
        tags=["Бот"],
        summary="Компании аккаунта, связанного с ботом",
        parameters=[BotCompanyIdentitySerializer],
        responses={200: BotCompanyListSerializer, **_ERRORS},
    ),
    post=extend_schema(
        tags=["Бот"],
        summary="Создать компанию в аккаунте и выбрать её в боте",
        request=BotCompanyCreateSerializer,
        responses={201: BotCompanyResultSerializer, **_ERRORS},
    ),
    patch=extend_schema(
        tags=["Бот"],
        summary="Переключить компанию бота в пределах связанного аккаунта",
        request=BotCompanySelectSerializer,
        responses={200: BotCompanyResultSerializer, **_ERRORS},
    ),
)
class BotUserCompaniesView(APIView):
    authentication_classes = []
    permission_classes = [HasServiceToken]

    def get(self, request):
        serializer = BotCompanyIdentitySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        binding = _binding(serializer.validated_data)
        companies = []
        for membership in (
            _memberships(binding)
            .prefetch_related(
                "supplier__addresses", "supplier__contacts", "supplier__bank_accounts", "supplier__schedule_rules"
            )
            .order_by("-supplier__created_at")
        ):
            membership.supplier.my_role = membership.role
            companies.append(membership.supplier)
        selected = binding.supplier_id if any(c.id == binding.supplier_id for c in companies) else None
        return Response(BotCompanyListSerializer({"companies": companies, "selected_supplier_id": selected}).data)

    @transaction.atomic
    def post(self, request):
        serializer = BotCompanyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        binding = _binding(serializer.validated_data, lock=True)
        if binding.user.role != UserRole.SUPPLIER:
            raise PermissionDenied("Only a supplier account can create a company.")
        supplier = create_supplier_by_owner(user=binding.user, **serializer.validated_data["company"])
        supplier.my_role = SupplierUserRole.OWNER
        _select_company(binding, supplier)
        return Response(
            BotCompanyResultSerializer({"company": supplier, "binding": binding}).data,
            status=status.HTTP_201_CREATED,
        )

    @transaction.atomic
    def patch(self, request):
        serializer = BotCompanySelectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        binding = _binding(serializer.validated_data, lock=True)
        membership = _memberships(binding).filter(supplier_id=serializer.validated_data["supplier_id"]).first()
        if membership is None:
            raise BotCompanyError("BOT_COMPANY_NOT_AVAILABLE", "Company is not available to this account.", 404)
        supplier = membership.supplier
        supplier.my_role = membership.role
        _select_company(binding, supplier)
        return Response(BotCompanyResultSerializer({"company": supplier, "binding": binding}).data)
