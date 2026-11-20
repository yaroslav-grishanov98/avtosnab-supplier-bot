from django.urls import path

from api.v1.auth.views import (
    BootstrapAdminUserView,
    EmailVerificationConfirmView,
    ForgotPasswordView,
    MeView,
    PasswordResetConfirmView,
    SupplierLoginView,
    SupplierRegisterView,
    VenueLoginView,
    VenueRegisterView,
    VKOAuthCallbackView,
    VKOAuthLoginView,
    YandexOAuthCallbackView,
    YandexOAuthLoginView,
)

app_name = "auth"

urlpatterns = [
    path("supplier/register", SupplierRegisterView.as_view(), name="supplier-register"),
    path("supplier/login", SupplierLoginView.as_view(), name="supplier-login"),
    path("venue/register", VenueRegisterView.as_view(), name="venue-register"),
    path("venue/login", VenueLoginView.as_view(), name="venue-login"),
    path("venue/bootstrap", BootstrapAdminUserView.as_view(), name="venue-bootstrap"),
    path(
        "email-verification/<str:token>/confirm",
        EmailVerificationConfirmView.as_view(),
        name="email-verification-confirm",
    ),
    path("forgot-password", ForgotPasswordView.as_view(), name="forgot-password"),
    path(
        "password-reset/<str:token>/confirm",
        PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path("yandex/login", YandexOAuthLoginView.as_view(), name="yandex-login"),
    path("yandex/callback", YandexOAuthCallbackView.as_view(), name="yandex-callback"),
    path("vk/login", VKOAuthLoginView.as_view(), name="vk-login"),
    path("vk/callback", VKOAuthCallbackView.as_view(), name="vk-callback"),
    path("me", MeView.as_view(), name="me"),
]
