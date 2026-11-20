from decimal import Decimal
from unittest import mock

from django.test import override_settings
from rest_framework.test import APITestCase

from api.v1.suppliers.services import issue_supplier_cabinet_tokens
from core.models import Unit, UnitKind
from suppliers.models import (
    ProductAvailability,
    Supplier,
    SupplierProduct,
    SupplierProductImportAction,
    SupplierProductImportCommitStatus,
    SupplierProductImportItem,
    SupplierProductImportSession,
    SupplierProductImportSessionStatus,
)

BASE = "/api/v1/suppliers/products/import"
DELAY = "api.v1.suppliers.tasks.commit_supplier_product_import_session_task.delay"
NOTIFY_VIEW = "api.v1.suppliers.views.product_import.notify_linked_supplier_chats"
NOTIFY_ASYNC = "api.v1.suppliers.product_import_services.notify_linked_supplier_chats"
CLOSE = "api.v1.suppliers.tasks.close_old_connections"


def _setup(client, *, commit_status=SupplierProductImportCommitStatus.NONE):
    supplier = Supplier.objects.create(legal_name="Api LLC", display_name="Api LLC")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_supplier_cabinet_tokens(supplier)['access']}")
    unit, _ = Unit.objects.get_or_create(code="api-kg", defaults={"name": "api-kg", "kind": UnitKind.PIECE})
    session = SupplierProductImportSession.objects.create(supplier=supplier, commit_status=commit_status)
    item = SupplierProductImportItem.objects.create(
        session=session,
        row_number=1,
        raw_name="Сахар",
        name="Сахар",
        unit=unit,
        country_of_origin="Россия",
        price=Decimal("10"),
        delivery_multiple=Decimal("1"),
        net_value=Decimal("1"),
        net_unit=unit,
        min_order_qty=Decimal("1"),
        availability=ProductAvailability.IN_STOCK,
        action=SupplierProductImportAction.NEW,
    )
    return supplier, session, item


@override_settings(SUPPLIER_PRODUCT_IMPORT_COMMIT_ASYNC=True)
class AsyncCommitEndpointTests(APITestCase):
    def test_returns_202_and_queues_task_with_actor(self):
        supplier, session, _ = _setup(self.client)

        with mock.patch(DELAY) as delay:
            response = self.client.post(f"{BASE}/{session.id}/commit")

        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(
            response.json(),
            {"commit_status": "pending", "status_url": f"/api/v1/suppliers/products/import/{session.id}"},
        )
        session.refresh_from_db()
        self.assertEqual(session.commit_status, SupplierProductImportCommitStatus.PENDING)
        delay.assert_called_once_with(session_id=str(session.id), actor={"type": "supplier", "id": str(supplier.pk)})
        self.assertTrue(session.items.exists(), "строки не трогаются до работы воркера")

    def test_repeated_post_while_pending_or_running_is_409(self):
        for state in (SupplierProductImportCommitStatus.PENDING, SupplierProductImportCommitStatus.RUNNING):
            _, session, _ = _setup(self.client, commit_status=state)

            with mock.patch(DELAY) as delay:
                response = self.client.post(f"{BASE}/{session.id}/commit")

            self.assertEqual(response.status_code, 409, (state, response.content))
            self.assertEqual(response.json(), {"detail": "Применение уже выполняется."})
            delay.assert_not_called()

    def test_already_committed_session_is_409(self):
        _, session, _ = _setup(self.client)
        SupplierProductImportSession.objects.filter(pk=session.pk).update(
            status=SupplierProductImportSessionStatus.COMMITTED
        )

        response = self.client.post(f"{BASE}/{session.id}/commit")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "Сессия уже импортирована."})

    def test_retry_after_failed_is_allowed(self):
        _, session, _ = _setup(self.client, commit_status=SupplierProductImportCommitStatus.FAILED)
        SupplierProductImportSession.objects.filter(pk=session.pk).update(commit_error="прошлая ошибка")

        with mock.patch(DELAY):
            response = self.client.post(f"{BASE}/{session.id}/commit")

        self.assertEqual(response.status_code, 202)
        session.refresh_from_db()
        self.assertEqual(session.commit_status, SupplierProductImportCommitStatus.PENDING)
        self.assertIsNone(session.commit_error)

    def test_session_still_processing_keeps_old_409(self):
        _, session, _ = _setup(self.client)
        SupplierProductImportSession.objects.filter(pk=session.pk).update(processing_status="processing")

        response = self.client.post(f"{BASE}/{session.id}/commit")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "Импорт ещё обрабатывается."})

    def test_broker_unavailable_marks_failed_and_returns_503(self):
        _, session, _ = _setup(self.client)

        with mock.patch(DELAY, side_effect=ConnectionError("broker down")):
            response = self.client.post(f"{BASE}/{session.id}/commit")

        self.assertEqual(response.status_code, 503, response.content)
        session.refresh_from_db()
        self.assertEqual(session.commit_status, SupplierProductImportCommitStatus.FAILED)
        self.assertEqual(session.commit_error, "Не удалось поставить применение в очередь.")

    def test_edits_are_rejected_while_commit_in_progress(self):
        for state in (SupplierProductImportCommitStatus.PENDING, SupplierProductImportCommitStatus.RUNNING):
            _, session, item = _setup(self.client, commit_status=state)
            base = f"{BASE}/{session.id}"
            calls = [
                self.client.patch(f"{base}/items/{item.id}", {"price": "10"}, format="json"),
                self.client.put(f"{base}/items/{item.id}/action", {"action": "skip"}, format="json"),
                self.client.patch(
                    f"{base}/items/bulk", {"item_ids": [str(item.id)], "availability": "in_stock"}, format="json"
                ),
            ]
            for call in calls:
                self.assertEqual(call.status_code, 409, (state, call.content))
                self.assertEqual(call.json(), {"detail": "Идёт применение импорта."})
            item.refresh_from_db()
            self.assertEqual(item.action, SupplierProductImportAction.NEW)

    def test_edits_allowed_after_failed_commit(self):
        _, session, item = _setup(self.client, commit_status=SupplierProductImportCommitStatus.FAILED)

        response = self.client.put(f"{BASE}/{session.id}/items/{item.id}/action", {"action": "skip"}, format="json")

        self.assertEqual(response.status_code, 200, response.content)

    def test_end_to_end_eager_run_stores_result_and_notifies_once(self):
        supplier, session, _ = _setup(self.client)

        with (
            override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True),
            mock.patch(CLOSE),
            mock.patch(NOTIFY_ASYNC) as notify,
            mock.patch(NOTIFY_VIEW) as sync_notify,
        ):
            response = self.client.post(f"{BASE}/{session.id}/commit")

        self.assertEqual(response.status_code, 202, response.content)
        session.refresh_from_db()
        self.assertEqual(session.commit_status, SupplierProductImportCommitStatus.NONE)
        self.assertEqual(session.commit_result["imported_count"], 1)
        self.assertTrue(SupplierProduct.objects.filter(supplier=supplier, supplier_name="Сахар").exists())
        notify.assert_called_once()
        sync_notify.assert_not_called()


