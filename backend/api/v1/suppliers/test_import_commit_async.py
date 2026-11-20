from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from api.v1.suppliers.product_import_services import (
    commit_supplier_product_import_session,
    run_supplier_product_import_commit,
)
from api.v1.suppliers.tasks import (
    commit_supplier_product_import_session_task,
    purge_committed_product_import_sessions_task,
)
from core.models import Unit, UnitKind
from file_imports.task_locks import import_task_lock
from file_imports.tasks import recover_stalled_imports_task
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

NOTIFY = "api.v1.suppliers.product_import_services.notify_linked_supplier_chats"


def _session_with_ready_item(*, commit_status=SupplierProductImportCommitStatus.PENDING):
    supplier = Supplier.objects.create(legal_name="Async LLC", display_name="Async LLC")
    unit = Unit.objects.create(code="async-kg", name="async-kg", kind=UnitKind.PIECE)
    session = SupplierProductImportSession.objects.create(supplier=supplier, commit_status=commit_status)
    SupplierProductImportItem.objects.create(
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
    return session


class CommitStatusFieldsTests(TestCase):
    def test_new_session_has_idle_commit_state(self):
        supplier = Supplier.objects.create(legal_name="Async LLC", display_name="Async LLC")

        session = SupplierProductImportSession.objects.create(supplier=supplier)

        self.assertEqual(session.commit_status, "none")
        self.assertIsNone(session.commit_result)
        self.assertIsNone(session.commit_error)


class KeepSessionTests(TestCase):
    def test_keep_session_marks_committed_without_deleting(self):
        session = _session_with_ready_item()

        result = commit_supplier_product_import_session(session=session, keep_session=True)

        self.assertEqual(result["imported_count"], 1)
        self.assertFalse(result["session_deleted"])
        self.assertEqual(result["session_status"], SupplierProductImportSessionStatus.COMMITTED)
        session.refresh_from_db()
        self.assertEqual(session.status, SupplierProductImportSessionStatus.COMMITTED)
        self.assertFalse(session.items.exists())


class RunCommitTests(TestCase):
    def test_success_stores_result_and_notifies_once(self):
        session = _session_with_ready_item()

        with mock.patch(NOTIFY) as notify:
            run_supplier_product_import_commit(session_id=session.pk)

        session.refresh_from_db()
        self.assertEqual(session.commit_status, SupplierProductImportCommitStatus.NONE)
        self.assertIsNone(session.commit_error)
        self.assertEqual(session.commit_result["imported_count"], 1)
        self.assertEqual(session.status, SupplierProductImportSessionStatus.COMMITTED)
        self.assertEqual(len(session.commit_result["draft_supplier_product_ids"]), 1)
        self.assertTrue(SupplierProduct.objects.filter(supplier=session.supplier, supplier_name="Сахар").exists())
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["supplier"], session.supplier)
        self.assertIn("Добавлено или обновлено: 1.", notify.call_args.kwargs["text"])

    def test_no_notification_when_nothing_imported(self):
        session = _session_with_ready_item()
        SupplierProductImportItem.objects.filter(session=session).update(action=SupplierProductImportAction.SKIP)

        with mock.patch(NOTIFY) as notify:
            run_supplier_product_import_commit(session_id=session.pk)

        notify.assert_not_called()

    def test_failure_marks_failed_with_readable_error_and_reraises(self):
        session = _session_with_ready_item()

        with (
            mock.patch(
                "api.v1.suppliers.product_import_services.commit_supplier_product_import_session",
                side_effect=RuntimeError("secret internals: connection to 10.0.0.5 lost"),
            ),
            mock.patch(NOTIFY) as notify,
            self.assertRaises(RuntimeError),
        ):
            run_supplier_product_import_commit(session_id=session.pk)

        session.refresh_from_db()
        self.assertEqual(session.commit_status, SupplierProductImportCommitStatus.FAILED)
        self.assertTrue(session.commit_error)
        self.assertNotIn("10.0.0.5", session.commit_error)
        self.assertIsNone(session.commit_result)
        notify.assert_not_called()

    def test_session_not_awaiting_commit_is_left_alone(self):
        session = _session_with_ready_item(commit_status=SupplierProductImportCommitStatus.NONE)

        with mock.patch(NOTIFY) as notify:
            outcome = run_supplier_product_import_commit(session_id=session.pk)

        self.assertIsNone(outcome)
        session.refresh_from_db()
        self.assertEqual(session.commit_status, SupplierProductImportCommitStatus.NONE)
        self.assertTrue(session.items.exists())
        notify.assert_not_called()

    def test_running_session_is_taken_over_after_worker_loss(self):
        session = _session_with_ready_item(commit_status=SupplierProductImportCommitStatus.RUNNING)

        with mock.patch(NOTIFY):
            run_supplier_product_import_commit(session_id=session.pk)

        session.refresh_from_db()
        self.assertEqual(session.commit_status, SupplierProductImportCommitStatus.NONE)
        self.assertEqual(session.commit_result["imported_count"], 1)

    def test_deleted_session_is_ignored(self):
        with mock.patch(NOTIFY) as notify:
            outcome = run_supplier_product_import_commit(session_id="00000000-0000-0000-0000-000000000000")

        self.assertIsNone(outcome)
        notify.assert_not_called()


