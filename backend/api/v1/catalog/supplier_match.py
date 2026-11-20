"""TASK-148: массовый подбор товаров поставщиков для справочника компании.

Здесь — общий контракт хранения (VenueProductMatrixSupplierMatch) и всё, что происходит со строкой
после подбора: стартовые параметры каталога, пересчёт количества при открытии, выбор товара.
Формирование поиска и bulk-запуск (запись match со статусом searching → found/not_found/error) —
отдельная часть, пишет в ту же модель через save_supplier_match_result.
"""

import logging

from django.db import transaction
from django.utils import timezone

from api.v1.catalog.selectors import catalog_products_queryset
from api.v1.catalog.serializers import CatalogProductQuerySerializer
from catalog.models import SupplierMatchStatus, VenueProductMatrixItem, VenueProductMatrixSupplierMatch

logger = logging.getLogger("autosnab.catalog.supplier_match")

# Параметры GET /catalog/products, которые автоматический подбор может выставить. supplier_id и
# availability намеренно нет (ТЗ §5: поставщик и наличие автоматически не ограничиваются), sort —
# стандартная сортировка каталога (§6), остальное — пагинация/персонализация, не часть поиска.
MATCH_FILTER_KEYS = ("category_id", "brand_id", "country", "offers", "price_min", "price_max")


class SupplierMatchAlreadyLinked(Exception):
    pass


def match_filters(raw_filters):
    """Сохранённые фильтры → только допустимые ключи с непустыми значениями (JSON-совместимо)."""
    raw_filters = raw_filters if isinstance(raw_filters, dict) else {}
    return {key: raw_filters[key] for key in MATCH_FILTER_KEYS if raw_filters.get(key) not in (None, "", [])}


def supplier_match_search(item):
    """Стартовое состояние каталога для строки (ТЗ §13): сохранённый поиск, если подбор был, иначе
    название товара справочника без фильтров."""
    match = get_supplier_match(item)
    if match is not None and (match.query or match_filters(match.filters)):
        return {"q": match.query, "filters": match_filters(match.filters)}
    return {"q": item.requested_name, "filters": {}}


def get_supplier_match(item):
    try:
        return item.supplier_match
    except VenueProductMatrixSupplierMatch.DoesNotExist:
        return None


def _catalog_filters(*, q, filters, venue_id):
    """Валидирует сохранённые параметры тем же сериализатором, что и каталог. Устаревший/битый
    фильтр отбрасывается, а не роняет открытие строки."""
    data = {"q": q, "venue_id": str(venue_id), **filters}
    ser = CatalogProductQuerySerializer(data=data)
    if not ser.is_valid():
        data = {key: value for key, value in data.items() if key not in ser.errors}
        ser = CatalogProductQuerySerializer(data=data)
        ser.is_valid(raise_exception=True)
    return ser.validated_data


def count_supplier_match_results(*, q, filters, venue_id, user=None):
    """Количество товаров каталога по параметрам подбора — тот же queryset, что отдаёт
    GET /catalog/products, поэтому N в справочнике совпадает с тем, что увидит пользователь."""
    validated = _catalog_filters(q=q, filters=match_filters(filters), venue_id=venue_id)
    return catalog_products_queryset(validated, venue_id=venue_id, user=user).count()


def save_supplier_match_result(item, *, status, query="", filters=None, result_count=None, error=None):
    """Единая точка записи результата подбора строки (для bulk-запуска и пересчёта)."""
    match, _ = VenueProductMatrixSupplierMatch.objects.update_or_create(
        item=item,
        defaults={
            "status": status,
            "query": query or "",
            "filters": match_filters(filters),
            "result_count": result_count,
            "error": error,
            "searched_at": timezone.now() if status != SupplierMatchStatus.SEARCHING else None,
        },
    )
    item.supplier_match = match
    return match


def refresh_supplier_match(item, *, user=None):
    """ТЗ §14: при открытии каталога пересчитать N по актуальному каталогу и обновить строку.
    Не трогает строку без подбора, ещё идущий подбор и товар, уже привязанный к поставщику.
    Сбой пересчёта не мешает открыть каталог — остаётся прежнее значение."""
    match = get_supplier_match(item)
    if match is None or item.supplier_product_id or match.status == SupplierMatchStatus.SEARCHING:
        return match
    if not (match.query or match_filters(match.filters)):
        return match
    try:
        count = count_supplier_match_results(q=match.query, filters=match.filters, venue_id=item.venue_id, user=user)
    except Exception:
        logger.exception("supplier_match.refresh_failed item_id=%s", item.id)
        return match
    return save_supplier_match_result(
        item,
        status=SupplierMatchStatus.FOUND if count else SupplierMatchStatus.NOT_FOUND,
        query=match.query,
        filters=match.filters,
        result_count=count,
    )


@transaction.atomic
def select_supplier_match_product(item, *, supplier_product):
    """ТЗ §17: «Выбрать» — привязать товар поставщика к строке справочника компании. Состояние
    подбора больше не нужно (строка показывает выбранный товар) и удаляется. Уже сопоставленную
    строку не перезаписываем — её меняют через существующий dropdown (PATCH, §18)."""
    locked = VenueProductMatrixItem.objects.select_for_update().get(pk=item.pk)
    if locked.supplier_product_id:
        raise SupplierMatchAlreadyLinked
    locked.supplier_product = supplier_product
    locked.supplier_product_deleted = False
    locked.save(update_fields=["supplier_product", "supplier_product_deleted", "updated_at"])
    VenueProductMatrixSupplierMatch.objects.filter(item=locked).delete()
    return locked
