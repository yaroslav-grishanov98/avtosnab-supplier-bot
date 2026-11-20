"""Venue subdivisions, off-system suppliers and venue search."""

from django.db.models import Q

from core.models import (
    EntityStatus,
    OffSystemSupplier,
    Venue,
    VenueSubdivision,
)


def venue_subdivisions_queryset(venue_id):
    return VenueSubdivision.objects.filter(venue_id=venue_id, status=EntityStatus.ACTIVE).order_by("name", "id")


def venue_off_system_suppliers_queryset(venue_id):
    return OffSystemSupplier.objects.filter(venue_id=venue_id, status=EntityStatus.ACTIVE).order_by(
        "display_name", "id"
    )


def venue_search_queryset(q):
    """2026-09-23, по просьбе пользователя: поиск заведений по ИНН и/или
    названию — для кабинета поставщика, где нужно выбрать одно или
    несколько заведений перед назначением индивидуальной цены
    (`POST /suppliers/products/{id}/individual-prices` принимает готовый
    `venue_ids`, а найти их раньше можно было только точным ИНН через
    `GET /venues/search-by-inn`, доступный лишь заведению для присоединения
    к себе — не подходит поставщику ни по правам, ни по семантике "точное
    совпадение, максимум одно заведение").

    Один текстовый `q` матчится подстрокой сразу по `display_name`,
    `legal_name` и `inn` — то же самое поле в реальности может искать и по
    названию, и по ИНН, разделять на два параметра было бы избыточно для
    одного поля поиска на фронте."""
    return Venue.objects.filter(
        Q(display_name__icontains=q) | Q(legal_name__icontains=q) | Q(inn__icontains=q), status=EntityStatus.ACTIVE
    ).order_by("display_name", "id")
