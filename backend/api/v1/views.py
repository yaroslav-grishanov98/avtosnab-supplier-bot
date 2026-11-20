import json
import logging
import os
import uuid
from collections import defaultdict

from django.conf import settings
from django.core.cache import cache
from django.core.mail import mail_admins
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.suppliers.selectors import supplier_products_queryset
from api.v1.suppliers.serializers import (
    SupplierPriceAIApplySerializer,
    SupplierPriceUploadFileSerializer,
    SupplierPriceUploadTextSerializer,
    SupplierProductUpdateSerializer,
)
from api.v1.suppliers.services import (
    apply_supplier_price_ai_result,
    archive_supplier_product,
    create_supplier_price_upload_from_file,
    create_supplier_price_upload_from_text,
    get_supplier_cabinet_snapshot,
    start_supplier_price_upload_normalization,
    update_supplier_product,
)
from api.v1.validators import mask_inn, validate_inn
from catalog.models import ProductCategory
from core.models import EntityStatus, Unit
from suppliers.models import (
    CabinetSession,
    Supplier,
    SupplierOwnershipRequest,
    SupplierPriceUpload,
    SupplierProduct,
)

logger = logging.getLogger(__name__)


class HealthView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Служебное"],
        summary="Healthcheck",
        description="Проверка доступности сервиса. Авторизация не требуется.",
        auth=[],
        responses={200: {"type": "object", "properties": {"status": {"type": "string", "example": "ok"}}}},
    )
    def get(self, request):
        return Response({"status": "ok"})


def price_list_crud(request):
    return render(request, "price_list_crud.html")


def team_access_page(request):
    """Standalone tool (2026-08-02): team list + invite by email + revoke
    access, for a company owner. Static markup only — auth (email+password
    via /api/v1/auth/supplier/login) and every action happen client-side
    against the already-secured real API, same as any other frontend
    would; no separate permission check needed at this view level."""
    return render(request, "team_access.html")


def supplier_products_page(request, supplier_id):
    import zoneinfo

    moscow_tz = zoneinfo.ZoneInfo("Europe/Moscow")
    supplier = get_object_or_404(Supplier, pk=supplier_id)
    from suppliers.models import SupplierPriceUpload

    last_upload = (
        SupplierPriceUpload.objects.filter(supplier=supplier, status="applied").order_by("-uploaded_at").first()
    )
    last_upload_since = last_upload.uploaded_at if last_upload else None
    products_qs = supplier_products_queryset(
        {
            "supplier_id": str(supplier.pk),
            "status": "active",
            "last_upload_since": last_upload_since,
        }
    )
    products = []
    for p in products_qs:
        has_prev = p.previous_price is not None
        products.append(
            {
                "id": str(p.pk),
                "raw_name": p.supplier_name or "",
                "normalized_name": p.normalized_name or "",
                "price": str(p.current_price) if p.current_price is not None else "",
                "prev_price": str(p.previous_price) if p.previous_price is not None else "",
                "price_changed_at": p.price_changed_at.astimezone(moscow_tz).strftime("%d.%m.%Y %H:%M")
                if p.price_changed_at
                else "",
                "changed": has_prev,
                "unit": p.unit.code if p.unit else "",
                "unit_id": str(p.unit.pk) if p.unit else "",
                "package": p.package or "",
                "min_order_qty": str(p.min_order_qty) if p.min_order_qty is not None else "",
                "delivery_multiple": str(p.delivery_multiple) if p.delivery_multiple is not None else "",
                "comment": p.comment or "",
                "system_sku": p.system_sku or "",
                "brand": p.brand.canonical_name if p.brand else "",
                "group": p.category.parent.parent.name
                if p.category and p.category.parent and p.category.parent.parent
                else "",
                "subgroup": p.category.parent.name if p.category and p.category.parent else "",
                "product_type": p.category.name if p.category else "",
                "image_url": p.image_url or "",
            }
        )
    products.sort(key=lambda x: (not x["changed"], x["raw_name"]))
    units = list(Unit.objects.filter(status=EntityStatus.ACTIVE).order_by("code").values_list("id", "code"))
    units_json = json.dumps([{"id": str(u[0]), "code": u[1]} for u in units], ensure_ascii=False)

    # category tree: {group: {subgroup: [vid, ...]}}
    category_tree = defaultdict(lambda: defaultdict(list))
    leaf_cats = (
        ProductCategory.objects.select_related("parent__parent")
        .filter(status="active", parent__isnull=False, parent__parent__isnull=False)
        .order_by("parent__parent__name", "parent__name", "name")
    )
    for cat in leaf_cats:
        group = cat.parent.parent.name
        subgroup = cat.parent.name
        if cat.name not in category_tree[group][subgroup]:
            category_tree[group][subgroup].append(cat.name)
    category_tree_json = json.dumps({g: dict(s) for g, s in category_tree.items()}, ensure_ascii=False)

    return render(
        request,
        "supplier_products.html",
        {
            "supplier_id": str(supplier.pk),
            "supplier_name": supplier.display_name,
            "products_json": json.dumps(products, ensure_ascii=False),
            "products_count": len(products),
            "units_json": units_json,
            "category_tree_json": category_tree_json,
        },
    )


