from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.views import TokenRefreshView

from core.models import EntityStatus, User, Venue
from suppliers.models import Supplier


class CoreUserJWTAuthentication(JWTAuthentication):
    """JWTAuthentication that resolves tokens against core.User instead of
    Django's AUTH_USER_MODEL, since /admin/ keeps using the default auth
    user and core.User was never made the project's AUTH_USER_MODEL."""

    def get_user(self, validated_token):
        user_id = validated_token.get("user_id")
        if user_id is None:
            raise InvalidToken("Token contained no recognizable user identification")
        try:
            user = User.objects.get(pk=user_id)
        except (User.DoesNotExist, ValueError, TypeError):
            raise InvalidToken("User not found")
        if user.status != EntityStatus.ACTIVE:
            raise InvalidToken("User is inactive")
        return user


class OptionalCoreUserJWTAuthentication(CoreUserJWTAuthentication):
    """Same JWT resolution, but swallows auth failures instead of raising.

    Some endpoints accept *either* a JWT or the shared SERVICE_API_TOKEN in
    the same `Authorization: Bearer ...` header. Plain JWTAuthentication
    raises as soon as it sees a non-JWT value there, which would 401 a
    service-token request before HasServiceToken-based permissions ever
    run. Returning None instead lets those permissions decide.
    """

    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except (InvalidToken, AuthenticationFailed):
            return None


class SupplierCabinetJWTAuthentication(JWTAuthentication):
    """JWTAuthentication for the supplier cabinet SPA session.

    Resolves tokens against suppliers.Supplier via a custom `supplier_id`
    claim rather than the `user_id` claim CoreUserJWTAuthentication reads,
    since a cabinet session identifies a Supplier, not a core.User. Issued
    by exchanging the bot's one-time cabinet token (see
    api.v1.suppliers.services.exchange_cabinet_session_token)."""

    def get_user(self, validated_token):
        supplier_id = validated_token.get("supplier_id")
        if supplier_id is None:
            raise InvalidToken("Token contained no recognizable supplier identification")
        try:
            supplier = Supplier.objects.get(pk=supplier_id)
        except (Supplier.DoesNotExist, ValueError, TypeError):
            raise InvalidToken("Supplier not found")
        if supplier.status != EntityStatus.ACTIVE:
            raise InvalidToken("Supplier is inactive")
        return supplier


class OptionalSupplierCabinetJWTAuthentication(SupplierCabinetJWTAuthentication):
    """Same JWT resolution, but swallows auth failures instead of raising.

    Lets an endpoint accept *either* a supplier cabinet JWT or the shared
    SERVICE_API_TOKEN in the same `Authorization: Bearer ...` header —
    mirrors OptionalCoreUserJWTAuthentication's reasoning."""

    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except (InvalidToken, AuthenticationFailed):
            return None


class VenueCabinetJWTAuthentication(JWTAuthentication):
    """JWTAuthentication for the venue cabinet session — mirrors
    SupplierCabinetJWTAuthentication exactly, resolving against core.Venue
    via a `venue_id` claim instead of `supplier_id`. See
    docs/wiki/logs/2026-08-15-venue-cabinet-mirror.md."""

    def get_user(self, validated_token):
        venue_id = validated_token.get("venue_id")
        if venue_id is None:
            raise InvalidToken("Token contained no recognizable venue identification")
        try:
            venue = Venue.objects.get(pk=venue_id)
        except (Venue.DoesNotExist, ValueError, TypeError):
            raise InvalidToken("Venue not found")
        if venue.status != EntityStatus.ACTIVE:
            raise InvalidToken("Venue is inactive")
        return venue


class OptionalVenueCabinetJWTAuthentication(VenueCabinetJWTAuthentication):
    """Same JWT resolution, but swallows auth failures instead of raising —
    mirrors OptionalSupplierCabinetJWTAuthentication."""

    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except (InvalidToken, AuthenticationFailed):
            return None


class DualModelTokenRefreshSerializer(TokenRefreshSerializer):
    """The stock TokenRefreshSerializer checks the token's subject still
    exists/is active via `get_user_model().objects.get(pk=user_id)` — i.e.
    Django's AUTH_USER_MODEL. This project never made core.User (or
    suppliers.Supplier) the AUTH_USER_MODEL — it can't be, neither extends
    AbstractBaseUser — so that lookup always crashed with a ValueError
    (a UUID against AUTH_USER_MODEL's integer `id`) for every refresh of a
    `user_id`-claim token. Mirrors CoreUserJWTAuthentication/
    SupplierCabinetJWTAuthentication's own lookup instead."""

    def validate(self, attrs):
        refresh = self.token_class(attrs["refresh"])

        user_id = refresh.payload.get("user_id")
        supplier_id = refresh.payload.get("supplier_id")
        venue_id = refresh.payload.get("venue_id")
        if user_id is not None:
            try:
                user = User.objects.get(pk=user_id)
            except (User.DoesNotExist, ValueError, TypeError):
                raise InvalidToken("User not found")
            if user.status != EntityStatus.ACTIVE:
                raise InvalidToken("User is inactive")
        elif supplier_id is not None:
            try:
                supplier = Supplier.objects.get(pk=supplier_id)
            except (Supplier.DoesNotExist, ValueError, TypeError):
                raise InvalidToken("Supplier not found")
            if supplier.status != EntityStatus.ACTIVE:
                raise InvalidToken("Supplier is inactive")
        elif venue_id is not None:
            try:
                venue = Venue.objects.get(pk=venue_id)
            except (Venue.DoesNotExist, ValueError, TypeError):
                raise InvalidToken("Venue not found")
            if venue.status != EntityStatus.ACTIVE:
                raise InvalidToken("Venue is inactive")

        data = {"access": str(refresh.access_token)}

        if api_settings.ROTATE_REFRESH_TOKENS:
            if api_settings.BLACKLIST_AFTER_ROTATION:
                try:
                    refresh.blacklist()
                except AttributeError:
                    pass

            refresh.set_jti()
            refresh.set_exp()
            refresh.set_iat()
            refresh.outstand()

            data["refresh"] = str(refresh)

        return data


class DualModelTokenRefreshView(TokenRefreshView):
    serializer_class = DualModelTokenRefreshSerializer
