import uuid
from collections.abc import Mapping
from hmac import compare_digest

from django.conf import settings
from rest_framework.permissions import BasePermission

from catalog.models import MasterProduct, SupplierProductVenuePrice, Tender
from core.models import AuditLog, AuditLogEntityType, EntityStatus, User, UserRole, Venue, VenueUser, VenueUserRole
from suppliers.models import Supplier, SupplierUser, SupplierUserRole


class HasServiceToken(BasePermission):
    """Allow n8n/backend integrations authenticated by a shared service token."""

    message = "Valid service token is required."

    def has_permission(self, request, view):
        expected_token = getattr(settings, "SERVICE_API_TOKEN", "")
        provided_token = self._get_token(request)

        if not expected_token or not provided_token:
            return False

        return compare_digest(provided_token, expected_token)

    @staticmethod
    def _get_token(request):
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token.strip()

        return request.headers.get("X-Service-Token", "").strip()


class IsCoreUser(BasePermission):
    """Any request authenticated as an active core.User via JWT."""

    message = "Authentication required."

    def has_permission(self, request, view):
        return isinstance(request.user, User)


class HasServiceTokenOrIsCoreUser(BasePermission):
    """Allow either the shared service token (bot/n8n/legacy catalog callers)
    or a JWT-authenticated core.User (e.g. a venue user browsing the
    catalog to see their own wishlists)."""

    message = "Requires a service token or authentication."

    def has_permission(self, request, view):
        if HasServiceToken().has_permission(request, view):
            return True
        return isinstance(request.user, User)


class IsSupplierCabinet(BasePermission):
    """JWT-authenticated supplier cabinet session (see SupplierCabinetJWTAuthentication)."""

    message = "Cabinet authentication required."

    def has_permission(self, request, view):
        return isinstance(request.user, Supplier)


def is_supplier_cabinet_active_member(request):
    """Is the acting user, per the cabinet JWT's `user_id` claim, an active
    member (owner or manager — identical rights since 2026-08-05) of
    request.user (a Supplier)?

    The claim only exists for sessions issued via cabinet/session-from-login
    (the web-login bridge, where request.user at issuance time is a real
    core.User). Sessions from the Telegram/Max bot's one-time cabinet token
    have no linked core.User to stamp — BotUserBinding.user is never
    populated today — so they have no way to prove who's asking, and this
    returns False for them: fail closed, not silently allowed. Shared by
    IsSupplierCabinetActiveMember (gates whole endpoints) and views that
    need this check within an otherwise multi-role endpoint (e.g. PATCH
    cabinet/me's registry/bank fields, 2026-08-01)."""
    if not isinstance(request.user, Supplier):
        return False
    user_id = request.auth.get("user_id") if request.auth else None
    if not user_id:
        return False
    return SupplierUser.objects.filter(
        supplier=request.user,
        user_id=user_id,
        status=EntityStatus.ACTIVE,
    ).exists()


class IsSupplierCabinetActiveMember(BasePermission):
    """Same cabinet-JWT session as IsSupplierCabinet, but additionally
    requires the acting user to be an identified, active member of the
    company (owner or manager — identical rights since 2026-08-05) — for
    actions as consequential as deleting it (2026-08-01 audit: DELETE
    /suppliers/cabinet/me had no check at all, any bot-originated session
    could archive the company). See is_supplier_cabinet_active_member()
    for why bot-originated sessions with no linked core.User always fail
    this check regardless of role."""

    message = "Requires active membership of this supplier (log in via the web cabinet)."

    def has_permission(self, request, view):
        return is_supplier_cabinet_active_member(request)


class IsSupplierCabinetOrHasServiceToken(BasePermission):
    """Allow the shared service token (bot/n8n) or a JWT-authenticated
    supplier cabinet session — self-service access to a supplier's own
    products/uploads. This only gates entry; the view itself must still
    scope any supplier_id/object lookup to request.user when it's a
    Supplier, since these endpoints take an unscoped id/upload_id and
    have no per-object ownership check of their own."""

    message = "Requires a service token or supplier cabinet authentication."

    def has_permission(self, request, view):
        if HasServiceToken().has_permission(request, view):
            return True
        return isinstance(request.user, Supplier)


class IsPlatformAdmin(BasePermission):
    """core.User authenticated via JWT with the admin role."""

    message = "Requires an admin account."

    def has_permission(self, request, view):
        user = request.user
        return isinstance(user, User) and user.role == UserRole.ADMIN


