from django.urls import path

from .views import (
    NotificationBotConnectView,
    NotificationBotStatusView,
    NotificationBotTestDeliveryView,
    NotificationDeliveryReceiptView,
    NotificationDeliveryReviewView,
    NotificationDestinationDetailView,
    NotificationDestinationListView,
    NotificationDestinationVerifyView,
    NotificationHistoryView,
    NotificationInboxReadAllView,
    NotificationInboxReadView,
    NotificationInboxView,
    NotificationLinkTokenView,
    NotificationRulesView,
    NotificationSupplierActionView,
    NotificationSupplierMiniAppView,
    NotificationSupplierRequestBotDetailView,
)

app_name = "notifications"

urlpatterns = [
    path("destinations", NotificationDestinationListView.as_view(), name="destination-list"),
    path("destinations/verify", NotificationDestinationVerifyView.as_view(), name="destination-verify"),
    path("destinations/<uuid:destination_id>", NotificationDestinationDetailView.as_view(), name="destination-detail"),
    path("destinations/<uuid:destination_id>/rules", NotificationRulesView.as_view(), name="destination-rules"),
    path("link-tokens", NotificationLinkTokenView.as_view(), name="link-token-create"),
    path("history", NotificationHistoryView.as_view(), name="history"),
    path("inbox", NotificationInboxView.as_view(), name="inbox"),
    path("inbox/read-all", NotificationInboxReadAllView.as_view(), name="inbox-read-all"),
    path("inbox/<uuid:notification_id>/read", NotificationInboxReadView.as_view(), name="inbox-read"),
    path("bot/connect", NotificationBotConnectView.as_view(), name="bot-connect"),
    path("bot/status", NotificationBotStatusView.as_view(), name="bot-status"),
    path("bot/test", NotificationBotTestDeliveryView.as_view(), name="bot-test-delivery"),
    path("bot/deliveries/confirm", NotificationDeliveryReceiptView.as_view(), name="bot-delivery-receipt"),
    path("bot/deliveries/review", NotificationDeliveryReviewView.as_view(), name="bot-delivery-review"),
    path("miniapp/supplier-request", NotificationSupplierMiniAppView.as_view(), name="supplier-miniapp"),
    path(
        "bot/supplier-requests/<uuid:request_id>",
        NotificationSupplierRequestBotDetailView.as_view(),
        name="bot-supplier-request-detail",
    ),
    path(
        "bot/supplier-requests/<uuid:request_id>/respond",
        NotificationSupplierActionView.as_view(),
        name="bot-supplier-respond",
    ),
]
