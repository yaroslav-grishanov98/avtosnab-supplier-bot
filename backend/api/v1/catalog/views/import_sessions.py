"""Catalog import review sessions (Master/Venue)."""

import json
from types import SimpleNamespace

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
    extend_schema_view,
)
from rest_framework.exceptions import ValidationError
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.authentication import CoreUserJWTAuthentication
from api.v1.catalog.import_sessions import (
    build_venue_preview_rows,
    prepare_venue_import_matching,
)
from api.v1.catalog.selectors import (
    resolve_master_product_import_rows,
    resolve_venue_import_rows,
)
from api.v1.catalog.serializers import (
    CatalogImportSessionBusyResponseSerializer,
    CatalogImportSessionCommitResponseSerializer,
    CatalogImportSessionConflictResponseSerializer,
    CatalogImportSessionErrorSerializer,
    CatalogImportSessionItemPatchRequestSerializer,
    CatalogImportSessionItemPatchResponseSerializer,
    CatalogImportSessionResponseSerializer,
    MasterProductImportApplyDecisionSerializer,
    MasterProductImportApplyRowSerializer,
    MasterProductImportPreviewRowSerializer,
    VenueImportApplyRowSerializer,
    VenueImportDecisionSerializer,
    VenueImportFilePreviewRowSerializer,
)
from api.v1.catalog.services import (
    apply_master_product_import,
    apply_venue_import,
)
from api.v1.permissions import (
    IsCoreUser,
    _user_has_master_catalog_access,
    _user_manages_venue,
)
from catalog.models import (
    CatalogImportDomain,
    CatalogImportItem,
    CatalogImportItemAction,
    CatalogImportProcessingStatus,
    CatalogImportSession,
    CatalogImportSessionStatus,
    MasterProduct,
    VenueProductMatrixItem,
)
from core.models import (
    EntityStatus,
    Unit,
    Venue,
    VenueUser,
)
from file_imports.errors import import_error_messages

from .import_common import ImportSessionBusy


def _require_ready(session):
    if session.processing_status in (CatalogImportProcessingStatus.PENDING, CatalogImportProcessingStatus.PROCESSING):
        raise ImportSessionBusy("Импорт ещё обрабатывается.")
    if session.processing_status == CatalogImportProcessingStatus.FAILED:
        raise ImportSessionBusy("Обработка импорта завершилась ошибкой.")


def _import_target(session, target_id):
    """Карточка, на которую строка может ссылаться: master - активный MasterProduct владельца сессии; venue -
    активная позиция матрицы заведения сессии. Иначе None."""
    if session.domain == CatalogImportDomain.MASTER:
        return MasterProduct.objects.filter(pk=target_id, owner_id=session.owner_id, status=EntityStatus.ACTIVE).first()
    if session.venue_id is None:
        return None
    return VenueProductMatrixItem.objects.filter(
        pk=target_id, venue_id=session.venue_id, status=EntityStatus.ACTIVE
    ).first()


def _require_session_access(user, session):
    if session.domain == CatalogImportDomain.MASTER:
        allowed = user.role == "admin" or _user_has_master_catalog_access(user, session.owner_id)
    else:
        allowed = user.role == "admin" or (
            session.owner_id == user.id
            and (
                (
                    str(user.venue_id) == str(session.venue_id)
                    and not VenueUser.objects.filter(user=user, venue_id=session.venue_id).exists()
                )
                or _user_manages_venue(user, session.venue_id)
            )
        )
    if not allowed:
        raise Http404