class IsSupplierUser(BasePermission):
    """core.User authenticated via JWT with role=supplier — used for
    self-service actions scoped to supplier accounts only."""

    message = "Requires a supplier account."

    def has_permission(self, request, view):
        user = request.user
        return isinstance(user, User) and user.role == UserRole.SUPPLIER


class IsSupplierOrVenueUser(BasePermission):
    """core.User authenticated via JWT with role=supplier or role=venue_user
    — used by MeView.patch (2026-08-19: editing the personal profile was
    gated to role=supplier only since 2026-08-04, before the venue cabinet
    mirror existed (2026-08-15); nothing in update_own_profile is actually
    supplier-specific, venue_user employees just had no way to call it —
    found via a real report: "не могу менять профиль у сотрудника заведения,
    пишет доступно только под supplier")."""

    message = "Requires a supplier or venue account."

    def has_permission(self, request, view):
        user = request.user
        return isinstance(user, User) and user.role in (UserRole.SUPPLIER, UserRole.VENUE_USER)


class IsPlatformAdminOrSupplier(BasePermission):
    """core.User authenticated via JWT with the admin or supplier role —
    used by /suppliers/inn-lookup: admins use it to check duplicates before
    onboarding a company by hand, suppliers use it to autofill their own
    company-creation form (2026-07-27 registry-fields work assumed this was
    already open to suppliers; it wasn't — found 2026-07-28 when Vitya's
    supplier-role JWT got a hard 403 calling it)."""

    message = "Requires an admin or supplier account."

    def has_permission(self, request, view):
        user = request.user
        return isinstance(user, User) and user.role in (UserRole.ADMIN, UserRole.SUPPLIER)


class IsPlatformAdminOrVenueUser(BasePermission):
    """Mirrors IsPlatformAdminOrSupplier — used by /venues/inn-lookup and
    /venues/address-lookup (2026-08-19, Vitya asked for the venue side of
    the same dadata-backed autofill suppliers already have)."""

    message = "Requires an admin or venue account."

    def has_permission(self, request, view):
        user = request.user
        return isinstance(user, User) and user.role in (UserRole.ADMIN, UserRole.VENUE_USER)


class IsVenueMember(BasePermission):
    """Allows the shared service token (bot/n8n), admin accounts (act across
    all venues), a venue_user scoped to their own legacy venue_id (from
    view.kwargs) — never someone else's — or an active VenueUser membership
    (owner/manager) of that venue.

    Without this, any authenticated core.User of any role — found via a
    2026-07-14 review — could pass an arbitrary venue_id in the URL and
    read/write another venue's cart/orders/favorites (IDOR).

    The legacy `core.User.venue` check is the original (2026-07-14)
    mechanism, set only by hand in Django admin. The 2026-08-15 venue
    cabinet mirror added real self-service venue creation/joining
    (`VenueUser`, `POST /venues/mine`, invites) but never wired it to this
    permission, leaving every self-service or invited venue account unable
    to reach favorites/cart/orders/product-matrix/lists (403 on every
    request) — found via live dev-stand verification 2026-09-02. Checking
    `VenueUser` here (mirrors `_user_manages_venue`, used by
    HasMasterCatalogAccess/HasSupplierProductVenuePriceAccess) closes that
    gap; the legacy check stays for any pre-existing admin-assigned
    account that has no matching VenueUser row."""

    message = "Requires a service token, staff account, or membership of this venue."

    def has_permission(self, request, view):
        if HasServiceToken().has_permission(request, view):
            return True

        user = request.user
        if not isinstance(user, User):
            return False
        if user.role == UserRole.ADMIN:
            return True

        venue_id = view.kwargs.get("venue_id")
        if user.role == UserRole.VENUE_USER and str(user.venue_id) == str(venue_id):
            return True
        return _user_manages_venue(user, venue_id)