@override_settings(SUPPLIER_PRODUCT_IMPORT_COMMIT_ASYNC=False)
class SyncCommitUnchangedWhenFlagOffTests(APITestCase):
    def test_flag_off_keeps_synchronous_200_and_deletes_session(self):
        supplier, session, _ = _setup(self.client)

        with mock.patch(DELAY) as delay, mock.patch(NOTIFY_VIEW) as notify:
            response = self.client.post(f"{BASE}/{session.id}/commit")

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["imported_count"], 1)
        self.assertTrue(body["session_deleted"])
        self.assertFalse(SupplierProductImportSession.objects.filter(pk=session.pk).exists())
        delay.assert_not_called()
        notify.assert_called_once()
        self.assertIn("Добавлено или обновлено: 1.", notify.call_args.kwargs["text"])


class CommitStateInSessionResponseTests(APITestCase):
    def test_get_session_without_items_exposes_idle_commit_state(self):
        _, session, _ = _setup(self.client)

        body = self.client.get(f"{BASE}/{session.id}", {"include_items": "false"}).json()

        self.assertEqual(body["commit_status"], "none")
        self.assertIsNone(body["commit_error"])
        self.assertIsNone(body["commit_result"])

    def test_get_session_shows_failed_state_with_error(self):
        _, session, _ = _setup(self.client, commit_status=SupplierProductImportCommitStatus.FAILED)
        SupplierProductImportSession.objects.filter(pk=session.pk).update(commit_error="Не удалось применить импорт.")

        body = self.client.get(f"{BASE}/{session.id}", {"include_items": "false"}).json()

        self.assertEqual(body["commit_status"], "failed")
        self.assertEqual(body["commit_error"], "Не удалось применить импорт.")

    @override_settings(SUPPLIER_PRODUCT_IMPORT_COMMIT_ASYNC=True)
    def test_get_after_background_commit_returns_result_not_404(self):
        _, session, _ = _setup(self.client)
        with (
            override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True),
            mock.patch(CLOSE),
            mock.patch(NOTIFY_ASYNC),
        ):
            self.assertEqual(self.client.post(f"{BASE}/{session.id}/commit").status_code, 202)

        response = self.client.get(f"{BASE}/{session.id}", {"include_items": "false"})

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["status"], "committed")
        self.assertEqual(body["commit_status"], "none")
        result = body["commit_result"]
        self.assertEqual(result["imported_count"], 1)
        self.assertFalse(result["session_deleted"])
        self.assertEqual(result["session_status"], "committed")
        self.assertEqual(len(result["draft_supplier_product_ids"]), 1)
        self.assertEqual(result["errors"], [])

    def test_list_items_expose_commit_status(self):
        _, session, _ = _setup(self.client, commit_status=SupplierProductImportCommitStatus.RUNNING)

        body = self.client.get(BASE).json()

        self.assertEqual([row["commit_status"] for row in body["results"]], ["running"])


class KeptCommittedSessionVisibilityTests(APITestCase):
    def _kept_committed_session(self):
        supplier, session, _ = _setup(self.client)
        SupplierProductImportSession.objects.filter(pk=session.pk).update(
            status=SupplierProductImportSessionStatus.COMMITTED,
            commit_result={"imported_count": 1},
            content_sha256="a" * 64,
        )
        session.items.all().delete()
        return supplier, session

    def test_same_file_is_not_reused_from_kept_committed_session(self):
        from api.v1.suppliers.product_import_selectors import find_reusable_supplier_product_import_session

        supplier, _ = self._kept_committed_session()

        self.assertIsNone(find_reusable_supplier_product_import_session(supplier=supplier, content_sha256="a" * 64))

    def test_default_list_hides_kept_committed_session_but_committed_filter_shows_it(self):
        _, session = self._kept_committed_session()

        default_ids = [row["id"] for row in self.client.get(BASE).json()["results"]]
        committed_ids = [row["id"] for row in self.client.get(BASE, {"status": "committed"}).json()["results"]]

        self.assertNotIn(str(session.id), default_ids)
        self.assertEqual(committed_ids, [str(session.id)])
