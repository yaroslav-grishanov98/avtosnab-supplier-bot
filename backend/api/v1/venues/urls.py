from django.urls import path

from api.v1.catalog.approval_api import ApprovalSettingsView
from api.v1.venues.views import (
    VenueAccountTransferClaimView,
    VenueAccountTransferInitiateView,
    VenueAddressLookupView,
    VenueAuditLogView,
    VenueCabinetAddressDetailView,
    VenueCabinetAddressListView,
    VenueCabinetBankAccountDetailView,
    VenueCabinetBankAccountListView,
    VenueCabinetContactDetailView,
    VenueCabinetContactListView,
    VenueCabinetLogoDetailView,
    VenueCabinetLogoUploadView,
    VenueCabinetSessionFromLoginView,
    VenueCabinetView,
    VenueDetailView,
    VenueInnLookupView,
    VenueInviteLinkJoinView,
    VenueInviteLinkPreviewView,
    VenueInviteLinkRegenerateView,
    VenueInviteLinkView,
    VenueJoinRequestApproveView,
    VenueJoinRequestCreateView,
    VenueJoinRequestListView,
    VenueJoinRequestMineView,
    VenueJoinRequestRejectView,
    VenueMineView,
    VenueRestoreView,
    VenueSearchByInnView,
    VenueUserInviteAcceptView,
    VenueUserInviteCreateView,
    VenueUserInvitePendingListView,
    VenueUserInvitePreviewView,
    VenueUserListView,
    VenueUserRevokeView,
    VenueUserRoleChangeView,
)

app_name = "venues"

urlpatterns = [
    path("mine", VenueMineView.as_view(), name="venue-mine"),
    path("cabinet/session-from-login", VenueCabinetSessionFromLoginView.as_view(), name="cabinet-session-from-login"),
    path("cabinet/me", VenueCabinetView.as_view(), name="cabinet-me"),
    path("cabinet/logo", VenueCabinetLogoUploadView.as_view(), name="cabinet-logo-upload"),
    path("cabinet/logo/<uuid:image_id>", VenueCabinetLogoDetailView.as_view(), name="cabinet-logo-detail"),
    path("cabinet/addresses", VenueCabinetAddressListView.as_view(), name="cabinet-address-list"),
    path("cabinet/addresses/<uuid:id>", VenueCabinetAddressDetailView.as_view(), name="cabinet-address-detail"),
    path("cabinet/contacts", VenueCabinetContactListView.as_view(), name="cabinet-contact-list"),
    path("cabinet/contacts/<uuid:id>", VenueCabinetContactDetailView.as_view(), name="cabinet-contact-detail"),
    path("cabinet/bank-accounts", VenueCabinetBankAccountListView.as_view(), name="cabinet-bank-account-list"),
    path(
        "cabinet/bank-accounts/<uuid:id>",
        VenueCabinetBankAccountDetailView.as_view(),
        name="cabinet-bank-account-detail",
    ),
    path(
        "<uuid:venue_id>/account-transfer", VenueAccountTransferInitiateView.as_view(), name="account-transfer-initiate"
    ),
    path("account-transfer/<str:token>/claim", VenueAccountTransferClaimView.as_view(), name="account-transfer-claim"),
    path("<uuid:venue_id>", VenueDetailView.as_view(), name="venue-detail"),
    path("<uuid:venue_id>/restore", VenueRestoreView.as_view(), name="venue-restore"),
    path("<uuid:venue_id>/audit-log", VenueAuditLogView.as_view(), name="venue-audit-log"),
    path("<uuid:venue_id>/users", VenueUserListView.as_view(), name="user-list"),
    path("<uuid:venue_id>/users/<uuid:user_id>/revoke", VenueUserRevokeView.as_view(), name="user-revoke"),
    path("<uuid:venue_id>/users/<uuid:user_id>", VenueUserRoleChangeView.as_view(), name="user-role-change"),
    path("<uuid:venue_id>/invites/pending", VenueUserInvitePendingListView.as_view(), name="user-invite-pending-list"),
    path("<uuid:venue_id>/invites", VenueUserInviteCreateView.as_view(), name="user-invite-create"),
    path("invites/<str:token>", VenueUserInvitePreviewView.as_view(), name="user-invite-preview"),
    path("invites/<str:token>/accept", VenueUserInviteAcceptView.as_view(), name="user-invite-accept"),
    path("<uuid:venue_id>/invite-link", VenueInviteLinkView.as_view(), name="invite-link"),
    path(
        "<uuid:venue_id>/invite-link/regenerate", VenueInviteLinkRegenerateView.as_view(), name="invite-link-regenerate"
    ),
    path("invite-link/<str:token>", VenueInviteLinkPreviewView.as_view(), name="invite-link-preview"),
    path("invite-link/<str:token>/join", VenueInviteLinkJoinView.as_view(), name="invite-link-join"),
    path("search-by-inn", VenueSearchByInnView.as_view(), name="search-by-inn"),
    path("inn-lookup", VenueInnLookupView.as_view(), name="inn-lookup"),
    path("address-lookup", VenueAddressLookupView.as_view(), name="address-lookup"),
    path("join-requests/mine", VenueJoinRequestMineView.as_view(), name="join-request-mine"),
    path("<uuid:venue_id>/join-requests", VenueJoinRequestCreateView.as_view(), name="join-request-create"),
    path("<uuid:venue_id>/join-requests/pending", VenueJoinRequestListView.as_view(), name="join-request-list"),
    path(
        "<uuid:venue_id>/join-requests/<uuid:request_id>/approve",
        VenueJoinRequestApproveView.as_view(),
        name="join-request-approve",
    ),
    path(
        "<uuid:venue_id>/join-requests/<uuid:request_id>/reject",
        VenueJoinRequestRejectView.as_view(),
        name="join-request-reject",
    ),
]


urlpatterns += [path("<uuid:venue_id>/approval-settings", ApprovalSettingsView.as_view())]
