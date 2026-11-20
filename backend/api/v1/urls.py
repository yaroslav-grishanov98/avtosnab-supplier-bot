from django.urls import include, path

from api.v1.authentication import DualModelTokenRefreshView
from api.v1.qa_views import QAShareAccessSeedView
from api.v1.suppliers.bot_companies import BotUserCompaniesView
from api.v1.suppliers.bot_orders import BotSupplierOrderDetailView, BotSupplierOrdersView
from api.v1.suppliers.views import (
    BotPendingActionCreateView,
    BotPendingActionDetailView,
    BotSupplierPriceUploadTextView,
    BotUserBindingDirectRegisterView,
    BotUserBindingRegisterView,
    BotUserBindingResolveView,
    BotUserStateCurrentView,
)
from api.v1.views import HealthView

app_name = "v1"

urlpatterns = [
    path("health", HealthView.as_view(), name="health"),
    path("token/refresh", DualModelTokenRefreshView.as_view(), name="token-refresh"),
    path("bot-user-bindings/resolve", BotUserBindingResolveView.as_view(), name="bot-user-binding-resolve"),
    path("bot-user-bindings/companies", BotUserCompaniesView.as_view(), name="bot-user-companies"),
    path("bot-user-bindings/orders", BotSupplierOrdersView.as_view(), name="bot-supplier-orders"),
    path(
        "bot-user-bindings/orders/<uuid:request_id>",
        BotSupplierOrderDetailView.as_view(),
        name="bot-supplier-order-detail",
    ),
    path("bot-user-bindings/register", BotUserBindingRegisterView.as_view(), name="bot-user-binding-register"),
    path(
        "bot-user-bindings/direct-register",
        BotUserBindingDirectRegisterView.as_view(),
        name="bot-user-binding-direct-register",
    ),
    path("bot-user-bindings/pending-actions", BotPendingActionCreateView.as_view(), name="bot-pending-action-create"),
    path(
        "bot-user-bindings/uploads/text",
        BotSupplierPriceUploadTextView.as_view(),
        name="bot-supplier-upload-text",
    ),
    path(
        "bot-user-bindings/pending-actions/<uuid:id>",
        BotPendingActionDetailView.as_view(),
        name="bot-pending-action-detail",
    ),
    path("bot-user-states/current", BotUserStateCurrentView.as_view(), name="bot-user-state-current"),
    path("qa/share-access/seed", QAShareAccessSeedView.as_view(), name="qa-share-access-seed"),
    path("suppliers/", include("api.v1.suppliers.urls")),
    path("catalog/", include("api.v1.catalog.urls")),
    path("auth/", include("api.v1.auth.urls")),
    path("venues/", include("api.v1.venues.urls")),
    path("accounting/", include("api.v1.accounting.urls")),
    path("notifications/", include("notifications.urls")),
]
