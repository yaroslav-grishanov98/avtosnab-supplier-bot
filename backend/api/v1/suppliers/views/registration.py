"""Supplier registration, INN/address lookup, restore and account transfer."""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import serializers as s
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.auth.services import EmailDeliveryError
from api.v1.authentication import (
    CoreUserJWTAuthentication,
    OptionalCoreUserJWTAuthentication,
)
from api.v1.dadata_client import AddressLookupError, INNLookupError, lookup_company_by_inn, suggest_addresses
from api.v1.permissions import (
    CanInitiateAccountTransfer,
    CanRestoreSupplier,
    HasServiceToken,
    IsCoreUser,
    IsPlatformAdmin,
    IsPlatformAdminOrSupplier,
)
from api.v1.suppliers.bot_events import (
    select_company_for_linked_account,
)
from api.v1.suppliers.serializers import (
    SupplierAccountTransferClaimSerializer,
    SupplierAccountTransferInitiateSerializer,
    SupplierAddressLookupQuerySerializer,
    SupplierAddressSuggestionSerializer,
    SupplierCreateSerializer,
    SupplierDetailSerializer,
    SupplierInnLookupQuerySerializer,
    SupplierInnLookupResponseSerializer,
    SupplierListQuerySerializer,
    SupplierSearchByInnQuerySerializer,
    SupplierSearchByInnResponseSerializer,
)
from api.v1.suppliers.services import (
    claim_supplier_account_transfer,
    create_supplier_by_owner,
    create_supplier_by_staff,
    initiate_supplier_account_transfer,
    restore_supplier,
)
from api.v1.validators import validate_inn
from core.models import EntityStatus, User, UserRole
from suppliers.models import (
    Supplier,
    SupplierUser,
    SupplierUserRole,
)

_ACCOUNT_TRANSFER_INITIATE_SCHEMA = inline_serializer(
    name="SupplierAccountTransferInitiateResult",
    fields={
        "id": s.UUIDField(help_text="ID созданной передачи аккаунта."),
        "supplier_id": s.UUIDField(help_text="ID передаваемой компании-поставщика."),
        "target_email": s.EmailField(help_text="Email получателя, на который отправлена ссылка."),
        "status": s.CharField(help_text="Статус передачи: pending / claimed / cancelled / expired."),
        "expires_at": s.DateTimeField(help_text="До какого момента ссылка действительна."),
    },
)


_ACCOUNT_TRANSFER_CLAIM_SCHEMA = inline_serializer(
    name="SupplierAccountTransferClaimResult",
    fields={
        "supplier_id": s.UUIDField(help_text="ID компании-поставщика, право на которую подтверждено."),
        "user_id": s.UUIDField(help_text="ID пользователя (нового или существующего), ставшего владельцем."),
        "email": s.EmailField(help_text="Email нового владельца — логин для входа в /auth/supplier/login."),
    },
)