def _batch_apply_session_items(session, items, user, prepared):
    """Fast path for valid batches; a rejected batch rolls back and falls back to per-row savepoints."""
    pending = [item for item in items if item.applied_at is None and item.action != CatalogImportItemAction.REVIEW]
    if len(pending) < 2:
        return [], []
    decision_serializer = (
        MasterProductImportApplyDecisionSerializer if session.domain == "master" else VenueImportDecisionSerializer
    )
    row_serializer = (
        MasterProductImportApplyRowSerializer if session.domain == "master" else VenueImportApplyRowSerializer
    )
    prepared_count = len(prepared["created"]) if prepared else 0
    try:
        with transaction.atomic():
            decisions = []
            for item in pending:
                from file_imports.canonical import effective_issues

                issues = effective_issues(item.parsed_snapshot)
                if issues:
                    raise ValidationError([issue["message"] for issue in issues])
                values = item.normalized_values or item.raw_values
                if not isinstance(values, dict):
                    raise ValidationError("Ожидается объект значений строки.")
                decision = dict(values) | {"action": item.action}
                target_key = "master_product_id" if session.domain == "master" else "matrix_item_id"
                decision.pop(target_key, None)
                decision.pop("expected_updated_at", None)
                if item.target_id:
                    if item.target_expected_updated_at is None:
                        raise ValidationError("Версия целевой карточки не сохранена.")
                    decision[target_key] = str(item.target_id)
                    decision["expected_updated_at"] = item.target_expected_updated_at
                typed = decision_serializer(data=decision)
                typed.is_valid(raise_exception=True)
                decisions.append(typed.validated_data)
            if session.domain == "master":
                # Previously applied create_new rows must also deduplicate a later partial commit.
                if any(item.applied_at and item.action == "create_new" for item in items):
                    return [], []
                applied = apply_master_product_import(owner=session.owner, decisions=decisions, actor=user)
            else:
                applied = apply_venue_import(
                    venue=session.venue,
                    user=user,
                    decisions=decisions,
                    present_matrix_item_ids=[item.target_id for item in items if item.target_id],
                    mark_missing=False,
                    prepared_matching=prepared,
                )
            serialized = json.loads(JSONRenderer().render(row_serializer(applied, many=True).data))
            results = []
            now = timezone.now()
            for item, row in zip(pending, serialized, strict=True):
                conflict = row.get("conflict")
                if conflict:
                    item.action = CatalogImportItemAction.REVIEW
                    item.validation_errors = [conflict]
                    result = {"status": "conflict", "errors": [conflict]}
                else:
                    item.applied_at = now
                    item.apply_result = row
                    from api.v1.catalog.import_sessions import record_catalog_receipt

                    record_catalog_receipt(session, item)
                    item.validation_errors = []
                    result = {"status": "skipped" if item.action == "skip" else "applied", "errors": [], "result": row}
                result.update(item_id=str(item.id), row_number=item.row_number)
                results.append(result)
            CatalogImportItem.objects.bulk_update(
                pending, ["applied_at", "apply_result", "validation_errors", "action"], batch_size=500
            )
            return [row for row in applied if not getattr(row, "conflict", None)], results
    except (ValidationError, DjangoValidationError, IntegrityError):
        if prepared:
            del prepared["created"][prepared_count:]
        for item in pending:
            item.refresh_from_db()
        return [], []


_IMPORT_SESSION_TAGS = ["Импорт справочников - сессия ревью"]


_IMPORT_SESSION_401 = OpenApiResponse(description="Нет JWT, либо JWT невалиден/просрочен.")


_IMPORT_SESSION_404 = OpenApiResponse(description="Сессии нет или она принадлежит другому пользователю.")