class CommitTaskTests(TestCase):
    def test_task_runs_commit_for_supplier_actor(self):
        session = _session_with_ready_item()

        # Задача зовёт close_old_connections() -- в TestCase это закрыло бы соединение теста (так же патчат другие тесты задач).
        with mock.patch(NOTIFY), mock.patch("api.v1.suppliers.tasks.close_old_connections"):
            outcome = commit_supplier_product_import_session_task.apply(
                kwargs={"session_id": str(session.pk), "actor": {"type": "supplier", "id": str(session.supplier_id)}}
            ).get()

        session.refresh_from_db()
        self.assertEqual(outcome["status"], SupplierProductImportCommitStatus.NONE)
        self.assertEqual(session.commit_result["imported_count"], 1)


@override_settings(IMPORT_RECOVERY_ENABLED=True)
class StalledCommitRecoveryTests(TestCase):
    def _session(self, commit_status, *, age):
        supplier = Supplier.objects.create(legal_name="Stale LLC", display_name="Stale LLC")
        session = SupplierProductImportSession.objects.create(supplier=supplier, commit_status=commit_status)
        SupplierProductImportSession.objects.filter(pk=session.pk).update(updated_at=timezone.now() - age)
        return session

    def test_stale_running_and_pending_commits_are_failed_not_retried(self):
        running = self._session(SupplierProductImportCommitStatus.RUNNING, age=timedelta(hours=3))
        pending = self._session(SupplierProductImportCommitStatus.PENDING, age=timedelta(hours=3))

        with mock.patch("api.v1.suppliers.tasks.commit_supplier_product_import_session_task.delay") as enqueue:
            outcome = recover_stalled_imports_task()

        self.assertEqual(outcome["failed"], 2)
        for session in (running, pending):
            session.refresh_from_db()
            self.assertEqual(session.commit_status, SupplierProductImportCommitStatus.FAILED)
            self.assertEqual(session.commit_error, "Применение не завершилось. Повторите попытку.")
        enqueue.assert_not_called()

    def test_fresh_commit_is_left_alone(self):
        session = self._session(SupplierProductImportCommitStatus.RUNNING, age=timedelta(minutes=5))

        self.assertEqual(recover_stalled_imports_task()["failed"], 0)

        session.refresh_from_db()
        self.assertEqual(session.commit_status, SupplierProductImportCommitStatus.RUNNING)

    def test_commit_held_by_live_worker_is_left_alone(self):
        session = self._session(SupplierProductImportCommitStatus.RUNNING, age=timedelta(hours=3))

        with import_task_lock("supplier_commit", session.id) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(recover_stalled_imports_task()["failed"], 0)

        session.refresh_from_db()
        self.assertEqual(session.commit_status, SupplierProductImportCommitStatus.RUNNING)

    @override_settings(IMPORT_RECOVERY_ENABLED=False)
    def test_recovery_disabled_leaves_stale_commit(self):
        session = self._session(SupplierProductImportCommitStatus.RUNNING, age=timedelta(hours=3))

        recover_stalled_imports_task()

        session.refresh_from_db()
        self.assertEqual(session.commit_status, SupplierProductImportCommitStatus.RUNNING)


@override_settings(SUPPLIER_PRODUCT_IMPORT_COMMITTED_TTL_HOURS=24)
class PurgeCommittedSessionsTests(TestCase):
    def _session(self, *, status, commit_result, age):
        supplier = Supplier.objects.create(legal_name="Purge LLC", display_name="Purge LLC")
        session = SupplierProductImportSession.objects.create(
            supplier=supplier, status=status, commit_result=commit_result
        )
        SupplierProductImportSession.objects.filter(pk=session.pk).update(updated_at=timezone.now() - age)
        return session

    def test_only_old_background_committed_sessions_are_deleted(self):
        committed = SupplierProductImportSessionStatus.COMMITTED
        old = self._session(status=committed, commit_result={"imported_count": 1}, age=timedelta(hours=25))
        fresh = self._session(status=committed, commit_result={"imported_count": 1}, age=timedelta(hours=1))
        legacy = self._session(status=committed, commit_result=None, age=timedelta(days=90))
        still_open = self._session(
            status=SupplierProductImportSessionStatus.OPEN, commit_result={"imported_count": 1}, age=timedelta(days=3)
        )

        outcome = purge_committed_product_import_sessions_task()

        self.assertEqual(outcome, {"purged": 1, "purged_failed": 0})
        self.assertFalse(SupplierProductImportSession.objects.filter(pk=old.pk).exists())
        for kept in (fresh, legacy, still_open):
            self.assertTrue(SupplierProductImportSession.objects.filter(pk=kept.pk).exists())
