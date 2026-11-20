"""QA-only harness for manually verifying the 2026-08-01 "share access"
work (owner-only checks on delete/bank-accounts/registry fields, team
list, revoke access) against the real running backend — not a mock.

Every view here 404s unless settings.DEBUG is True. This must never be
reachable on a deployed environment: the seed endpoint creates live
Supplier/core.User rows and hands back working JWTs with no
authentication of its own."""

import uuid

from django.http import Http404
from django.shortcuts import render
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import EntityStatus, User, UserRole
from suppliers.models import Supplier, SupplierUser, SupplierUserRole


def _require_debug():
    from django.conf import settings

    if not settings.DEBUG:
        raise Http404


def qa_share_access_page(request):
    _require_debug()
    return render(request, "qa_share_access.html")


class QAShareAccessSeedView(APIView):
    """POST creates a fresh Supplier with one owner and one manager
    SupplierUser, and returns core.User JWTs for both — the page then
    exchanges each for a cabinet JWT itself via the real
    cabinet/session-from-login endpoint, exactly like a real login would."""

    authentication_classes = []
    permission_classes = []

    @extend_schema(exclude=True)
    def post(self, request):
        _require_debug()

        stamp = uuid.uuid4().hex[:8]
        supplier = Supplier.objects.create(
            display_name=f"QA Share Access {stamp}",
            legal_name=f"QA Share Access {stamp} LLC",
            status=EntityStatus.ACTIVE,
        )
        owner = User.objects.create(email=f"qa-owner-{stamp}@example.com", full_name="QA Owner", role=UserRole.SUPPLIER)
        manager = User.objects.create(
            email=f"qa-manager-{stamp}@example.com", full_name="QA Manager", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=supplier, user=owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        SupplierUser.objects.create(
            supplier=supplier, user=manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )

        return Response(
            {
                "supplier_id": str(supplier.id),
                "supplier_display_name": supplier.display_name,
                "owner": {
                    "user_id": str(owner.id),
                    "email": owner.email,
                    "core_jwt": str(RefreshToken.for_user(owner).access_token),
                },
                "manager": {
                    "user_id": str(manager.id),
                    "email": manager.email,
                    "core_jwt": str(RefreshToken.for_user(manager).access_token),
                },
            },
            status=201,
        )
