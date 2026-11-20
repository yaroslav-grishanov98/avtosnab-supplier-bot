"""
Extra tests for api/v1/suppliers/views.py — covers branches missing from the main test suite.

Covered:
- BotUserBindingRegisterView: binding conflict (409) and inactive binding reactivation
- BotPendingActionCreateView: GET → 404 when not found; POST with message_id lookup
- BotPendingActionDetailView: PATCH updates raw_payload
- BotUserStateCurrentView: GET when no state exists; PUT with invalid payload
- SupplierPriceUploadNormalizeView: sync mode with run_ai_cleanup=true (success + error paths)
- SupplierPriceUploadPreviewExcelView: GET returns Excel file
- CabinetTokenView: POST creates cabinet session token
- SupplierCabinetSessionView: exchange one-time token for JWT
- SupplierCabinetView: JWT-scoped cabinet snapshot GET/PATCH
"""

from datetime import timedelta
from io import BytesIO
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from core.models import EntityStatus, Unit, UnitKind, User, UserRole
from suppliers.models import (
    BotUserBinding,
    BotUserState,
    CabinetSession,
    SourceChannel,
    Supplier,
    SupplierBotLinkToken,
    SupplierPriceUpload,
    SupplierPriceUploadItem,
    SupplierProduct,
    SupplierUser,
    SupplierUserRole,
    UploadItemStatus,
    UploadStatus,
)

REGISTER_URL = "/api/v1/bot-user-bindings/register"
PENDING_URL = "/api/v1/bot-user-bindings/pending-actions"
STATE_URL = "/api/v1/bot-user-states/current"
CABINET_TOKEN_URL = "/api/v1/suppliers/cabinet/token"
CABINET_SESSION_URL = "/api/v1/suppliers/cabinet/session"
CABINET_SESSION_FROM_LOGIN_URL = "/api/v1/suppliers/cabinet/session-from-login"
CABINET_ME_URL = "/api/v1/suppliers/cabinet/me"


def _jwt_bearer(user):
    from rest_framework_simplejwt.tokens import RefreshToken

    return f"Bearer {RefreshToken.for_user(user).access_token}"


def _make_supplier(invite_code="CODE42", status=EntityStatus.ACTIVE, **kwargs):
    meta = {"invite_code": invite_code}
    return Supplier.objects.create(
        display_name=kwargs.pop("display_name", "TestSupplier"),
        legal_name=kwargs.pop("legal_name", "TestSupplier LLC"),
        status=status,
        metadata=meta,
        **kwargs,
    )


def _make_link_token(supplier, token, created_by=None):
    return SupplierBotLinkToken.objects.create(
        supplier=supplier, token=token, expires_at=timezone.now() + timedelta(minutes=30), created_by=created_by
    )


def _make_binding(supplier, chat_id="chat-1", channel=SourceChannel.TELEGRAM, status=EntityStatus.ACTIVE):
    return BotUserBinding.objects.create(
        supplier=supplier,
        channel=channel,
        chat_id=chat_id,
        external_user_id=None,
        username=None,
        status=status,
    )


def _make_upload(supplier, status=UploadStatus.APPLIED):
    return SupplierPriceUpload.objects.create(
        supplier=supplier,
        source_channel=SourceChannel.TELEGRAM,
        status=status,
        raw_payload={"kind": "text", "text": "test", "metadata": {"text_sha256": "abc"}},
    )


def _upload_normalize_url(upload_id):
    return f"/api/v1/suppliers/uploads/{upload_id}/normalize"


def _upload_preview_url(upload_id):
    return f"/api/v1/suppliers/uploads/{upload_id}/preview.xlsx"