def _render_cabinet(request, supplier, binding=None):
    snapshot = get_supplier_cabinet_snapshot(supplier)
    return render(
        request,
        "cabinet.html",
        {
            "supplier": supplier,
            "binding": binding,
            "supplier_metadata_json": json.dumps(supplier.metadata or {}, ensure_ascii=False),
            "uploads": snapshot["uploads"],
            "products_count": snapshot["products_count"],
            "last_upload_at": snapshot["last_upload_at"],
            "supplier_page_url": f"/supplier/{supplier.pk}/",
            "yandex_maps_key": settings.YANDEX_MAPS_KEY,
        },
    )


def cabinet_view(request):
    from django.utils import timezone

    # Session-based access: allows F5 / page refresh after first token validation
    session_supplier_id = request.session.get("cabinet_supplier_id")
    if session_supplier_id:
        supplier = Supplier.objects.filter(pk=session_supplier_id).first()
        if supplier:
            return _render_cabinet(request, supplier)

    token = request.GET.get("token")
    if not token:
        return render(request, "cabinet.html", {"error": "Откройте кабинет через Telegram-бот."})

    session = (
        CabinetSession.objects.filter(token=token, used=False, expires_at__gt=timezone.now())
        .select_related("binding__supplier")
        .first()
    )

    if not session:
        return render(request, "cabinet.html", {"error": "Ссылка истекла или недействительна. Запросите новую в боте."})

    session.used = True
    session.save(update_fields=["used"])

    binding = session.binding
    supplier = binding.supplier

    # Store supplier in Django session so F5 works and cabinet_by_supplier_view is accessible
    request.session["cabinet_supplier_id"] = str(supplier.pk)
    allowed = set(request.session.get("cabinet_supplier_ids", []))
    allowed.add(str(supplier.pk))
    request.session["cabinet_supplier_ids"] = list(allowed)

    return _render_cabinet(request, supplier, binding=binding)


def cabinet_by_supplier_view(request, supplier_id):
    allowed_ids = request.session.get("cabinet_supplier_ids", [])
    if str(supplier_id) not in allowed_ids:
        return render(request, "cabinet.html", {"error": "Откройте кабинет через Telegram-бот."})

    supplier = get_object_or_404(Supplier, pk=supplier_id)
    return _render_cabinet(request, supplier)