@extend_schema_view(
    get=extend_schema(
        operation_id="catalog_import_session_retrieve",
        tags=_IMPORT_SESSION_TAGS,
        summary="Сессия ревью импорта: статус, прогресс и строки",
        description=(
            "Общая сессия ревью для импорта Мастер-справочника и Справочника заведения. `processing_status` - "
            "фоновая обработка файла (pending -> processing -> ready | failed); пока она не ready, `items` может "
            "быть неполным, а commit и PATCH строки отвечают 409. include_items=false — лёгкий опрос без строк."
        ),
        parameters=[
            OpenApiParameter(
                name="include_items",
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                description="false — не сериализовать строки; по умолчанию true.",
            )
        ],
        responses={200: CatalogImportSessionResponseSerializer, 401: _IMPORT_SESSION_401, 404: _IMPORT_SESSION_404},
    ),
    post=extend_schema(
        operation_id="catalog_import_session_commit",
        tags=_IMPORT_SESSION_TAGS,
        summary="Применить решения сессии (commit)",
        description=(
            "Применяет готовые решения; возвращает item_results, remaining_count и conflicted_item_ids. "
            "При CATALOG_IMPORT_PARTIAL_COMMIT_ENABLED=true review-строки остаются в открытой сессии; "
            "иначе review блокирует commit (400). Конфликты/ошибки строк не закрывают сессию. "
            "Если применять нечего — 409. Применённые строки повторно не записываются."
        ),
        request=None,
        responses={
            200: CatalogImportSessionCommitResponseSerializer,
            400: CatalogImportSessionErrorSerializer,
            401: _IMPORT_SESSION_401,
            404: _IMPORT_SESSION_404,
            409: OpenApiResponse(
                response=CatalogImportSessionConflictResponseSerializer,
                description="Либо все строки с целью оказались изменёнными после preview (conflicted_item_ids), "
                'либо сессия ещё обрабатывается/упала: {"detail": "Импорт ещё обрабатывается." / "Обработка импорта '
                'завершилась ошибкой."} (CatalogImportSessionBusyResponse).',
            ),
        },
    ),
)
class CatalogImportSessionDetailView(APIView):
    """Shared review API for the new Master/Venue import sessions."""

    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    def _session(self, request, session_id, *, lock=False):
        queryset = CatalogImportSession.objects.all()
        if lock:
            queryset = queryset.select_for_update()
        session = get_object_or_404(queryset, pk=session_id)
        _require_session_access(request.user, session)
        return session

    def get(self, request, session_id):
        session = self._session(request, session_id)
        serializer = CatalogImportSessionResponseSerializer(session)
        if request.query_params.get("include_items", "true").lower() == "false":
            # Avoid serializing the entire preview during polling.
            serializer.fields.pop("items")
            payload = serializer.data
            payload["items"] = []
        else:
            payload = serializer.data
        return Response(payload)

    def post(self, request, session_id):
        session = self._session(request, session_id)
        _require_ready(session)
        prepared = None
        if session.domain == "venue" and getattr(settings, "VENUE_IMPORT_PREPARED_MATCHING_ENABLED", False):
            prepared = prepare_venue_import_matching(session)
        return self._commit(request, session_id, prepared)

    @transaction.atomic
    def _commit(self, request, session_id, prepared):
        session = self._session(request, session_id, lock=True)
        _require_ready(session)
        if session.status != CatalogImportSessionStatus.OPEN:
            raise ValidationError({"session": "Сессия уже завершена."})
        items = list(session.items.select_for_update().order_by("row_number", "id"))
        if not getattr(settings, "CATALOG_IMPORT_PARTIAL_COMMIT_ENABLED", False) and any(
            item.applied_at is None and item.action == CatalogImportItemAction.REVIEW for item in items
        ):
            raise ValidationError({"items": "Сначала примите решение по всем строкам."})
        if session.domain == "venue" and session.venue_id:
            Venue.objects.select_for_update().get(pk=session.venue_id)
        if prepared:
            candidate_ids = {candidate[0] for row in prepared["rows"].values() for candidate in row["candidates"]}
            list(MasterProduct.objects.select_for_update().filter(pk__in=candidate_ids).order_by("pk"))
        target_model = MasterProduct if session.domain == CatalogImportDomain.MASTER else VenueProductMatrixItem
        # Stable lock order closes the check/write race for all targets in this session.
        list(
            target_model.objects.select_for_update()
            .filter(pk__in=[item.target_id for item in items if item.target_id])
            .order_by("pk")
        )
        rows, results = _batch_apply_session_items(session, items, request.user, prepared)
        processed_ids = {result["item_id"] for result in results}
        conflicted = [result["item_id"] for result in results if result["status"] == "conflict"]
        replaced_targets = set()
        decision_serializer = (
            MasterProductImportApplyDecisionSerializer
            if session.domain == CatalogImportDomain.MASTER
            else VenueImportDecisionSerializer
        )
        row_serializer = (
            MasterProductImportApplyRowSerializer
            if session.domain == CatalogImportDomain.MASTER
            else VenueImportApplyRowSerializer
        )
        created_master_ids = {
            (row.normalized_values or row.raw_values).get("raw_name", "").strip().casefold(): row.apply_result.get(
                "master_product_id"
            )
            for row in items
            if row.applied_at and row.action == CatalogImportItemAction.CREATE_NEW
        }
        for item in items:
            if item.applied_at is not None or str(item.id) in processed_ids:
                continue
            result = {"item_id": str(item.id), "row_number": item.row_number, "status": "review", "errors": []}
            if item.action == CatalogImportItemAction.REVIEW:
                result["errors"] = item.validation_errors
                results.append(result)
                continue
            prepared_created_count = len(prepared["created"]) if prepared else 0
            try:
                with transaction.atomic():
                    if item.action == CatalogImportItemAction.REPLACE and item.target_id in replaced_targets:
                        raise ValidationError("Несколько строк заменяют одну карточку.")
                    from file_imports.canonical import effective_issues

                    issues = effective_issues(item.parsed_snapshot)
                    if issues:
                        raise ValidationError([issue["message"] for issue in issues])
                    values = item.normalized_values or item.raw_values
                    if not isinstance(values, dict):
                        raise ValidationError("Ожидается объект значений строки.")
                    decision = dict(values)
                    decision["action"] = item.action
                    target_key = (
                        "master_product_id" if session.domain == CatalogImportDomain.MASTER else "matrix_item_id"
                    )
                    decision.pop(target_key, None)
                    decision.pop("expected_updated_at", None)
                    if item.target_id:
                        decision[target_key] = str(item.target_id)
                        target = _import_target(session, item.target_id)
                        if target is None or target.updated_at != item.target_expected_updated_at:
                            result["status"] = "conflict"
                            raise ValidationError("Целевая карточка изменилась после preview.")
                        decision["expected_updated_at"] = item.target_expected_updated_at
                    serializer = decision_serializer(data=decision)
                    serializer.is_valid(raise_exception=True)
                    if session.domain == CatalogImportDomain.MASTER:
                        name_key = decision["raw_name"].strip().casefold()
                        duplicate_id = (
                            created_master_ids.get(name_key)
                            if item.action == CatalogImportItemAction.CREATE_NEW
                            else None
                        )
                        if duplicate_id:
                            mp = MasterProduct.objects.get(pk=duplicate_id, owner_id=session.owner_id)
                            applied = [
                                SimpleNamespace(
                                    raw_name=decision["raw_name"],
                                    action=item.action,
                                    master_product_id=mp.id,
                                    canonical_name=mp.canonical_name,
                                    unit_id=mp.unit_id,
                                    net_value=mp.net_value,
                                    note=mp.note,
                                    conflict=None,
                                )
                            ]
                        else:
                            applied = apply_master_product_import(
                                owner=session.owner, decisions=[serializer.validated_data], actor=request.user
                            )
                    else:
                        if session.venue_id is None:
                            raise ValidationError("Для venue-сессии не задано заведение.")
                        applied = apply_venue_import(
                            venue=session.venue,
                            user=request.user,
                            decisions=[serializer.validated_data],
                            present_matrix_item_ids=[row.target_id for row in items if row.target_id],
                            mark_missing=False,
                            prepared_matching=prepared,
                        )
                    if applied and getattr(applied[0], "conflict", None):
                        result["status"] = "conflict"
                        raise ValidationError(applied[0].conflict)
                    serialized_rows = json.loads(JSONRenderer().render(row_serializer(applied, many=True).data))
                    item.applied_at = timezone.now()
                    item.apply_result = serialized_rows[0] if serialized_rows else {}
                    if (
                        session.domain == "master"
                        and item.action == "create_new"
                        and item.apply_result.get("master_product_id")
                    ):
                        created_master_ids[name_key] = item.apply_result["master_product_id"]
                    item.validation_errors = []
                    item.save(update_fields=["applied_at", "apply_result", "validation_errors", "updated_at"])
                    from api.v1.catalog.import_sessions import record_catalog_receipt

                    record_catalog_receipt(session, item)
                    rows.extend(applied)
                    if item.action == CatalogImportItemAction.REPLACE:
                        replaced_targets.add(item.target_id)
                    result["status"] = "skipped" if item.action == CatalogImportItemAction.SKIP else "applied"
                    result["result"] = item.apply_result
            except (ValidationError, DjangoValidationError, IntegrityError) as error:
                if prepared:
                    del prepared["created"][prepared_created_count:]
                if isinstance(error, IntegrityError):
                    messages = ["Конфликт данных: карточка с такими уникальными полями уже существует."]
                else:
                    messages = import_error_messages(error)
                item.action = CatalogImportItemAction.REVIEW
                item.validation_errors = messages
                item.save(update_fields=["action", "validation_errors", "updated_at"])
                result["errors"] = messages
                if result["status"] == "conflict":
                    conflicted.append(str(item.id))
                else:
                    result["status"] = "error"
            results.append(result)
        if session.domain == CatalogImportDomain.VENUE and rows:
            present_ids = [item.target_id for item in items if item.target_id]
            present_ids += [
                item.apply_result.get("matrix_item_id") for item in items if item.apply_result.get("matrix_item_id")
            ]
            VenueProductMatrixItem.objects.filter(
                venue_id=session.venue_id, status=EntityStatus.ACTIVE, source="accounting_system"
            ).exclude(pk__in=present_ids).update(missing_from_latest_import=True)
            VenueProductMatrixItem.objects.filter(
                venue_id=session.venue_id, pk__in=present_ids, status=EntityStatus.ACTIVE, source="accounting_system"
            ).update(missing_from_latest_import=False)
        remaining = session.items.filter(applied_at__isnull=True).count()
        if not remaining:
            session.status = CatalogImportSessionStatus.COMMITTED
            session.save(update_fields=["status", "updated_at"])
        payload = CatalogImportSessionCommitResponseSerializer(
            {
                "session_id": session.id,
                "status": session.status,
                "domain": session.domain,
                "rows": rows,
                "remaining_count": remaining,
                "conflicted_item_ids": conflicted,
                "item_results": results,
            }
        ).data
        JSONRenderer().render(payload)  # Fail before leaving the write transaction.
        return Response(payload, status=409 if not rows and remaining else 200)


