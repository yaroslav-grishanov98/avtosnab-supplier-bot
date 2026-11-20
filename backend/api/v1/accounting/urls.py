from django.urls import path

from api.v1.accounting.views import (
    AccountingStatusListView,
    VenueConnectionSyncView,
    VenueStatusView,
    VenueSyncJobView,
)

app_name = "accounting"

urlpatterns = [
    path("status", AccountingStatusListView.as_view(), name="status-list"),
    path("venues/<uuid:venue_id>/status", VenueStatusView.as_view(), name="venue-status"),
    path(
        "venues/<uuid:venue_id>/connections/<uuid:connection_id>/sync",
        VenueConnectionSyncView.as_view(),
        name="venue-connection-sync",
    ),
    path("venues/<uuid:venue_id>/sync-jobs/<uuid:job_id>", VenueSyncJobView.as_view(), name="venue-sync-job"),
]
