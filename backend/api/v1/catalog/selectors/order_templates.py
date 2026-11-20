"""Venue order template reads."""

from django.db.models import Count

from core.models import (
    EntityStatus,
    OrderTemplate,
)


def venue_order_templates_queryset(venue_id):
    return (
        OrderTemplate.objects.filter(venue_id=venue_id, status=EntityStatus.ACTIVE)
        .annotate(items_count=Count("items"))
        .order_by("name", "id")
    )
