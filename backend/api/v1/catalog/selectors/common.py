"""Small venue helpers used across catalog selectors."""

from catalog.models import (
    VenueProductMatrixItem,
)
from core.models import (
    EntityStatus,
    VenueUser,
    VenueUserRole,
)


def _owner_active_venue_ids(owner_id_expr):
    """Venue ids the given owner (a concrete id, or an OuterRef into the
    outer queryset) currently owns via an active OWNER VenueUser row. Single
    source of truth for what counts as "the owner's active companies" —
    shared by the companies_count annotation (below) and
    master_product_venue_breakdown, so the two can never drift on scoping
    again (2026-09-01 fix wave, Fix 2/3: an archived venue used to still
    count toward companies_count while the breakdown modal already excluded
    it, since delete_venue_cabinet soft-archives a venue but leaves its
    VenueUser/matrix-item rows in place)."""
    return VenueUser.objects.filter(user_id=owner_id_expr, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE).values(
        "venue_id"
    )


def owner_linked_venue_ids(owner_id, master_product_ids):
    """TASK-93 (ТЗ «Мастер-справочник — создание тендера» §6): venues where a
    master product is used — an active matrix row in an active venue of the
    owner. Same definition as master_product_venue_breakdown, so the tender
    auto-selection and the venues modal agree. {master_product_id: {venue_id}}."""
    linked = {mp_id: set() for mp_id in master_product_ids}
    rows = VenueProductMatrixItem.objects.filter(
        master_product_id__in=master_product_ids,
        status=EntityStatus.ACTIVE,
        venue__status=EntityStatus.ACTIVE,
        venue_id__in=_owner_active_venue_ids(owner_id),
    ).values_list("master_product_id", "venue_id")
    for mp_id, venue_id in rows:
        linked[mp_id].add(venue_id)
    return linked


def venue_label(venue):
    """How the buyer's cabinet names a venue — short_name, else display_name. Every tender response and message
    uses it; orders keep display_name."""
    return venue.short_name or venue.display_name
