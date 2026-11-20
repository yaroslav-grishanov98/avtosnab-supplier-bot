"""Integration status and manual sync of accounting systems (MVP: read the status, start a sync).

Connecting, replacing credentials and assigning departments are done by a platform admin in Django admin (plan D11);
the REST write API is phase 2. Nothing here returns a login, password hash, ciphertext or token.
"""

from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from accounting.models import AccountingConnection, AccountingSyncJob, SyncKind
from accounting.services import (
    Actor,
    ConnectionDisabled,
    SyncThrottled,
    can_manage_accounting,
    managed_venue_ids,
    request_manual_sync,
)
from accounting.status import compute_status, statuses_for_venues
from api.v1.accounting.serializers import (
    ErrorSerializer,
    SyncJobSerializer,
    ThrottledErrorSerializer,
    VenueIntegrationStatusSerializer,
)
from api.v1.authentication import CoreUserJWTAuthentication, OptionalCoreUserJWTAuthentication
from api.v1.permissions import HasServiceToken, IsCoreUser, IsVenueMember
from api.v1.venues.services import list_my_venues
from core.models import User, Venue

TAG = ["Интеграция учётных систем"]
VENUE_PARAM = OpenApiParameter(
    "venue_id", OpenApiTypes.UUID, OpenApiParameter.PATH, required=True, description="Заведение."
)
CONNECTION_PARAM = OpenApiParameter(
    "connection_id",
    OpenApiTypes.UUID,
    OpenApiParameter.PATH,
    required=True,
    description="Подключение к учётной системе, которое обслуживает это заведение.",
)
JOB_PARAM = OpenApiParameter(
    "job_id",
    OpenApiTypes.UUID,
    OpenApiParameter.PATH,
    required=True,
    description="Задача синхронизации подключения этого заведения.",
)
_VENUE_NOT_FOUND = "Заведение не найдено."
_CONNECTION_NOT_FOUND = "Подключение не найдено у этого заведения."
_JOB_NOT_FOUND = "Задача синхронизации не найдена у этого заведения."


def _status_payload(status_value, can_manage):
    return {
        "venue_id": status_value.venue_id,
        "state": status_value.state,
        "reason_code": status_value.reason_code,
        "connection_id": status_value.connection_id,
        "provider": status_value.provider,
        "last_success_at": status_value.last_success_at,
        "stale_since": status_value.stale_since,
        "warehouses_count": status_value.warehouses_count,
        "can_manage": can_manage,
    }


@extend_schema_view(
    get=extend_schema(
        tags=TAG,
        summary="Статус интеграции по всем заведениям пользователя",
        description="Заведения, где пользователь — активный участник. Только JWT пользователя.",
        responses={
            200: VenueIntegrationStatusSerializer(many=True),
            401: OpenApiResponse(response=ErrorSerializer, description="Нет JWT или он недействителен."),
        },
    )
)
class AccountingStatusListView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request):
        venue_ids = [venue.id for venue in list_my_venues(user_id=request.user.id)]
        statuses = statuses_for_venues(venue_ids, timezone.now())
        manageable = managed_venue_ids(request.user, venue_ids)
        return Response([_status_payload(statuses[venue_id], venue_id in manageable) for venue_id in venue_ids])


@extend_schema_view(
    get=extend_schema(
        tags=TAG,
        summary="Статус интеграции заведения",
        parameters=[VENUE_PARAM],
        description="Служебный токен может читать статус; поле can_manage для него всегда false.",
        responses={
            200: VenueIntegrationStatusSerializer,
            401: OpenApiResponse(response=ErrorSerializer, description="Нет JWT или он недействителен."),
            403: OpenApiResponse(
                response=ErrorSerializer,
                description="Не участник заведения и не служебный токен или администратор.",
            ),
            404: OpenApiResponse(response=ErrorSerializer, description="Заведения нет."),
        },
    )
)
class VenueStatusView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id):
        if not Venue.objects.filter(pk=venue_id).exists():
            raise NotFound(_VENUE_NOT_FOUND)
        user = request.user if isinstance(request.user, User) else None
        can_manage = bool(user) and can_manage_accounting(user, venue_id)
        return Response(_status_payload(compute_status(venue_id, timezone.now()), can_manage))


def _venue_connections(venue_id):
    return AccountingConnection.objects.for_venue(venue_id)


@extend_schema_view(
    post=extend_schema(
        tags=TAG,
        summary="Запустить синхронизацию остатков подключения заведения",
        parameters=[VENUE_PARAM, CONNECTION_PARAM],
        description=(
            "Ставит задачу в очередь и сразу отвечает. Если для подключения уже есть задача в очереди или "
            "выполняется, возвращается она. Только JWT пользователя, служебный токен не допускается. Слишком "
            "частые запросы — 429."
        ),
        request=None,
        responses={
            202: SyncJobSerializer,
            401: OpenApiResponse(response=ErrorSerializer, description="Нет JWT или он недействителен."),
            403: OpenApiResponse(response=ErrorSerializer, description="Не участник заведения или служебный токен."),
            404: OpenApiResponse(
                response=ErrorSerializer, description="Подключение не относится к заведению или не найдено."
            ),
            409: OpenApiResponse(response=ErrorSerializer, description="Подключение отключено."),
            429: OpenApiResponse(
                response=ThrottledErrorSerializer, description="Ручной синк запускали слишком недавно."
            ),
        },
    )
)
class VenueConnectionSyncView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember, IsCoreUser]

    def permission_denied(self, request, message=None, code=None):
        # A valid service token is not "unauthenticated": say it is the wrong kind of caller.
        if HasServiceToken().has_permission(request, self):
            raise PermissionDenied("Служебный токен не может запускать синхронизацию: нужен вход пользователя.")
        super().permission_denied(request, message=message, code=code)

    def post(self, request, venue_id, connection_id):
        connection = _venue_connections(venue_id).filter(pk=connection_id).first()
        if connection is None:
            raise NotFound(_CONNECTION_NOT_FOUND)
        actor = Actor(label=request.user.email, user=request.user)
        try:
            job = request_manual_sync(connection, actor, SyncKind.STOCK)
        except SyncThrottled as exc:
            return Response(
                {
                    "detail": f"Синхронизацию уже запускали недавно. Повторите через {exc.retry_after} с.",
                    "retry_after": exc.retry_after,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(exc.retry_after)},
            )
        except ConnectionDisabled:
            return Response(
                {"detail": "Подключение отключено, синхронизация невозможна."}, status=status.HTTP_409_CONFLICT
            )
        return Response(SyncJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


@extend_schema_view(
    get=extend_schema(
        tags=TAG,
        summary="Статус задачи синхронизации",
        parameters=[VENUE_PARAM, JOB_PARAM],
        responses={
            200: SyncJobSerializer,
            401: OpenApiResponse(response=ErrorSerializer, description="Нет JWT или он недействителен."),
            403: OpenApiResponse(response=ErrorSerializer, description="Не участник заведения."),
            404: OpenApiResponse(
                response=ErrorSerializer, description="Задача не относится к подключениям заведения или не найдена."
            ),
        },
    )
)
class VenueSyncJobView(APIView):
    authentication_classes = [OptionalCoreUserJWTAuthentication]
    permission_classes = [IsVenueMember]

    def get(self, request, venue_id, job_id):
        job = AccountingSyncJob.objects.filter(connection__in=_venue_connections(venue_id), pk=job_id).first()
        if job is None:
            raise NotFound(_JOB_NOT_FOUND)
        return Response(SyncJobSerializer(job).data)
