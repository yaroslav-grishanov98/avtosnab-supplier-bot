"""Call 01.10, docs/adr/0017: reference data the buyer's tender forms need
and only a supplier-cabinet endpoint used to give."""

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import CoreUserJWTAuthentication
from api.v1.permissions import IsCoreUser
from catalog.models import ProductCategory
from core.models import EntityStatus


def product_kinds():
    """Every active «Вид товара» (the third level Группа → Подгруппа → Вид) with its active parents."""
    return (
        ProductCategory.objects.filter(
            status=EntityStatus.ACTIVE,
            parent__status=EntityStatus.ACTIVE,
            parent__parent__status=EntityStatus.ACTIVE,
            parent__parent__parent__isnull=True,
        )
        .select_related("parent__parent")
        .order_by("parent__parent__name", "parent__name", "name", "id")
    )


class TenderProductKindSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, help_text="ID вида товара.")
    name = serializers.CharField(read_only=True, help_text="Вид товара — это значение пишется в product_kind_label.")
    subgroup_id = serializers.UUIDField(source="parent_id", read_only=True, help_text="ID подгруппы.")
    subgroup_name = serializers.CharField(source="parent.name", read_only=True, help_text="Подгруппа.")
    group_id = serializers.UUIDField(source="parent.parent_id", read_only=True, help_text="ID группы.")
    group_name = serializers.CharField(source="parent.parent.name", read_only=True, help_text="Группа.")


@extend_schema_view(
    get=extend_schema(
        operation_id="tender_product_kind_list",
        tags=["Тендеры"],
        summary="Справочник видов товара для позиции тендера",
        description=(
            "Все действующие виды товара (Группа → Подгруппа → Вид) плоским списком — для выпадающего списка "
            "«Вид товара» с поиском. В позицию сохраняется название вида (product_kind_label). Тот же справочник, "
            "что GET /suppliers/categories, но на токене входа (ADR-0017)."
        ),
        responses={
            200: TenderProductKindSerializer(many=True),
            401: OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен."),
        },
    )
)
class TenderProductKindListView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def get(self, request):
        return Response(TenderProductKindSerializer(product_kinds(), many=True).data)