class CanInitiateAccountTransfer(BasePermission):
    """Allows the shared service token (bot/n8n), staff accounts, or any of
    the supplier's own active members (owner or manager — identical rights
    since 2026-08-05) to initiate an account transfer. Despite the narrow
    name this is the general-purpose "team management" gate, used well
    beyond account-transfer — see call sites in views.py."""

    message = "Requires a service token, staff account, or active membership of this supplier."

    def has_permission(self, request, view):
        if HasServiceToken().has_permission(request, view):
            return True

        user = request.user
        if not isinstance(user, User):
            return False
        if user.role == UserRole.ADMIN:
            return True

        supplier_id = view.kwargs.get("supplier_id")
        return SupplierUser.objects.filter(
            supplier_id=supplier_id,
            user=user,
            status=EntityStatus.ACTIVE,
        ).exists()


class CanRestoreSupplier(BasePermission):
    """Restoring a deleted (archived) company is as consequential as
    transferring it (re-publishes it, re-opens cabinet access for
    everyone on the team) — same bar as CanInitiateAccountTransfer:
    any of the supplier's own active members (owner or manager — identical
    rights since 2026-08-05), or platform staff. No service-token bypass
    here, unlike account transfer — nothing bot/n8n-driven needs to
    restore a company on a supplier's behalf."""

    message = "Requires a staff account or active membership of this supplier."

    def has_permission(self, request, view):
        user = request.user
        if not isinstance(user, User):
            return False
        if user.role == UserRole.ADMIN:
            return True

        supplier_id = view.kwargs.get("supplier_id")
        return SupplierUser.objects.filter(
            supplier_id=supplier_id,
            user=user,
            status=EntityStatus.ACTIVE,
        ).exists()


def is_current_supplier_owner_or_staff(request, supplier_id):
    """2026-08-06 (review finding, confirmed by explicit user decision): under
    CanInitiateAccountTransfer's flat owner/manager parity, any active member could
    grant role=owner to anyone — including themselves — which the same-day
    single-owner invariant turned into an instant, unilateral takeover (self-promote
    strips the real owner of their title with zero confirmation — 2026-08-07: demotes
    to manager, not a company-wide eviction, but still not theirs to decide unilaterally).
    Granting ownership is carved out
    as the one action that stays owner-only: a manager can still do everything else
    an owner can, but cannot make someone (including themselves) the new owner.
    Service token and staff (role=admin) bypass, same as CanInitiateAccountTransfer.
    Used wherever a request body can set role=owner: SupplierUserRoleChangeView,
    SupplierUserInviteCreateView, SupplierInviteLinkView (PATCH)."""
    if HasServiceToken().has_permission(request, None):
        return True

    user = request.user
    if not isinstance(user, User):
        return False
    if user.role == UserRole.ADMIN:
        return True

    return SupplierUser.objects.filter(
        supplier_id=supplier_id,
        user=user,
        role=SupplierUserRole.OWNER,
        status=EntityStatus.ACTIVE,
    ).exists()


class IsVenueCabinet(BasePermission):
    """JWT-authenticated venue cabinet session — mirrors IsSupplierCabinet
    (see VenueCabinetJWTAuthentication)."""

    message = "Cabinet authentication required."

    def has_permission(self, request, view):
        return isinstance(request.user, Venue)


def is_venue_cabinet_active_member(request):
    """Mirrors is_supplier_cabinet_active_member — is the acting user, per
    the cabinet JWT's `user_id` claim, an active member (owner or manager)
    of request.user (a Venue)?"""
    if not isinstance(request.user, Venue):
        return False
    user_id = request.auth.get("user_id") if request.auth else None
    if not user_id:
        return False
    return VenueUser.objects.filter(
        venue=request.user,
        user_id=user_id,
        status=EntityStatus.ACTIVE,
    ).exists()


class IsVenueCabinetActiveMember(BasePermission):
    """Mirrors IsSupplierCabinetActiveMember."""

    message = "Requires active membership of this venue (log in via the web cabinet)."

    def has_permission(self, request, view):
        return is_venue_cabinet_active_member(request)


class IsVenueCabinetOrHasServiceToken(BasePermission):
    """Mirrors IsSupplierCabinetOrHasServiceToken."""

    message = "Requires a service token or venue cabinet authentication."

    def has_permission(self, request, view):
        if HasServiceToken().has_permission(request, view):
            return True
        return isinstance(request.user, Venue)