@require_http_methods(["POST"])
def cabinet_update_proxy(request, supplier_id):
    supplier = get_object_or_404(Supplier, pk=supplier_id)
    body, err = _json_body(request)
    if err:
        return err
    # Клиентский JS (templates/cabinet.html) шлёт "min_order_amount"/"min_order_currency" —
    # это устоявшийся контракт легаси-кабинета, не переименовываем на фронте. На бэке это
    # соответствует Supplier.delivery_min_order_amount/delivery_currency (2026-07-28: поля
    # разъехались на delivery_*/pickup_*, но для этой формы актуален только курьерский минимум).
    allowed = {
        "display_name",
        "legal_name",
        "inn",
        "phone",
        "email",
        "delivery_terms",
        "min_order_amount",
        "min_order_currency",
    }
    for key, value in body.items():
        if key in allowed:
            if key == "min_order_amount":
                from decimal import Decimal, InvalidOperation

                try:
                    supplier.delivery_min_order_amount = Decimal(value) if value else None
                except (InvalidOperation, TypeError):
                    return JsonResponse({"detail": f"Invalid value for {key}"}, status=400)
            elif key == "min_order_currency":
                supplier.delivery_currency = value or "RUB"
            else:
                setattr(supplier, key, value or None)
    if "metadata" in body and isinstance(body["metadata"], dict):
        supplier.metadata = {**(supplier.metadata or {}), **body["metadata"]}
    supplier.save()
    return JsonResponse(
        {
            "display_name": supplier.display_name,
            "legal_name": supplier.legal_name,
            "inn": supplier.inn or "",
            "phone": supplier.phone or "",
            "email": supplier.email or "",
            "delivery_terms": supplier.delivery_terms or "",
            "min_order_amount": str(supplier.delivery_min_order_amount) if supplier.delivery_min_order_amount else "",
            "min_order_currency": supplier.delivery_currency,
            "metadata": supplier.metadata or {},
        }
    )


# ---------------------------------------------------------------------------
# Server-side proxy views for the supplier products page.
# The browser calls these endpoints (CSRF-protected, no token required).
# The SERVICE_API_TOKEN never leaves the server.
# ---------------------------------------------------------------------------

_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_IMAGE_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
_MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB


@require_http_methods(["POST"])
def supplier_product_image_upload(request, supplier_id):
    get_object_or_404(Supplier, pk=supplier_id)
    image_file = request.FILES.get("image")
    if not image_file:
        return JsonResponse({"detail": "Файл не передан."}, status=400)
    if image_file.content_type not in _ALLOWED_IMAGE_TYPES:
        return JsonResponse({"detail": "Допустимые форматы: JPEG, PNG, WebP, GIF."}, status=400)
    if image_file.size > _MAX_IMAGE_SIZE:
        return JsonResponse({"detail": "Максимальный размер файла — 5 МБ."}, status=400)

    ext = _IMAGE_EXTENSIONS[image_file.content_type]
    filename = f"{uuid.uuid4().hex}{ext}"
    rel_path = os.path.join("supplier_images", str(supplier_id), filename)
    abs_path = os.path.join(settings.MEDIA_ROOT, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "wb") as f:
        for chunk in image_file.chunks():
            f.write(chunk)

    image_url = settings.MEDIA_URL + rel_path
    return JsonResponse({"image_url": image_url})


def _json_body(request):
    try:
        return json.loads(request.body), None
    except (ValueError, TypeError) as e:
        return None, JsonResponse({"detail": str(e)}, status=400)