@override_settings(SERVICE_API_TOKEN="test-token")
class BotUserBindingRegisterConflictTests(APITestCase):
    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def setUp(self):
        self.authenticate()

    def test_binding_conflict_returns_409(self):
        supplier_a = _make_supplier(display_name="Supplier A", legal_name="A LLC")
        supplier_b = _make_supplier(display_name="Supplier B", legal_name="B LLC")
        _make_link_token(supplier_b, "LINK-B")
        _make_binding(supplier_a, chat_id="chat-conflict")

        response = self.client.post(
            REGISTER_URL,
            {
                "platform": SourceChannel.TELEGRAM,
                "chat_id": "chat-conflict",
                "link_token": "LINK-B",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["code"], "BOT_USER_BINDING_CONFLICT")

    def test_binding_conflict_sets_error_state(self):
        supplier_a = _make_supplier(display_name="Supplier A1", legal_name="A1 LLC")
        supplier_b = _make_supplier(display_name="Supplier B1", legal_name="B1 LLC")
        _make_link_token(supplier_b, "LINK-B1")
        _make_binding(supplier_a, chat_id="chat-c2")

        self.client.post(
            REGISTER_URL,
            {
                "platform": SourceChannel.TELEGRAM,
                "chat_id": "chat-c2",
                "link_token": "LINK-B1",
            },
            format="json",
        )

        state = BotUserState.objects.filter(platform=SourceChannel.TELEGRAM, chat_id="chat-c2").first()
        self.assertIsNotNone(state)
        self.assertEqual(state.payload.get("error_code"), "BOT_USER_BINDING_CONFLICT")

    def test_inactive_binding_is_reactivated(self):
        supplier = _make_supplier()
        _make_link_token(supplier, "LINK-REACT")
        _make_binding(supplier, chat_id="chat-inactive", status=EntityStatus.ARCHIVED)

        response = self.client.post(
            REGISTER_URL,
            {
                "platform": SourceChannel.TELEGRAM,
                "chat_id": "chat-inactive",
                "link_token": "LINK-REACT",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        binding = BotUserBinding.objects.get(channel=SourceChannel.TELEGRAM, chat_id="chat-inactive")
        self.assertEqual(binding.status, EntityStatus.ACTIVE)

    def test_inactive_binding_reactivation_returns_success(self):
        supplier = _make_supplier()
        _make_link_token(supplier, "LINK-REACT2")
        _make_binding(supplier, chat_id="chat-inactive2", status=EntityStatus.ARCHIVED)

        response = self.client.post(
            REGISTER_URL,
            {
                "platform": SourceChannel.TELEGRAM,
                "chat_id": "chat-inactive2",
                "link_token": "LINK-REACT2",
            },
            format="json",
        )

        self.assertTrue(response.json()["success"])

    def test_new_binding_gets_link_token_creator_as_user(self):
        """2026-08-04: профиль поставщика показывает Telegram как системное
        поле "заполняется автоматически" — раньше BotUserBinding.user
        никогда не проставлялся, даже при создании."""
        supplier = _make_supplier()
        owner = User.objects.create(email="bind-owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        _make_link_token(supplier, "LINK-USER1", created_by=owner)

        response = self.client.post(
            REGISTER_URL,
            {"platform": SourceChannel.TELEGRAM, "chat_id": "chat-user1", "link_token": "LINK-USER1"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        binding = BotUserBinding.objects.get(channel=SourceChannel.TELEGRAM, chat_id="chat-user1")
        self.assertEqual(binding.user_id, owner.id)

    def test_relinking_existing_binding_updates_user_to_latest_creator(self):
        supplier = _make_supplier()
        first_owner = User.objects.create(email="bind-first@example.com", full_name="First", role=UserRole.SUPPLIER)
        second_owner = User.objects.create(email="bind-second@example.com", full_name="Second", role=UserRole.SUPPLIER)
        _make_link_token(supplier, "LINK-USER2A", created_by=first_owner)
        self.client.post(
            REGISTER_URL,
            {"platform": SourceChannel.TELEGRAM, "chat_id": "chat-user2", "link_token": "LINK-USER2A"},
            format="json",
        )

        _make_link_token(supplier, "LINK-USER2B", created_by=second_owner)
        response = self.client.post(
            REGISTER_URL,
            {"platform": SourceChannel.TELEGRAM, "chat_id": "chat-user2", "link_token": "LINK-USER2B"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        binding = BotUserBinding.objects.get(channel=SourceChannel.TELEGRAM, chat_id="chat-user2")
        self.assertEqual(binding.user_id, second_owner.id)

    def test_link_token_without_creator_leaves_binding_user_null(self):
        supplier = _make_supplier()
        _make_link_token(supplier, "LINK-NOUSER", created_by=None)

        response = self.client.post(
            REGISTER_URL,
            {"platform": SourceChannel.TELEGRAM, "chat_id": "chat-nouser", "link_token": "LINK-NOUSER"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        binding = BotUserBinding.objects.get(channel=SourceChannel.TELEGRAM, chat_id="chat-nouser")
        self.assertIsNone(binding.user_id)


@override_settings(SERVICE_API_TOKEN="test-token")
class BotPendingActionCreateViewExtraTests(APITestCase):
    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def setUp(self):
        self.authenticate()

    def test_get_returns_404_when_no_pending_action(self):
        response = self.client.get(
            PENDING_URL,
            {
                "platform": SourceChannel.TELEGRAM,
                "chat_id": "chat-empty",
                "action_type": "upload_price",
                "status": "pending_registration",
            },
        )

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertFalse(payload["exists"])
        self.assertEqual(payload["code"], "BOT_PENDING_ACTION_NOT_FOUND")

    def test_post_with_message_id_creates_action(self):
        response = self.client.post(
            PENDING_URL,
            {
                "platform": SourceChannel.TELEGRAM,
                "chat_id": "chat-msg",
                "action_type": "upload_price",
                "message_id": "msg-999",
                "status": "pending_registration",
            },
            format="json",
        )

        self.assertIn(response.status_code, (200, 201))
        payload = response.json()
        # Response is the serialized BotPendingAction directly
        self.assertEqual(payload["message_id"], "msg-999")

    def test_post_with_message_id_deduplicates_existing(self):
        from suppliers.models import BotPendingAction

        action = BotPendingAction.objects.create(
            platform=SourceChannel.TELEGRAM,
            chat_id="chat-dedup",
            action_type="upload_price",
            message_id="msg-dup",
            status="pending_registration",
        )

        response = self.client.post(
            PENDING_URL,
            {
                "platform": SourceChannel.TELEGRAM,
                "chat_id": "chat-dedup",
                "action_type": "upload_price",
                "message_id": "msg-dup",
                "status": "pending_registration",
            },
            format="json",
        )

        # Returns 200 (existing found), response is the action serialized directly
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], str(action.id))


@override_settings(SERVICE_API_TOKEN="test-token")
class BotPendingActionDetailRawPayloadTests(APITestCase):
    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def setUp(self):
        self.authenticate()
        from suppliers.models import BotPendingAction

        self.action = BotPendingAction.objects.create(
            platform=SourceChannel.TELEGRAM,
            chat_id="chat-raw",
            action_type="upload_price",
            status="pending_registration",
            raw_payload={"old_key": "old_val"},
        )

    def test_patch_raw_payload_updates_it(self):
        url = f"/api/v1/bot-user-bindings/pending-actions/{self.action.id}"

        response = self.client.patch(
            url,
            {
                "raw_payload": {"new_key": "new_val"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.action.refresh_from_db()
        self.assertEqual(self.action.raw_payload, {"new_key": "new_val"})

    def test_patch_empty_raw_payload_clears_it(self):
        url = f"/api/v1/bot-user-bindings/pending-actions/{self.action.id}"

        response = self.client.patch(
            url,
            {
                "raw_payload": {},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.action.refresh_from_db()
        self.assertEqual(self.action.raw_payload, {})


@override_settings(SERVICE_API_TOKEN="test-token")
class BotUserStateCurrentViewExtraTests(APITestCase):
    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def setUp(self):
        self.authenticate()

    def test_get_returns_start_state_when_no_state_exists(self):
        response = self.client.get(
            STATE_URL,
            {
                "platform": SourceChannel.TELEGRAM,
                "chat_id": "chat-no-state",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["exists"])
        self.assertEqual(payload["state"], "start")
        self.assertIsNone(payload["bot_user_state"])

    def test_put_with_invalid_payload_returns_400(self):
        response = self.client.put(
            STATE_URL,
            {
                "platform": "INVALID_PLATFORM",
                "chat_id": "chat-bad",
                "state": "start",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierPriceUploadNormalizeSyncWithCleanupTests(APITestCase):
    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def setUp(self):
        self.authenticate()
        self.supplier = _make_supplier(invite_code="CODE-NORM")
        self.upload = _make_upload(self.supplier)

    def _norm_result(self):
        return {
            "upload_id": str(self.upload.id),
            "supplier_id": str(self.supplier.id),
            "supplier_name": self.supplier.display_name,
            "status": UploadStatus.APPLIED,
            "items_total": 1,
            "items_created": 1,
            "products_created": 1,
            "products_updated": 0,
            "prices_created": 1,
            "items_matched": 0,
            "needs_review": 0,
            "normalization": {},
        }

    def test_sync_normalize_with_ai_cleanup_triggers_cleanup(self):
        url = _upload_normalize_url(self.upload.id)

        with patch(
            "api.v1.suppliers.views.price_uploads.normalize_supplier_price_upload",
            return_value=self._norm_result(),
        ):
            with patch(
                "api.v1.suppliers.views.price_uploads.start_supplier_price_upload_ai_cleanup",
                return_value={"accepted": True, "ai_cleanup": {"status": "queued"}},
            ) as mock_cleanup:
                response = self.client.post(
                    url,
                    {
                        "run_async": False,
                        "run_ai_cleanup": True,
                        "ai_cleanup_scope": "all",
                    },
                    format="json",
                )

        self.assertEqual(response.status_code, 200)
        mock_cleanup.assert_called_once()
        payload = response.json()
        self.assertTrue(payload.get("ai_cleanup_accepted"))

    def test_sync_normalize_cleanup_error_captured(self):
        url = _upload_normalize_url(self.upload.id)

        with patch(
            "api.v1.suppliers.views.price_uploads.normalize_supplier_price_upload",
            return_value=self._norm_result(),
        ):
            with patch(
                "api.v1.suppliers.views.price_uploads.start_supplier_price_upload_ai_cleanup",
                side_effect=Exception("cleanup queue down"),
            ):
                response = self.client.post(
                    url,
                    {
                        "run_async": False,
                        "run_ai_cleanup": True,
                    },
                    format="json",
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload.get("ai_cleanup_accepted"))
        self.assertIn("cleanup queue down", payload.get("ai_cleanup_enqueue_error", ""))

    def test_sync_normalize_no_cleanup_when_status_not_applied(self):
        url = _upload_normalize_url(self.upload.id)
        norm_result = {**self._norm_result(), "status": UploadStatus.FAILED}

        with patch(
            "api.v1.suppliers.views.price_uploads.normalize_supplier_price_upload",
            return_value=norm_result,
        ):
            with patch(
                "api.v1.suppliers.views.price_uploads.start_supplier_price_upload_ai_cleanup",
            ) as mock_cleanup:
                response = self.client.post(
                    url,
                    {
                        "run_async": False,
                        "run_ai_cleanup": True,
                    },
                    format="json",
                )

        self.assertEqual(response.status_code, 200)
        mock_cleanup.assert_not_called()


@override_settings(SERVICE_API_TOKEN="test-token")
class SupplierPriceUploadPreviewExcelViewTests(APITestCase):
    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def setUp(self):
        self.authenticate()
        self.supplier = _make_supplier(invite_code="CODE-XLS")
        self.upload = _make_upload(self.supplier)
        SupplierPriceUploadItem.objects.create(
            upload=self.upload,
            row_number=1,
            raw_name="Болт М8",
            normalized_name="болт м8",
            parsed_price=None,
            status=UploadItemStatus.NEW,
            raw_payload={"package_info": "10 шт", "supplier_sku": "SKU-001"},
        )

    def test_get_returns_excel_file(self):
        url = _upload_preview_url(self.upload.id)

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_get_excel_content_is_valid_xlsx(self):
        from openpyxl import load_workbook

        url = _upload_preview_url(self.upload.id)
        response = self.client.get(url)

        content = b"".join(response.streaming_content)
        wb = load_workbook(BytesIO(content))
        ws = wb.active
        headers = [ws.cell(row=1, column=col).value for col in range(1, 10)]
        self.assertIn("Наименование", headers)

    def test_get_returns_attachment_disposition(self):
        url = _upload_preview_url(self.upload.id)
        response = self.client.get(url)

        self.assertIn("attachment", response.get("Content-Disposition", ""))

    def test_get_404_for_nonexistent_upload(self):
        from uuid import uuid4

        url = _upload_preview_url(uuid4())
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


@override_settings(SERVICE_API_TOKEN="test-token", CATALOG_QDRANT_AUTO_SYNC_ENABLED=False)
class SupplierPriceUploadCabinetWorkflowTests(APITestCase):
    def setUp(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")
        self.supplier = _make_supplier(invite_code="CODE-WORKFLOW")

    def create_upload(self, *, status=UploadStatus.RECEIVED, rows=None):
        rows = rows or [
            {
                "row_number": 2,
                "cells": {"Наименование": "Молоко", "Цена": "120", "Ед": "шт"},
            }
        ]
        return SupplierPriceUpload.objects.create(
            supplier=self.supplier,
            source_channel=SourceChannel.WEB,
            source_file_name="price.xlsx",
            status=status,
            raw_payload={
                "kind": "file",
                "extracted_rows": rows,
                "extraction": {"rows_total": len(rows), "extractor": "xlsx", "deferred": False},
            },
        )

    def test_json_preview_reports_required_fields_and_first_rows(self):
        upload = self.create_upload()

        response = self.client.get(f"/api/v1/suppliers/uploads/{upload.id}/preview")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["rows_total"], 1)
        self.assertTrue(payload["ready_for_processing"])
        self.assertEqual(payload["required_fields"], {"name": True, "price": True, "unit": True})
        self.assertEqual(payload["rows"][0]["name"], "Молоко")

    def test_json_preview_identifies_missing_required_unit(self):
        upload = self.create_upload(rows=[{"row_number": 2, "cells": {"Наименование": "Молоко", "Цена": "120"}}])

        response = self.client.get(f"/api/v1/suppliers/uploads/{upload.id}/preview")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["required_fields"]["unit"])
        self.assertIn("unit", response.json()["missing_required_fields"])

    def test_upload_history_comes_from_backend_and_is_supplier_filtered(self):
        older = self.create_upload(status=UploadStatus.APPLIED)
        newer = self.create_upload(status=UploadStatus.NEEDS_REVIEW)
        other_supplier = _make_supplier(invite_code="CODE-OTHER", display_name="Other", legal_name="Other LLC")
        SupplierPriceUpload.objects.create(
            supplier=other_supplier,
            source_channel=SourceChannel.WEB,
            status=UploadStatus.APPLIED,
        )

        response = self.client.get("/api/v1/suppliers/uploads", {"supplier_id": str(self.supplier.id)})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 2)
        self.assertEqual(
            [item["id"] for item in response.json()["results"]],
            [str(newer.id), str(older.id)],
        )

    def test_problem_row_can_be_corrected_and_reprocessed(self):
        unit, _ = Unit.objects.get_or_create(code="шт", defaults={"name": "Штука", "kind": UnitKind.PIECE})
        upload = self.create_upload(
            status=UploadStatus.NEEDS_REVIEW,
            rows=[{"row_number": 2, "cells": {"Наименование": "Молоко"}}],
        )
        item = SupplierPriceUploadItem.objects.create(
            upload=upload,
            row_number=2,
            raw_name="Молоко",
            normalized_name="молоко",
            status=UploadItemStatus.NEEDS_REVIEW,
            raw_payload={"warnings": ["price_unknown", "unit_unknown"]},
        )

        correction_response = self.client.patch(
            f"/api/v1/suppliers/uploads/{upload.id}/items/{item.id}",
            {"name": "Молоко 3,2%", "price": "125.50", "unit_id": str(unit.id)},
            format="json",
        )

        self.assertEqual(correction_response.status_code, 200)
        self.assertTrue(correction_response.json()["reprocess_required"])
        upload.refresh_from_db()
        self.assertEqual(upload.status, UploadStatus.RECEIVED)
        corrected_cells = upload.raw_payload["extracted_rows"][0]["cells"]
        self.assertEqual(corrected_cells["name"], "Молоко 3,2%")
        self.assertEqual(corrected_cells["price"], "125.50")
        self.assertEqual(corrected_cells["unit"], "шт")

        normalize_response = self.client.post(
            _upload_normalize_url(upload.id),
            {"use_ai": False, "run_async": False},
            format="json",
        )

        self.assertEqual(normalize_response.status_code, 200)
        self.assertEqual(normalize_response.json()["status"], UploadStatus.APPLIED)
        self.assertEqual(SupplierProduct.objects.get().supplier_name, "Молоко 3,2%")

    def test_error_report_contains_only_problem_rows(self):
        upload = self.create_upload(status=UploadStatus.NEEDS_REVIEW)
        SupplierPriceUploadItem.objects.create(
            upload=upload,
            row_number=2,
            raw_name="Проблемный товар",
            status=UploadItemStatus.NEEDS_REVIEW,
            review_comment="Нет цены",
            raw_payload={"warnings": ["price_unknown"]},
        )
        SupplierPriceUploadItem.objects.create(
            upload=upload,
            row_number=3,
            raw_name="Готовый товар",
            status=UploadItemStatus.NEW,
        )

        response = self.client.get(f"/api/v1/suppliers/uploads/{upload.id}/errors.xlsx")

        self.assertEqual(response.status_code, 200)
        from openpyxl import load_workbook

        workbook = load_workbook(BytesIO(b"".join(response.streaming_content)))
        worksheet = workbook.active
        self.assertEqual(worksheet.max_row, 2)
        self.assertEqual(worksheet.cell(row=2, column=2).value, "Проблемный товар")

    def test_manual_column_mapping_then_change_plan_and_publish(self):
        Unit.objects.get_or_create(code="шт", defaults={"name": "Штука", "kind": UnitKind.PIECE})
        upload = self.create_upload(
            rows=[
                {"row_number": 2, "cells": {"Номенкл": "Сок яблочный", "Розница": "135", "Мера": "шт"}},
            ]
        )

        mapping_response = self.client.patch(
            f"/api/v1/suppliers/uploads/{upload.id}/column-mapping",
            {"mapping": {"name": "Номенкл", "price": "Розница", "unit": "Мера"}},
            format="json",
        )
        self.assertEqual(mapping_response.status_code, 200)
        self.assertEqual(mapping_response.json()["column_mapping"]["name"], "Номенкл")

        preview_response = self.client.get(f"/api/v1/suppliers/uploads/{upload.id}/preview")
        self.assertTrue(preview_response.json()["ready_for_processing"])
        self.assertEqual(preview_response.json()["rows"][0]["name"], "Сок яблочный")

        plan_response = self.client.get(f"/api/v1/suppliers/uploads/{upload.id}/change-plan")
        self.assertEqual(plan_response.status_code, 200)
        self.assertEqual(plan_response.json()["create"], 1)
        self.assertTrue(plan_response.json()["can_publish"])

        publish_response = self.client.post(
            f"/api/v1/suppliers/uploads/{upload.id}/publish",
            {"use_ai": False, "run_async": False},
            format="json",
        )
        self.assertEqual(publish_response.status_code, 200)
        self.assertEqual(publish_response.json()["status"], UploadStatus.APPLIED)
        self.assertTrue(SupplierProduct.objects.filter(supplier_name="Сок яблочный").exists())

    def test_change_plan_detects_duplicate_and_invalid_rows(self):
        upload = self.create_upload(
            rows=[
                {"row_number": 2, "cells": {"Наименование": "Сахар", "Цена": "100", "Ед": "шт"}},
                {"row_number": 3, "cells": {"Наименование": "Сахар", "Цена": "100", "Ед": "шт"}},
                {"row_number": 4, "cells": {"Наименование": "Мука", "Ед": "кг"}},
            ]
        )

        response = self.client.get(f"/api/v1/suppliers/uploads/{upload.id}/change-plan")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["create"], 1)
        self.assertEqual(response.json()["duplicate"], 1)
        self.assertEqual(response.json()["reject"], 1)
        self.assertFalse(response.json()["can_publish"])

    def test_column_mapping_rejects_unknown_source_column(self):
        upload = self.create_upload()

        response = self.client.patch(
            f"/api/v1/suppliers/uploads/{upload.id}/column-mapping",
            {"mapping": {"name": "Не существует"}},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("mapping", response.json())

    def test_column_mapping_cannot_erase_applied_upload_audit_items(self):
        upload = self.create_upload(status=UploadStatus.APPLIED)
        item = SupplierPriceUploadItem.objects.create(
            upload=upload,
            row_number=2,
            raw_name="Молоко",
            status=UploadItemStatus.NEW,
        )

        response = self.client.patch(
            f"/api/v1/suppliers/uploads/{upload.id}/column-mapping",
            {"mapping": {"name": "Наименование"}},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(SupplierPriceUploadItem.objects.filter(id=item.id).exists())

    def test_original_source_file_can_be_downloaded(self):
        source_path = default_storage.save(
            f"test-supplier-price-uploads/{self.supplier.id}/price.csv",
            ContentFile(b"name,price,unit\nSugar,100,pcs\n"),
        )
        self.addCleanup(lambda: default_storage.delete(source_path))
        upload = self.create_upload()
        upload.source_file_uri = source_path
        upload.source_file_name = "price.csv"
        upload.save(update_fields=["source_file_uri", "source_file_name"])

        response = self.client.get(f"/api/v1/suppliers/uploads/{upload.id}/source-file")

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.get("Content-Disposition", ""))
        self.assertEqual(b"".join(response.streaming_content), b"name,price,unit\nSugar,100,pcs\n")


@override_settings(SERVICE_API_TOKEN="test-token")
class CabinetTokenViewTests(APITestCase):
    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

    def setUp(self):
        self.authenticate()
        self.supplier = _make_supplier(invite_code="CODE-CAB")
        self.binding = _make_binding(self.supplier, chat_id="chat-cab")

    def test_post_returns_token(self):
        response = self.client.post(
            CABINET_TOKEN_URL,
            {
                "platform": SourceChannel.TELEGRAM,
                "chat_id": "chat-cab",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("token", payload)
        self.assertTrue(len(payload["token"]) > 10)

    def test_post_creates_cabinet_session(self):
        response = self.client.post(
            CABINET_TOKEN_URL,
            {
                "platform": SourceChannel.TELEGRAM,
                "chat_id": "chat-cab",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CabinetSession.objects.count(), 1)
        session = CabinetSession.objects.get()
        self.assertEqual(session.token, response.json()["token"])

    def test_post_returns_supplier_info(self):
        response = self.client.post(
            CABINET_TOKEN_URL,
            {
                "platform": SourceChannel.TELEGRAM,
                "chat_id": "chat-cab",
            },
            format="json",
        )

        payload = response.json()
        self.assertEqual(payload["supplier_id"], str(self.supplier.id))
        self.assertEqual(payload["supplier_name"], self.supplier.display_name)

    def test_post_without_chat_id_returns_400(self):
        response = self.client.post(
            CABINET_TOKEN_URL,
            {
                "platform": SourceChannel.TELEGRAM,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_post_with_unknown_chat_id_returns_404(self):
        response = self.client.post(
            CABINET_TOKEN_URL,
            {
                "platform": SourceChannel.TELEGRAM,
                "chat_id": "chat-does-not-exist",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 404)


class SupplierCabinetSessionViewTests(APITestCase):
    """POST /suppliers/cabinet/session — exchanges the bot's one-time token for a JWT pair."""

    def setUp(self):
        self.supplier = _make_supplier(invite_code="CODE-SESSION")
        self.binding = _make_binding(self.supplier, chat_id="chat-session")

    def _make_session(self, token, used=False, expires_delta=timedelta(minutes=30)):
        return CabinetSession.objects.create(
            token=token,
            binding=self.binding,
            used=used,
            expires_at=timezone.now() + expires_delta,
        )

    def test_valid_token_returns_jwt_pair(self):
        self._make_session("tok-valid")

        response = self.client.post(CABINET_SESSION_URL, {"token": "tok-valid"}, format="json")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("access", payload)
        self.assertIn("refresh", payload)
        self.assertEqual(payload["supplier_id"], str(self.supplier.id))
        self.assertEqual(payload["supplier_name"], self.supplier.display_name)

    def test_does_not_require_any_credentials(self):
        self._make_session("tok-public")
        self.client.credentials()  # no Authorization header at all

        response = self.client.post(CABINET_SESSION_URL, {"token": "tok-public"}, format="json")

        self.assertEqual(response.status_code, 200)

    def test_marks_session_as_used(self):
        session = self._make_session("tok-once")

        self.client.post(CABINET_SESSION_URL, {"token": "tok-once"}, format="json")

        session.refresh_from_db()
        self.assertTrue(session.used)

    def test_already_used_token_returns_404(self):
        self._make_session("tok-used", used=True)

        response = self.client.post(CABINET_SESSION_URL, {"token": "tok-used"}, format="json")

        self.assertEqual(response.status_code, 404)

    def test_expired_token_returns_404(self):
        self._make_session("tok-expired", expires_delta=timedelta(minutes=-1))

        response = self.client.post(CABINET_SESSION_URL, {"token": "tok-expired"}, format="json")

        self.assertEqual(response.status_code, 404)

    def test_unknown_token_returns_404(self):
        response = self.client.post(CABINET_SESSION_URL, {"token": "does-not-exist"}, format="json")

        self.assertEqual(response.status_code, 404)

    def test_missing_token_returns_400(self):
        response = self.client.post(CABINET_SESSION_URL, {}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_reusing_same_token_twice_fails_second_time(self):
        self._make_session("tok-reuse")

        first = self.client.post(CABINET_SESSION_URL, {"token": "tok-reuse"}, format="json")
        second = self.client.post(CABINET_SESSION_URL, {"token": "tok-reuse"}, format="json")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 404)


class SupplierCabinetSessionFromLoginViewTests(APITestCase):
    """POST /suppliers/cabinet/session-from-login — bridges a core.User JWT
    (email+password/OAuth login) to a supplier cabinet JWT, for a supplier
    who never went through the bot's one-time cabinet token."""

    def setUp(self):
        self.supplier = _make_supplier(invite_code="CODE-BRIDGE", display_name="Bridge Co")
        self.user = User.objects.create(
            email="bridge@example.com", full_name="Bridge User", role=UserRole.SUPPLIER, status=EntityStatus.ACTIVE
        )

    def test_requires_auth(self):
        response = self.client.post(CABINET_SESSION_FROM_LOGIN_URL)
        self.assertEqual(response.status_code, 401)

    def test_returns_404_without_any_membership(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))

        response = self.client.post(CABINET_SESSION_FROM_LOGIN_URL)

        self.assertEqual(response.status_code, 404)

    def test_returns_cabinet_jwt_for_active_member(self):
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.user, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))

        response = self.client.post(CABINET_SESSION_FROM_LOGIN_URL)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("access", payload)
        self.assertIn("refresh", payload)
        self.assertEqual(payload["supplier_id"], str(self.supplier.id))
        self.assertEqual(payload["supplier_name"], self.supplier.display_name)

    def test_issued_jwt_works_for_cabinet_me(self):
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.user, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))
        tokens = self.client.post(CABINET_SESSION_FROM_LOGIN_URL).json()

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        response = self.client.get(CABINET_ME_URL)

        self.assertEqual(response.status_code, 200)

    def test_ignores_inactive_membership(self):
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.user, role=SupplierUserRole.OWNER, status=EntityStatus.INACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))

        response = self.client.post(CABINET_SESSION_FROM_LOGIN_URL)

        self.assertEqual(response.status_code, 404)

    def test_picks_most_recently_created_company_when_multiple(self):
        older = _make_supplier(invite_code="CODE-OLDER", display_name="Older Co")
        SupplierUser.objects.create(
            supplier=older, user=self.user, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        newer = _make_supplier(invite_code="CODE-NEWER", display_name="Newer Co")
        SupplierUser.objects.create(
            supplier=newer, user=self.user, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))

        response = self.client.post(CABINET_SESSION_FROM_LOGIN_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["supplier_id"], str(newer.id))

    def test_explicit_supplier_id_picks_that_company_over_most_recent(self):
        older = _make_supplier(invite_code="CODE-OLDER-2", display_name="Older Co")
        SupplierUser.objects.create(
            supplier=older, user=self.user, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        newer = _make_supplier(invite_code="CODE-NEWER-2", display_name="Newer Co")
        SupplierUser.objects.create(
            supplier=newer, user=self.user, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))

        response = self.client.post(CABINET_SESSION_FROM_LOGIN_URL, {"supplier_id": str(older.id)}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["supplier_id"], str(older.id))

    def test_explicit_supplier_id_not_belonging_to_user_returns_404(self):
        other_supplier = _make_supplier(invite_code="CODE-OTHER", display_name="Other Co")
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.user, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.user))

        response = self.client.post(
            CABINET_SESSION_FROM_LOGIN_URL, {"supplier_id": str(other_supplier.id)}, format="json"
        )

        self.assertEqual(response.status_code, 404)


class SupplierCabinetViewTests(APITestCase):
    """GET/PATCH /suppliers/cabinet/me — JWT-scoped cabinet snapshot."""

    def setUp(self):
        self.supplier = _make_supplier(invite_code="CODE-ME", display_name="Кабинет ООО")
        self.binding = _make_binding(self.supplier, chat_id="chat-me")
        self.unit = Unit.objects.create(code="pcs-cab", name="штука", kind=UnitKind.PIECE)

    def _issue_tokens(self):
        CabinetSession.objects.create(
            token="tok-me",
            binding=self.binding,
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        response = self.client.post(CABINET_SESSION_URL, {"token": "tok-me"}, format="json")
        return response.json()

    def _authenticate(self):
        tokens = self._issue_tokens()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        return tokens

    def test_get_without_credentials_returns_401(self):
        self.client.credentials()

        response = self.client.get(CABINET_ME_URL)

        self.assertEqual(response.status_code, 401)

    def test_get_with_unrelated_bearer_value_returns_401(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-jwt-at-all")

        response = self.client.get(CABINET_ME_URL)

        self.assertEqual(response.status_code, 401)

    def test_get_returns_supplier_snapshot(self):
        self._authenticate()
        SupplierProduct.objects.create(
            supplier=self.supplier,
            unit=self.unit,
            supplier_name="Товар А",
            normalized_name="товар а",
            status=EntityStatus.ACTIVE,
        )

        response = self.client.get(CABINET_ME_URL)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["supplier"]["id"], str(self.supplier.id))
        self.assertEqual(payload["products_count"], 1)
        self.assertEqual(payload["uploads"], [])

    def test_get_includes_upload_history(self):
        self._authenticate()
        SupplierPriceUpload.objects.create(
            supplier=self.supplier,
            source_channel=SourceChannel.TELEGRAM,
            status=UploadStatus.APPLIED,
            raw_payload={},
        )

        response = self.client.get(CABINET_ME_URL)

        self.assertEqual(len(response.json()["uploads"]), 1)

    def test_patch_updates_company_fields(self):
        self._authenticate()

        response = self.client.patch(
            CABINET_ME_URL,
            {"description": "Овощи и фрукты оптом", "delivery_terms": "по будням"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.description, "Овощи и фрукты оптом")
        self.assertEqual(self.supplier.delivery_terms, "по будням")

    def test_patch_merges_metadata_instead_of_replacing(self):
        self.supplier.metadata = {"invite_code": "CODE-ME", "existing": "keep"}
        self.supplier.save()
        self._authenticate()

        response = self.client.patch(CABINET_ME_URL, {"metadata": {"new": "value"}}, format="json")

        self.assertEqual(response.status_code, 200)
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.metadata["existing"], "keep")
        self.assertEqual(self.supplier.metadata["new"], "value")

    def test_manager_via_web_login_can_patch_registry_fields_too(self):
        """2026-08-01: изначально было owner-only (ИНН/КПП/ОГРН и т.п. меняют
        юридическую идентичность компании). 2026-08-05: пользователь явно решил,
        что owner и manager имеют одинаковые права — требуется лишь
        идентифицированная активная сессия (веб-логин, не bot), не конкретно owner."""
        manager = User.objects.create(
            email="cabinet-patch-registry-manager@example.com", full_name="Manager", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self._login_as(manager)

        response = self.client.patch(CABINET_ME_URL, {"inn": "7707083893"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.inn, "7707083893")

    def test_owner_via_web_login_can_patch_registry_fields(self):
        owner = User.objects.create(
            email="cabinet-patch-registry-owner@example.com", full_name="Owner", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self._login_as(owner)

        response = self.client.patch(CABINET_ME_URL, {"inn": "7707083893"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.inn, "7707083893")

    def test_bot_session_without_linked_user_cannot_patch_registry_fields(self):
        self._authenticate()

        response = self.client.patch(CABINET_ME_URL, {"kpp": "770701001"}, format="json")

        self.assertEqual(response.status_code, 403)

    def test_manager_can_still_patch_ordinary_fields(self):
        """Ограничение только на owner-only поля — обычные операции кабинета
        (description и т.п.) остаются открыты любому активному участнику."""
        manager = User.objects.create(
            email="cabinet-patch-ordinary-manager@example.com", full_name="Manager", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self._login_as(manager)

        response = self.client.patch(CABINET_ME_URL, {"description": "Овощи оптом"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.description, "Овощи оптом")

    def test_inactive_supplier_jwt_is_rejected(self):
        self._authenticate()
        self.supplier.status = EntityStatus.ARCHIVED
        self.supplier.save()

        response = self.client.get(CABINET_ME_URL)

        self.assertEqual(response.status_code, 401)

    def test_jwt_from_one_supplier_does_not_see_another(self):
        other_supplier = _make_supplier(invite_code="CODE-OTHER", display_name="Другая компания")
        other_binding = _make_binding(other_supplier, chat_id="chat-other")
        CabinetSession.objects.create(
            token="tok-other",
            binding=other_binding,
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        other_tokens = self.client.post(CABINET_SESSION_URL, {"token": "tok-other"}, format="json").json()

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other_tokens['access']}")
        response = self.client.get(CABINET_ME_URL)

        self.assertEqual(response.json()["supplier"]["id"], str(other_supplier.id))
        self.assertNotEqual(response.json()["supplier"]["id"], str(self.supplier.id))

    def _login_as(self, user):
        """Bridges a core.User JWT to a cabinet JWT via session-from-login —
        the only path that stamps a user_id claim on the cabinet token
        (2026-08-01), needed for IsSupplierCabinetActiveMember to identify the
        acting user."""
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))
        tokens = self.client.post(CABINET_SESSION_FROM_LOGIN_URL).json()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def test_delete_via_bot_session_without_linked_user_is_forbidden(self):
        """2026-08-01: аудит "поделиться доступом" нашёл, что удаление вообще
        не проверяло роль — любой manager мог удалить компанию наравне с
        owner. Побочный эффект фикса: cabinet-JWT из бота не знает, какой
        участник за ней стоит (BotUserBinding.user нигде не проставляется),
        поэтому не может подтвердить owner — теперь запрещено по умолчанию
        (fail closed), а не разрешено кому угодно, как было раньше."""
        self._authenticate()

        response = self.client.delete(CABINET_ME_URL)

        self.assertEqual(response.status_code, 403)
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.status, EntityStatus.ACTIVE)

    def test_owner_via_web_login_can_delete(self):
        """2026-07-29: "у поставщика должна быть возможность удалить свою
        компанию" — мягкое удаление, не DELETE строки (SupplierPriceUpload/
        SupplierProduct ссылаются через on_delete=RESTRICT). Теперь только
        через веб-логин (cabinet/session-from-login), owner."""
        owner = User.objects.create(email="cabinet-delete-owner@example.com", full_name="Owner", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self._login_as(owner)

        response = self.client.delete(CABINET_ME_URL)

        self.assertEqual(response.status_code, 204)
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.status, EntityStatus.ARCHIVED)
        self.assertFalse(self.supplier.is_published)

    def test_manager_via_web_login_can_delete_too(self):
        """2026-08-05: owner и manager имеют одинаковые права."""
        manager = User.objects.create(
            email="cabinet-delete-manager@example.com", full_name="Manager", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=manager, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self._login_as(manager)

        response = self.client.delete(CABINET_ME_URL)

        self.assertEqual(response.status_code, 204)
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.status, EntityStatus.ARCHIVED)

    def test_delete_locks_out_the_same_cabinet_token(self):
        """Удаление сразу лишает доступа тем же токеном — это ожидаемо
        (в отличие от is_published=False, которое кабинет не блокирует)."""
        owner = User.objects.create(
            email="cabinet-delete-lockout@example.com", full_name="Owner", role=UserRole.SUPPLIER
        )
        SupplierUser.objects.create(
            supplier=self.supplier, user=owner, role=SupplierUserRole.OWNER, status=EntityStatus.ACTIVE
        )
        self._login_as(owner)

        delete_response = self.client.delete(CABINET_ME_URL)
        self.assertEqual(delete_response.status_code, 204)

        get_response = self.client.get(CABINET_ME_URL)
        self.assertEqual(get_response.status_code, 401)

    def test_delete_without_credentials_returns_401(self):
        self.client.credentials()

        response = self.client.delete(CABINET_ME_URL)

        self.assertEqual(response.status_code, 401)
