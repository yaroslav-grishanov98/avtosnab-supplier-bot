"""Google Sheets export and sync log."""

from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import serializers as s
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.catalog.selectors import (
    google_sheet_export_queryset,
)
from api.v1.catalog.serializers import (
    GoogleSheetExportQuerySerializer,
    GoogleSheetExportRowSerializer,
    GoogleSheetSyncLogCreateSerializer,
    GoogleSheetSyncLogResponseSerializer,
)


@extend_schema_view(
    get=extend_schema(
        tags=["Google Sheets"],
        summary="Снапшот каталога для Google Sheets",
        description="Возвращает список активных товаров поставщиков с ценами для записи в лист «Товары от поставщиков».",
        parameters=[
            OpenApiParameter("supplier_id", OpenApiTypes.UUID, description="Фильтр по поставщику"),
            OpenApiParameter("updated_since", OpenApiTypes.DATETIME, description="Только обновлённые после даты"),
        ],
        responses={
            200: inline_serializer(
                name="GoogleSheetExport",
                fields={
                    "sheet": s.CharField(),
                    "rows": GoogleSheetExportRowSerializer(many=True),
                },
            )
        },
    ),
)
class GoogleSheetExportView(APIView):
    def get(self, request):
        serializer = GoogleSheetExportQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        queryset = google_sheet_export_queryset(serializer.validated_data)
        return Response(
            {
                "sheet": "Товары от поставщиков",
                "rows": GoogleSheetExportRowSerializer(queryset, many=True).data,
            }
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Google Sheets"],
        summary="Записать лог синхронизации с Google Sheets",
        description="Создаёт запись о завершённой (или упавшей) синхронизации листа Google Sheets. Вызывается из n8n.",
        request=GoogleSheetSyncLogCreateSerializer,
        responses={201: GoogleSheetSyncLogResponseSerializer},
    ),
)
class GoogleSheetSyncLogCreateView(APIView):
    def post(self, request):
        serializer = GoogleSheetSyncLogCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        sync_log = serializer.save()
        return Response(GoogleSheetSyncLogResponseSerializer(sync_log).data, status=status.HTTP_201_CREATED)