@require_http_methods(["PATCH"])
def supplier_product_update_proxy(request, supplier_id, product_id):
    get_object_or_404(Supplier, pk=supplier_id)
    get_object_or_404(SupplierProduct, pk=product_id, supplier_id=supplier_id)
    body, err = _json_body(request)
    if err:
        return err
    ser = SupplierProductUpdateSerializer(data=body, partial=True)
    if not ser.is_valid():
        return JsonResponse(ser.errors, status=400)
    try:
        update_supplier_product(supplier_product_id=product_id, data=ser.validated_data)
    except Exception as e:
        return JsonResponse({"detail": str(e)}, status=400)
    import zoneinfo

    moscow_tz = zoneinfo.ZoneInfo("Europe/Moscow")
    last_upload = (
        SupplierPriceUpload.objects.filter(supplier_id=supplier_id, status="applied")
        .order_by("-uploaded_at")
        .values_list("uploaded_at", flat=True)
        .first()
    )
    product = supplier_products_queryset({"last_upload_since": last_upload}).get(pk=product_id)
    return JsonResponse(
        {
            "id": str(product.pk),
            "supplier_raw_name": product.supplier_name or "",
            "supplier_normalized_name": product.normalized_name or "",
            "current_price": str(product.current_price) if product.current_price is not None else None,
            "previous_price": str(product.previous_price) if product.previous_price is not None else None,
            "price_changed_at": product.price_changed_at.astimezone(moscow_tz).strftime("%d.%m.%Y %H:%M")
            if product.price_changed_at
            else None,
            "unit": product.unit.code if product.unit else None,
            "package": product.package or "",
            "min_order_qty": str(product.min_order_qty) if product.min_order_qty is not None else None,
            "delivery_multiple": str(product.delivery_multiple) if product.delivery_multiple is not None else None,
            "comment": product.comment or "",
            "image_url": product.image_url or "",
            "brand": product.brand.canonical_name if product.brand else "",
        }
    )


@require_http_methods(["POST"])
def supplier_product_archive_proxy(request, supplier_id, product_id):
    get_object_or_404(Supplier, pk=supplier_id)
    get_object_or_404(SupplierProduct, pk=product_id, supplier_id=supplier_id)
    try:
        archive_supplier_product(supplier_product_id=product_id)
    except Exception as e:
        return JsonResponse({"detail": str(e)}, status=400)
    return JsonResponse({"status": "archived"})


@require_http_methods(["POST"])
def supplier_upload_text_proxy(request, supplier_id):
    supplier = get_object_or_404(Supplier, pk=supplier_id)
    body, err = _json_body(request)
    if err:
        return err
    body["supplier_id"] = str(supplier.pk)
    body.setdefault("source_channel", "web")
    ser = SupplierPriceUploadTextSerializer(data=body)
    if not ser.is_valid():
        return JsonResponse(ser.errors, status=400)
    try:
        upload = create_supplier_price_upload_from_text(data=ser.validated_data)
    except Exception as e:
        return JsonResponse({"detail": str(e)}, status=400)
    return JsonResponse({"upload_id": str(upload.pk)}, status=201)


@require_http_methods(["POST"])
def supplier_upload_apply_proxy(request, supplier_id, upload_id):
    get_object_or_404(Supplier, pk=supplier_id)
    body, err = _json_body(request)
    if err:
        return err
    ser = SupplierPriceAIApplySerializer(data=body)
    if not ser.is_valid():
        return JsonResponse(ser.errors, status=400)
    try:
        result = apply_supplier_price_ai_result(upload_id=upload_id, data=ser.validated_data)
    except Exception as e:
        return JsonResponse({"detail": str(e)}, status=400)
    return JsonResponse(result)


@require_http_methods(["GET"])
def supplier_product_price_history(request, supplier_id, product_id):
    import zoneinfo

    msk = zoneinfo.ZoneInfo("Europe/Moscow")
    get_object_or_404(Supplier, pk=supplier_id)
    product = get_object_or_404(SupplierProduct, pk=product_id, supplier_id=supplier_id)
    prices = product.prices.order_by("-valid_from").values("price", "valid_from", "valid_to")[:50]
    history = []
    for p in prices:
        history.append(
            {
                "price": str(p["price"]),
                "valid_from": p["valid_from"].astimezone(msk).strftime("%d.%m.%Y %H:%M"),
                "valid_to": p["valid_to"].astimezone(msk).strftime("%d.%m.%Y %H:%M") if p["valid_to"] else None,
            }
        )
    return JsonResponse(
        {
            "product_name": product.normalized_name or product.supplier_name or "",
            "history": history,
        }
    )


def _is_rate_limited(request, scope, limit=5, window=3600):
    ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "unknown")).split(",")[0].strip()
    key = f"rate:{scope}:{ip}"
    count = cache.get(key, 0)
    if count >= limit:
        return True
    cache.set(key, count + 1, window)
    return False