class CanInitiateVenueAccountTransfer(BasePermission):
    """Mirrors CanInitiateAccountTransfer — general-purpose venue team
    management gate (invites, join requests, user list/revoke/role-change,
    invite-link)."""

    message = "Requires a service token, staff account, or active membership of this venue."

    def has_permission(self, request, view):
        if HasServiceToken().has_permission(request, view):
            return True

        user = request.user
        if not isinstance(user, User):
            return False
        if user.role == UserRole.ADMIN:
            return True

        venue_id = view.kwargs.get("venue_id")
        return VenueUser.objects.filter(
            venue_id=venue_id,
            user=user,
            status=EntityStatus.ACTIVE,
        ).exists()


class CanRestoreVenue(BasePermission):
    """Mirrors CanRestoreSupplier — no service-token bypass."""

    message = "Requires a staff account or active membership of this venue."

    def has_permission(self, request, view):
        user = request.user
        if not isinstance(user, User):
            return False
        if user.role == UserRole.ADMIN:
            return True

        venue_id = view.kwargs.get("venue_id")
        return VenueUser.objects.filter(
            venue_id=venue_id,
            user=user,
            status=EntityStatus.ACTIVE,
        ).exists()


def is_current_venue_owner_or_staff(request, venue_id):
    """Mirrors is_current_supplier_owner_or_staff — granting role=owner is
    carved out as owner-only, everything else is owner/manager parity.
    Used wherever a request body can set role=owner: VenueUserRoleChangeView,
    VenueUserInviteCreateView, VenueInviteLinkView (PATCH)."""
    if HasServiceToken().has_permission(request, None):
        return True

    user = request.user
    if not isinstance(user, User):
        return False
    if user.role == UserRole.ADMIN:
        return True

    return VenueUser.objects.filter(
        venue_id=venue_id,
        user=user,
        role=VenueUserRole.OWNER,
        status=EntityStatus.ACTIVE,
    ).exists()


class IsVenueUser(BasePermission):
    """core.User authenticated via JWT with role=venue_user — mirrors
    IsSupplierUser, used to gate accept-invite/join-via-link to venue
    accounts specifically."""

    message = "Requires a venue account."

    def has_permission(self, request, view):
        user = request.user
        return isinstance(user, User) and user.role == UserRole.VENUE_USER


def _user_has_master_catalog_access(user, owner_id):
    """Spec 2026-08-31 §3.1 (rule 14): access to a MasterProduct is granted
    to its owner directly, or to any core.User with an active VenueUser
    row (role owner or manager) in ANY venue currently owned by that
    owner — not just the venue where a particular VenueProductMatrixItem
    link happens to live, since the master catalog is inherently
    cross-company."""
    if str(user.id) == str(owner_id):
        return True

    owner_venue_ids = VenueUser.objects.filter(
        user_id=owner_id, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE
    ).values_list("venue_id", flat=True)
    return VenueUser.objects.filter(
        user=user,
        venue_id__in=owner_venue_ids,
        role__in=[VenueUserRole.OWNER, VenueUserRole.MANAGER],
        status=EntityStatus.ACTIVE,
    ).exists()


class HasMasterCatalogAccessToOwner(BasePermission):
    """Gates every view that needs owner-scoped Master Catalog access:
    MasterProductListView (list/create), MasterProductBulkCreateView,
    MasterProductBulkDeleteView, and MasterProductImportView — owner_id is
    given explicitly by the caller (query param on GET, body field
    otherwise) since there's no venue_id in the URL to derive it from.

    A missing or malformed owner_id is a request-*shape* problem, not an
    authorization problem: MasterProductListQuerySerializer (GET) and
    MasterProductCreateSerializer (POST) already reject it with a 400 in
    the view. So this permission defers to the view (returns True) rather
    than reporting a misleading 403 in that case — which also closes the
    admin bypass where UserRole.ADMIN's unconditional `return True` used to
    let a malformed owner_id reach master_products_queryset() completely
    unvalidated and crash with an uncaught 500 (2026-09-01 fix wave, Fix 1).
    request.data can be a non-dict (e.g. a JSON array body) — guard that
    explicitly and deny (403), since there's nothing sensible to defer to
    in that case (2026-09-01 fix wave, Fix 6). The check is against
    Mapping, not dict, specifically because MasterProductImportView is a
    multipart/form-data upload — DRF parses that into a QueryDict, which
    behaves like a mapping (and supports the .get("owner_id") call above)
    but is not itself a dict subclass, so isinstance(request.data, dict)
    would wrongly deny every file-import request."""

    message = "Requires ownership of, or manager/owner access to, this account's master catalog."

    def has_permission(self, request, view):
        user = request.user
        if not isinstance(user, User):
            return False
        if user.role == UserRole.ADMIN:
            return True

        if request.method == "GET":
            owner_id = request.query_params.get("owner_id")
        else:
            if not isinstance(request.data, Mapping):
                return False
            owner_id = request.data.get("owner_id")

        if not owner_id:
            return True
        try:
            uuid.UUID(str(owner_id))
        except (ValueError, TypeError):
            return True
        return _user_has_master_catalog_access(user, owner_id)