@extend_schema_view(
    patch=extend_schema(
        operation_id="catalog_import_session_item_update",
        tags=_IMPORT_SESSION_TAGS,
        summary="Изменить решение по строке сессии",
        request=CatalogImportSessionItemPatchRequestSerializer,
        responses={
            200: CatalogImportSessionItemPatchResponseSerializer,
            400: CatalogImportSessionErrorSerializer,
            401: _IMPORT_SESSION_401,
            403: OpenApiResponse(description="Строка чужой сессии."),
            404: OpenApiResponse(description="Строки нет."),
            409: OpenApiResponse(
                response=CatalogImportSessionBusyResponseSerializer,
                description='Сессия ещё обрабатывается или упала: {"detail": "Импорт ещё обрабатывается." / '
                '"Обработка импорта завершилась ошибкой."}.',
            ),
        },
    )
)
class CatalogImportSessionItemView(APIView):
    authentication_classes = [CoreUserJWTAuthentication]
    permission_classes = [IsCoreUser]

    @transaction.atomic
    def patch(self, request, session_id, item_id):
        session = CatalogImportSessionDetailView()._session(request, session_id, lock=True)
        item = get_object_or_404(session.items.select_for_update(), pk=item_id)
        _require_ready(session)
        if session.status != CatalogImportSessionStatus.OPEN:
            raise ValidationError({"session": "Сессия уже завершена."})
        if item.applied_at is not None:
            raise ImportSessionBusy("Уже применённую строку нельзя изменить.")
        serializer = CatalogImportSessionItemPatchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        previous_values = dict(item.normalized_values or item.raw_values)
        values = dict(previous_values)
        changed = data.get("raw_values", {}) | data.get("normalized_values", {})
        values.update(changed)
        # Validate partial row edits with the same typed fields as legacy apply.
        domain_serializer = (
            MasterProductImportApplyDecisionSerializer if session.domain == "master" else VenueImportDecisionSerializer
        )
        fields_serializer = domain_serializer(data=changed, partial=True)
        for field in ("raw_name", "unit_id", "net_value", "requested_unit_label", "note"):
            if field in changed and field in fields_serializer.fields:
                fields_serializer.fields[field].run_validation(changed[field])
        if (
            changed.get("unit_id")
            and not Unit.objects.filter(pk=changed["unit_id"], status=EntityStatus.ACTIVE).exists()
        ):
            raise ValidationError({"unit_id": "Единица не найдена или архивна."})
        if item.parsed_snapshot:
            from file_imports.canonical import override_snapshot

            keys = {"raw_name": "name", "unit_id": "unit_label", "requested_unit_label": "unit_label"}
            item.parsed_snapshot = override_snapshot(
                item.parsed_snapshot,
                {keys.get(key, key): value for key, value in changed.items()},
                at=timezone.now().isoformat(),
                actor_id=request.user.pk,
            )
            item.field_sources = dict(item.field_sources) | {key: "user" for key in changed}
        item.normalized_values = values
        if "raw_values" in data:
            item.raw_values = item.raw_values | data["raw_values"]
        semantic_change = any(
            values.get(key) != previous_values.get(key)
            for key in changed.keys() & {"raw_name", "unit_id", "requested_unit_label"}
        )
        if semantic_change and "action" not in data:
            item.action = CatalogImportItemAction.REVIEW
            item.target_id = None
            item.target_expected_updated_at = None
            if session.domain == "master":
                matched = resolve_master_product_import_rows(
                    owner=session.owner,
                    rows=[
                        {
                            "row_number": item.row_number,
                            "name": values.get("raw_name"),
                            "unit": Unit.objects.get(pk=values["unit_id"]).name if values.get("unit_id") else None,
                        }
                    ],
                )
                preview = MasterProductImportPreviewRowSerializer(matched[0]).data
            else:
                matched = resolve_venue_import_rows(
                    venue=session.venue,
                    raw_names=[values.get("raw_name")],
                    unit_labels=[values.get("requested_unit_label")],
                )
                preview = VenueImportFilePreviewRowSerializer(
                    build_venue_preview_rows(
                        [
                            {
                                "row_number": item.row_number,
                                "name": values.get("raw_name"),
                                "unit": values.get("requested_unit_label"),
                            }
                        ],
                        matched,
                    )[0]
                ).data
            item.candidates = json.loads(JSONRenderer().render(preview.get("candidates", [])))
        for field in ("field_sources", "candidates", "validation_errors", "action", "target_id"):
            if field in data:
                setattr(item, field, data[field])
        if item.action in (CatalogImportItemAction.CREATE_NEW, CatalogImportItemAction.SKIP):
            item.target_id = None
        if (
            item.action in (CatalogImportItemAction.UPDATE_EXISTING, CatalogImportItemAction.REPLACE)
            and item.target_id is None
        ):
            raise ValidationError({"target_id": f"Для {item.action} нужна цель."})
        if item.target_id is not None:
            target = _import_target(session, item.target_id)
            if target is None:
                raise ValidationError({"target_id": "Цель не найдена, чужая или архивная."})
            if "target_id" in data:
                item.target_expected_updated_at = target.updated_at
        else:
            item.target_expected_updated_at = None
        item.save()
        return Response(CatalogImportSessionItemPatchResponseSerializer(item).data)