def _notify_admin_new_claim(supplier, full_name, email, role):
    role_labels = {
        "owner": "Запрос права владения",
        "employee": "Запрос сотрудника",
        "support": "Обращение в поддержку",
        "add_company": "Добавление новой компании",
    }
    subject = f"[АвтоСнаб] {role_labels.get(role, role)}: {supplier.display_name}"
    message = (
        f"Компания: {supplier.display_name}\n"
        f"ИНН: {supplier.inn or '—'}\n"
        f"Заявитель: {full_name} <{email}>\n"
        f"Тип заявки: {role_labels.get(role, role)}\n\n"
        f"Просмотр в админке:\n"
        f"/admin/suppliers/supplierownershiprequest/?status=pending"
    )
    try:
        mail_admins(subject, message, fail_silently=True)
    except Exception:
        logger.exception("Failed to send admin notification for claim")


def suppliers_list_view(request):
    suppliers = (
        Supplier.objects.filter(status="active")
        .order_by("display_name")
        .values("id", "display_name", "legal_name", "inn")
    )
    suppliers_data = [
        {
            "id": str(s["id"]),
            "display_name": s["display_name"],
            "legal_name": s["legal_name"] or "",
            "inn": s["inn"] or "",
            "inn_masked": mask_inn(s["inn"]),
        }
        for s in suppliers
    ]
    return render(request, "suppliers_list.html", {"suppliers": suppliers_data})


@require_http_methods(["GET", "POST"])
def supplier_add_view(request):
    if request.method == "POST":
        if _is_rate_limited(request, "add_company"):
            return render(request, "supplier_add.html", {"rate_limited": True})

        company_name = request.POST.get("company_name", "").strip()
        full_name = request.POST.get("full_name", "").strip()
        email = request.POST.get("email", "").strip()
        phone = request.POST.get("phone", "").strip()
        inn = request.POST.get("inn", "").strip()
        message = request.POST.get("message", "").strip()

        errors = {}
        if not company_name:
            errors["company_name"] = "Введите название компании."
        if not full_name:
            errors["full_name"] = "Введите ваше полное имя."
        if not email or "@" not in email:
            errors["email"] = "Введите корректный email."
        if not inn:
            errors["inn"] = "Введите ИНН компании."
        else:
            inn_err = validate_inn(inn)
            if inn_err:
                errors["inn"] = inn_err

        if not errors:
            # Active supplier already on platform → redirect to claim flow
            existing = Supplier.objects.filter(inn=inn.strip(), status="active").first()
            if existing:
                return render(
                    request,
                    "supplier_add.html",
                    {
                        "existing_supplier": existing,
                        "existing_inn_display": mask_inn(existing.inn),
                    },
                )

            # Pending add_company request from the same email for this INN → duplicate
            duplicate = SupplierOwnershipRequest.objects.filter(
                email=email,
                role_requested="add_company",
                status="pending",
                supplier__inn=inn.strip(),
            ).exists()
            if duplicate:
                return render(request, "supplier_add.html", {"duplicate": True})

            new_supplier = Supplier.objects.create(
                display_name=company_name,
                legal_name=company_name,
                inn=inn.strip(),
                status="inactive",
                metadata={"added_via_web": True},
            )
            SupplierOwnershipRequest.objects.create(
                supplier=new_supplier,
                full_name=full_name,
                email=email,
                phone=phone or None,
                inn=inn,
                role_requested="add_company",
                message=message or None,
            )
            _notify_admin_new_claim(new_supplier, full_name, email, "add_company")
            return redirect("/suppliers/add/?added=1")

        return render(
            request,
            "supplier_add.html",
            {
                "errors": errors,
                "form_data": request.POST,
            },
        )

    return render(
        request,
        "supplier_add.html",
        {
            "success": request.GET.get("added") == "1",
        },
    )