class HasTenderAccessToOwner(BasePermission):
    """Gates TenderListView (list/create) — same owner-scoped-account model
    as HasMasterCatalogAccessToOwner above (raw ТЗ "Тендеры" §2: "Покупатель
    — пользователь учётной записи АвтоСнаб", same account concept as
    MasterProduct.owner), reuses the same _user_has_master_catalog_access
    check — "account" access isn't a master-catalog-specific concept, it's
    the same owner/manager-of-any-of-this-account's-venues rule. owner_id
    given explicitly (query param on GET, body field on POST), same
    missing/malformed-owner_id-defers-to-the-view reasoning as above."""

    message = "Requires ownership of, or manager/owner access to, this account's tenders."

    def has_permission(self, request, view):
        user = request.user
        if not isinstance(user, User):
            return False
        if user.role == UserRole.ADMIN:
            return True

        if request.method == "GET":
            owner_id = request.query_params.get("owner_id")
        else:
            if not isinstance(request.data, Mapping):
                return False
            owner_id = request.data.get("owner_id")

        if not owner_id:
            return True
        try:
            uuid.UUID(str(owner_id))
        except (ValueError, TypeError):
            return True
        return _user_has_master_catalog_access(user, owner_id)


class HasTenderAccess(BasePermission):
    """Gates every tender_id-scoped buyer-side view added by the minimal
    end-to-end slice (items, publish, distribution, offers-list) — mirrors
    HasMasterCatalogAccess: resolves owner_id from the Tender identified by
    view.kwargs["tender_id"], not from a request param, since these URLs
    never carry an explicit owner_id."""

    message = "Requires ownership of, or manager/owner access to, this tender's account."

    def has_permission(self, request, view):
        user = request.user
        if not isinstance(user, User):
            return False
        if user.role == UserRole.ADMIN:
            return True

        tender_id = view.kwargs.get("tender_id")
        tender = Tender.objects.filter(pk=tender_id).first()
        if tender is None:
            return False
        return _user_has_master_catalog_access(user, tender.created_by_id)


class HasMasterCatalogAccess(BasePermission):
    """Gates MasterProductDetailView/MasterProductVenueBreakdownView/
    MasterProductVenueLinkView — resolves owner_id from the MasterProduct
    identified by view.kwargs["master_product_id"]."""

    message = "Requires ownership of, or manager/owner access to, this master product's account."

    def has_permission(self, request, view):
        user = request.user
        if not isinstance(user, User):
            return False
        if user.role == UserRole.ADMIN:
            return True

        master_product_id = view.kwargs.get("master_product_id")
        master_product = MasterProduct.objects.filter(pk=master_product_id).first()
        if master_product is None:
            return False
        return _user_has_master_catalog_access(user, master_product.owner_id)


class HasMasterProductAuditLogAccess(BasePermission):
    """Gates MasterProductAuditLogView. Unlike HasMasterCatalogAccess, the
    MasterProduct here may already be hard deleted (delete_master_product
    is a real .delete(), spec §4.5) — there is no live row to resolve
    owner_id from in that case. Falls back to AuditLog.scope_id, snapshotted
    at delete time (see record_audit_log call in delete_master_product).
    Mirrors MasterProductDetailView's blanket 403 — doesn't distinguish "no
    such id" from "not yours", same reasoning as HasMasterCatalogAccess."""

    message = "Requires ownership of, or manager/owner access to, this master product's account."

    def has_permission(self, request, view):
        user = request.user
        if not isinstance(user, User):
            return False
        if user.role == UserRole.ADMIN:
            return True

        master_product_id = view.kwargs.get("master_product_id")
        master_product = MasterProduct.objects.filter(pk=master_product_id).first()
        if master_product is not None:
            return _user_has_master_catalog_access(user, master_product.owner_id)

        scope_id = (
            AuditLog.objects.filter(entity_type=AuditLogEntityType.MASTER_PRODUCT, entity_id=master_product_id)
            .exclude(scope_id__isnull=True)
            .values_list("scope_id", flat=True)
            .first()
        )
        if scope_id is None:
            return False
        return _user_has_master_catalog_access(user, scope_id)