@extend_schema_view(
    get=extend_schema(
        tags=["Поставщики"],
        summary="Список всех компаний-поставщиков (admin)",
        description="Требует JWT admin. В отличие от /suppliers/mine — не ограничен своими компаниями.",
        responses={
            200: inline_serializer(
                name="SupplierListPage",
                fields={
                    "count": s.IntegerField(),
                    "limit": s.IntegerField(),
                    "offset": s.IntegerField(),
                    "results": SupplierDetailSerializer(many=True),
                },
            )
        },
    ),
    post=extend_schema(
        tags=["Поставщики"],
        summary="Admin добавляет компанию",
        description=(
            "Требует JWT admin. Создаёт активного Supplier и делает вызывающего "
            "его временным владельцем (SupplierUser, role=owner) — до передачи "
            "реальному поставщику через account-transfer. Перед созданием проверяется "
            "дедуп (api/v1/suppliers/dedup.py) по ИНН/КПП/ОГРН и по похожести названия+адреса "
            "для компаний без ИНН — точное совпадение блокируется всегда, нечёткое можно "
            "обойти явным confirm_not_duplicate=true."
        ),
        request=SupplierCreateSerializer,
        responses={
            201: SupplierDetailSerializer,
            400: OpenApiResponse(description="Невалидный ИНН, либо найден дубль (см. duplicate_match в ответе)"),
        },
    ),
)
class SupplierCreateView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        query_ser = SupplierListQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        limit = query_ser.validated_data["limit"]
        offset = query_ser.validated_data["offset"]

        queryset = Supplier.objects.all().order_by("-created_at")
        total = queryset.count()
        page = queryset[offset : offset + limit].prefetch_related(
            "addresses", "contacts", "bank_accounts", "schedule_rules", "images"
        )

        return Response(
            {
                "count": total,
                "limit": limit,
                "offset": offset,
                "results": SupplierDetailSerializer(page, many=True).data,
            }
        )

    def post(self, request):
        serializer = SupplierCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        supplier = create_supplier_by_staff(
            created_by_user=request.user,
            **serializer.validated_data,
        )
        return Response(SupplierDetailSerializer(supplier).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        tags=["Поставщики"],
        summary="Проверить компанию по ИНН (dadata)",
        description=(
            "Сначала локальная checksum-проверка ИНН, затем запрос в dadata за реквизитами "
            "(название, адрес, статус в реестре, ОКВЭД/ОКПО/дата регистрации/ФИО директора). Не блокирует "
            "создание компании — только обогащение/проверка. Требует JWT admin или JWT supplier "
            "(поставщик использует это для автозаполнения формы создания своей компании)."
        ),
        parameters=[OpenApiParameter("inn", OpenApiTypes.STR, description="ИНН, 10 или 12 цифр")],
        responses={
            200: SupplierInnLookupResponseSerializer,
            400: OpenApiResponse(description="ИНН не прошёл checksum-проверку"),
            404: OpenApiResponse(description="Компания с таким ИНН не найдена в dadata"),
            503: OpenApiResponse(description="dadata не настроена или недоступна"),
        },
    ),
)
class SupplierInnLookupView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsPlatformAdminOrSupplier | HasServiceToken]

    def get(self, request):
        query_ser = SupplierInnLookupQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        inn = query_ser.validated_data["inn"]

        inn_error = validate_inn(inn)
        if inn_error:
            return Response({"detail": inn_error}, status=status.HTTP_400_BAD_REQUEST)

        try:
            data = lookup_company_by_inn(inn)
        except INNLookupError as error:
            message = str(error)
            http_status = status.HTTP_404_NOT_FOUND if "не найдена" in message else status.HTTP_503_SERVICE_UNAVAILABLE
            return Response({"detail": message}, status=http_status)

        return Response(SupplierInnLookupResponseSerializer(data).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Поставщики"],
        summary="Поиск по адресу (dadata) — карточка компании",
        description=(
            "Тот же принцип, что у /suppliers/inn-lookup — обогащение через dadata, только это "
            "live-автокомплит (dadata сама ранжирует и ограничивает выдачу), а не поиск одной точной "
            "записи по ИНН: пустой список — нормальный ответ, пока запрос слишком короткий/неоднозначный, "
            "не 404. Поля suggestion'а — те же структурированные части адреса, что и в inn-lookup "
            "(region/district/street/apartment_office/postal_code), для прямого заполнения формы адреса "
            "в карточке компании (country/region/district/locality/street/postal_code/apartment_office)."
        ),
        parameters=[
            OpenApiParameter("query", OpenApiTypes.STR, description="Часть адреса, как в поле ввода."),
        ],
        responses={
            200: SupplierAddressSuggestionSerializer(many=True),
            503: OpenApiResponse(description="dadata не настроена или недоступна"),
        },
    ),
)
class SupplierAddressLookupView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsPlatformAdminOrSupplier]

    def get(self, request):
        query_ser = SupplierAddressLookupQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        query = query_ser.validated_data["query"]

        try:
            suggestions = suggest_addresses(query)
        except AddressLookupError as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response(SupplierAddressSuggestionSerializer(suggestions, many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Мои компании",
        description=(
            "Список компаний, где текущий core.User — активный участник (SupplierUser), от последней "
            "созданной к первой. Поставщик может состоять в нескольких компаниях одновременно — "
            "переключение между ними на уровне API происходит через supplier_id в "
            "POST /suppliers/cabinet/session-from-login, который выдаёт JWT кабинета для конкретной "
            "компании из этого списка. Пустой список, не 404, если компаний ещё нет."
        ),
        responses={200: SupplierDetailSerializer(many=True)},
    ),
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Добавить ещё одну свою компанию — self-service",
        description=(
            "Требует JWT с role=supplier. Закрывает разрыв «зарегистрировался — а GET /suppliers/mine "
            "говорит, что компании нет, и деваться некуда»: сам поставщик заводит компанию и сразу "
            "становится её владельцем (SupplierUser, role=owner) — не временным, в отличие от "
            "create_supplier_by_staff. Можно вызывать многократно — у одного пользователя может быть "
            "несколько активных компаний, все перечисляются в GET /suppliers/mine. Перед созданием "
            "проверяется дедуп по ИНН/КПП/ОГРН и по похожести названия+адреса — см. "
            "SupplierCreateSerializer.confirm_not_duplicate."
        ),
        request=SupplierCreateSerializer,
        responses={
            201: SupplierDetailSerializer,
            400: OpenApiResponse(description="Невалидный ИНН, либо найден дубль (см. duplicate_match в ответе)"),
            403: OpenApiResponse(description="JWT не с role=supplier"),
        },
    ),
)
class SupplierMineView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request):
        # my_role (2026-08-07, запрос Вити — нужно отобразить owner/manager
        # в таблице компаний) — читаем role прямо из SupplierUser вместе с
        # supplier_id, вместо отдельного values_list только по id, и
        # проставляем как динамический атрибут на каждый Supplier перед
        # сериализацией (SupplierDetailSerializer.get_my_role() его читает).
        role_by_supplier_id = dict(
            SupplierUser.objects.filter(user=request.user, status=EntityStatus.ACTIVE).values_list(
                "supplier_id", "role"
            )
        )
        suppliers = list(
            Supplier.objects.filter(id__in=role_by_supplier_id.keys())
            .order_by("-created_at")
            .prefetch_related("addresses", "contacts", "bank_accounts", "schedule_rules", "images")
        )
        for supplier in suppliers:
            supplier.my_role = role_by_supplier_id.get(supplier.id)
        return Response(SupplierDetailSerializer(suppliers, many=True).data)

    def post(self, request):
        if request.user.role != UserRole.SUPPLIER:
            return Response(
                {"detail": "Самостоятельно создать компанию может только аккаунт поставщика."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = SupplierCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        supplier = create_supplier_by_owner(user=request.user, **serializer.validated_data)
        supplier.my_role = SupplierUserRole.OWNER
        select_company_for_linked_account(user=request.user, supplier=supplier, notify=True)
        return Response(SupplierDetailSerializer(supplier).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    post=extend_schema(
        tags=["Бот"],
        summary="Передать ЛК-аккаунт поставщика на другой email",
        description=(
            "Отправляет письмо со ссылкой на claim-форму; предыдущая незавершённая "
            "передача для этого поставщика отменяется. Вызывается сервис-токеном "
            "(бот/n8n) либо JWT admin/активного участника компании (owner или manager) — тогда "
            "initiated_by_user_id берётся из токена, а не из тела запроса."
        ),
        request=SupplierAccountTransferInitiateSerializer,
        responses={
            201: _ACCOUNT_TRANSFER_INITIATE_SCHEMA,
            503: OpenApiResponse(description="Не удалось отправить письмо — сбой SMTP, повторите позже"),
        },
    ),
)
class SupplierAccountTransferInitiateView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [CanInitiateAccountTransfer]

    def post(self, request, supplier_id):
        serializer = SupplierAccountTransferInitiateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        initiated_by_user_id = serializer.validated_data.get("initiated_by_user_id")
        if isinstance(request.user, User):
            initiated_by_user_id = request.user.id

        try:
            transfer = initiate_supplier_account_transfer(
                supplier_id=supplier_id,
                target_email=serializer.validated_data["target_email"],
                initiated_by_user_id=initiated_by_user_id,
            )
        except EmailDeliveryError as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(
            {
                "id": transfer.id,
                "supplier_id": transfer.supplier_id,
                "target_email": transfer.target_email,
                "status": transfer.status,
                "expires_at": transfer.expires_at,
            },
            status=status.HTTP_201_CREATED,
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Восстановить удалённую (archived) компанию",
        description=(
            "Отменяет DELETE /suppliers/cabinet/me — возвращает status=active, "
            "is_published=true. Не через cabinet-JWT (тот заблокирован, пока status != active) — "
            "обычный JWT core.User + активное членство в компании (owner или manager) или "
            "staff-аккаунт, тот же уровень доступа, что и у передачи аккаунта. 400, если компания "
            "не archived — восстанавливать нечего."
        ),
        request=None,
        responses={
            200: SupplierDetailSerializer,
            400: OpenApiResponse(description="Компания не удалена"),
        },
    ),
)
class SupplierRestoreView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [CanRestoreSupplier]

    def post(self, request, supplier_id):
        supplier = restore_supplier(supplier_id=supplier_id)
        return Response(SupplierDetailSerializer(supplier).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Бот"],
        summary="Принять переданный ЛК-аккаунт — задать пароль",
        description="Публичный endpoint: вызывается со страницы, на которую ведёт ссылка из письма.",
        request=SupplierAccountTransferClaimSerializer,
        responses={200: _ACCOUNT_TRANSFER_CLAIM_SCHEMA},
    ),
)
class SupplierAccountTransferClaimView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, token):
        serializer = SupplierAccountTransferClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = claim_supplier_account_transfer(
            token=token,
            new_password=serializer.validated_data["new_password"],
        )
        return Response(result)


@extend_schema_view(
    get=extend_schema(
        tags=["Кабинет поставщика"],
        summary="Найти компанию по ИНН, чтобы присоединиться",
        description=(
            "Ищет уже зарегистрированную активную компанию по ИНН — первый шаг флоу "
            '"присоединиться к существующей компании" в ЛК. 404, если компания с таким ИНН не найдена.'
        ),
        responses={200: SupplierSearchByInnResponseSerializer},
    ),
)
class SupplierSearchByInnView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request):
        query_ser = SupplierSearchByInnQuerySerializer(data=request.query_params)
        query_ser.is_valid(raise_exception=True)
        inn = query_ser.validated_data["inn"]

        inn_error = validate_inn(inn)
        if inn_error:
            return Response({"detail": inn_error}, status=status.HTTP_400_BAD_REQUEST)

        supplier = Supplier.objects.filter(inn=inn, status=EntityStatus.ACTIVE).first()
        if supplier is None:
            return Response({"detail": "Компания с таким ИНН не найдена."}, status=status.HTTP_404_NOT_FOUND)

        return Response(SupplierSearchByInnResponseSerializer(supplier).data)