@require_http_methods(["POST"])
def supplier_upload_file_proxy(request, supplier_id):
    supplier = get_object_or_404(Supplier, pk=supplier_id)
    source_file = request.FILES.get("file")
    if not source_file:
        return JsonResponse({"detail": "Файл не передан."}, status=400)

    data = {
        "supplier_id": str(supplier.pk),
        "source_channel": "web",
        "source_file": source_file,
        "source_file_name": source_file.name,
    }
    ser = SupplierPriceUploadFileSerializer(data=data)
    if not ser.is_valid():
        first_error = next(iter(ser.errors.values()), ["Ошибка валидации"])[0]
        return JsonResponse({"detail": str(first_error)}, status=400)

    try:
        upload = create_supplier_price_upload_from_file(data=ser.validated_data)
    except Exception as e:
        return JsonResponse({"detail": str(e)}, status=400)

    try:
        start_supplier_price_upload_normalization(upload_id=upload.pk, use_ai=True)
    except Exception:
        pass  # normalization enqueue failure is non-fatal; upload record already exists

    return JsonResponse({"upload_id": str(upload.pk), "status": upload.status}, status=201)


@require_http_methods(["GET"])
def supplier_upload_status_proxy(request, supplier_id, upload_id):
    get_object_or_404(Supplier, pk=supplier_id)
    upload = get_object_or_404(SupplierPriceUpload, pk=upload_id, supplier_id=supplier_id)
    import zoneinfo

    msk = zoneinfo.ZoneInfo("Europe/Moscow")
    norm = (upload.raw_payload or {}).get("normalization", {}).get("result", {})
    return JsonResponse(
        {
            "status": upload.status,
            "items_total": norm.get("items_total", 0),
            "products_created": norm.get("products_created", 0),
            "prices_created": norm.get("prices_created", 0),
            "error_message": upload.error_message or "",
            "date": upload.uploaded_at.astimezone(msk).strftime("%d.%m.%Y %H:%M"),
            "file": upload.source_file_name or "файл",
        }
    )


@require_http_methods(["GET"])
def supplier_products_list_proxy(request, supplier_id):
    import zoneinfo

    moscow_tz = zoneinfo.ZoneInfo("Europe/Moscow")
    get_object_or_404(Supplier, pk=supplier_id)
    limit = min(int(request.GET.get("limit", 500)), 10000)
    last_upload = (
        SupplierPriceUpload.objects.filter(supplier_id=supplier_id, status="applied")
        .order_by("-uploaded_at")
        .values_list("uploaded_at", flat=True)
        .first()
    )
    qs = supplier_products_queryset(
        {
            "supplier_id": str(supplier_id),
            "status": "active",
            "last_upload_since": last_upload,
        }
    )
    results = []
    for p in qs[:limit]:
        has_prev = p.previous_price is not None
        results.append(
            {
                "id": str(p.pk),
                "supplier_raw_name": p.supplier_name or "",
                "supplier_normalized_name": p.normalized_name or "",
                "current_price": str(p.current_price) if p.current_price is not None else None,
                "prev_price": str(p.previous_price) if p.previous_price is not None else "",
                "price_changed_at": p.price_changed_at.astimezone(moscow_tz).strftime("%d.%m.%Y %H:%M")
                if p.price_changed_at
                else "",
                "changed": has_prev,
                "unit": p.unit.code if p.unit else None,
                "unit_id": str(p.unit.pk) if p.unit else None,
                "package": p.package or "",
                "min_order_qty": str(p.min_order_qty) if p.min_order_qty is not None else None,
                "delivery_multiple": str(p.delivery_multiple) if p.delivery_multiple is not None else None,
                "comment": p.comment or "",
                "brand": p.brand.canonical_name if p.brand else "",
                "group": p.category.parent.parent.name
                if p.category and p.category.parent and p.category.parent.parent
                else "",
                "subgroup": p.category.parent.name if p.category and p.category.parent else "",
                "product_type": p.category.name if p.category else "",
                "image_url": p.image_url or "",
                "category": p.category.name if p.category else "",
                "country": p.country_of_origin or "",
                "system_sku": p.system_sku or "",
            }
        )
    return JsonResponse({"results": results})