def _user_manages_venue(user, venue_id):
    return VenueUser.objects.filter(
        user=user,
        venue_id=venue_id,
        role__in=[VenueUserRole.OWNER, VenueUserRole.MANAGER],
        status=EntityStatus.ACTIVE,
    ).exists()


def user_can_access_venue(user, venue_id):
    """2026-09-08: same rule as IsVenueMember.has_permission, minus the
    service-token branch (used from view bodies where venue_id comes from a
    request body, not view.kwargs — e.g. cart submit/save-as-template once
    the cart itself stopped being venue-scoped)."""
    if user.role == UserRole.ADMIN:
        return True
    if user.role == UserRole.VENUE_USER and str(user.venue_id) == str(venue_id):
        return True
    return _user_manages_venue(user, venue_id)


def user_managed_venue_ids(user):
    """2026-09-08: the set of venue ids `user` can act on, for validating a
    venue-owned reference (subdivision/off_system_supplier) when there is no
    single fixed venue_id in scope any more (account-scoped cart items).
    Returns None as a sentinel for "no restriction" (admin — every venue)."""
    if user.role == UserRole.ADMIN:
        return None
    venue_ids = set(
        VenueUser.objects.filter(
            user=user,
            role__in=[VenueUserRole.OWNER, VenueUserRole.MANAGER],
            status=EntityStatus.ACTIVE,
        ).values_list("venue_id", flat=True)
    )
    if user.role == UserRole.VENUE_USER and user.venue_id:
        venue_ids.add(user.venue_id)
    return venue_ids


def _is_uuid(value):
    """2026-09-01 final-review fix wave (C3): never hand an unvalidated
    user-supplied value to an ORM filter on a UUID column — Django raises
    django.core.exceptions.ValidationError from inside the query, which no
    DRF handler catches, so it surfaces as an uncaught 500."""
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


class HasSupplierProductVenuePriceAccess(BasePermission):
    """Gates SupplierProductVenuePriceListView (read-only — venues can see
    individual prices that apply to them). 2026-09-07: writing an individual
    price moved to the supplier's own cabinet (Павел: "actor — поставщик"),
    this class now only ever sees GET.

    Malformed input is handled exactly like HasMasterCatalogAccessToOwner
    handles a malformed owner_id (2026-09-01 final-review fix wave, C3): a
    venue_id that isn't a UUID defers (returns True) instead of crashing on
    an ORM UUID cast — SupplierProductVenuePriceQuerySerializer rejects it
    with a 400 in the view.

    A GET without venue_id is a 403 (Ruling 2 of the plan) — the filter is
    mandatory, listing unscoped prices must never be allowed."""

    message = "Requires owner/manager membership of the venue in this request."

    def has_permission(self, request, view):
        user = request.user
        if not isinstance(user, User):
            return False
        if user.role == UserRole.ADMIN:
            return True

        venue_id = request.query_params.get("venue_id")
        if not venue_id:
            return False
        if not _is_uuid(venue_id):
            return True

        return _user_manages_venue(user, venue_id)


class HasSupplierProductVenuePriceObjectAccess(BasePermission):
    """Gates SupplierProductVenuePriceAuditLogView — resolves the venue from
    the SupplierProductVenuePrice identified by view.kwargs["price_id"].

    A malformed price_id is denied outright rather than deferred (unlike
    HasSupplierProductVenuePriceAccess above): this endpoint takes no
    request body and has no serializer downstream that would turn it into a
    400, so there is nothing to defer to (2026-09-01 final-review fix wave,
    C3)."""

    message = "Requires owner/manager membership of the venue this price applies to."

    def has_permission(self, request, view):
        user = request.user
        if not isinstance(user, User):
            return False
        if user.role == UserRole.ADMIN:
            return True

        price_id = view.kwargs.get("price_id")
        if not _is_uuid(price_id):
            return False
        price = SupplierProductVenuePrice.objects.filter(pk=price_id).first()
        if price is None:
            return False
        return _user_manages_venue(user, price.venue_id)
