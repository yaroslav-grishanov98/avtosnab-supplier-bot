"""Regression coverage for real session writes, response rendering and partial retries."""

from unittest.mock import patch

from django.test import override_settings
from rest_framework.test import APITestCase

from api.v1.catalog import tests as fixtures
from catalog.models import CatalogImportItem, MasterProduct, VenueProductMatrixItem
from core.models import EntityStatus, Venue, VenueUser, VenueUserRole


@override_settings(CATALOG_IMPORT_PARTIAL_COMMIT_ENABLED=True)
class CatalogImportPipelineRegressionTests(APITestCase):
    setUp = fixtures.CatalogImportSessionAPITests.setUp
    auth = fixtures.CatalogImportSessionAPITests.auth
    _master = fixtures.CatalogImportSessionAPITests._master

    def test_real_master_commit_renders_and_accepts_uuid_unit(self):
        self.item.normalized_values["unit_id"] = str(self.unit.id)
        self.item.save()
        self.auth(self.owner)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        product = MasterProduct.objects.get(owner=self.owner)
        self.assertEqual(product.unit_id, self.unit.id)
        self.assertEqual(response.json()["rows"][0]["master_product_id"], str(product.id))
        self.assertEqual(response.json()["remaining_count"], 0)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 400)
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner).count(), 1)

    def test_partial_commit_preserves_review_and_does_not_reapply_good_row(self):
        pending = CatalogImportItem.objects.create(
            session=self.session,
            row_number=3,
            raw_values={"raw_name": "Соль"},
            normalized_values={"raw_name": "Соль"},
            action="review",
        )
        self.auth(self.owner)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["status"], response.json()["remaining_count"]), ("open", 1))
        self.assertEqual(self.client.patch(self.item_url, {"action": "skip"}, format="json").status_code, 409)
        repeat = self.client.post(self.url, {}, format="json")
        self.assertEqual(repeat.status_code, 409, repeat.content)
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner).count(), 1)
        self.client.patch(f"{self.url}/items/{pending.id}", {"action": "create_new"}, format="json")
        final = self.client.post(self.url, {}, format="json")
        self.assertEqual(final.status_code, 200, final.content)
        self.assertEqual(final.json()["status"], "committed")
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner).count(), 2)

    def test_conflicted_target_does_not_close_session_or_block_good_row(self):
        target = self._master()
        conflict = CatalogImportItem.objects.create(
            session=self.session,
            row_number=3,
            raw_values={"raw_name": "Старая версия"},
            normalized_values={"raw_name": "Старая версия"},
            action="update_existing",
            target_id=target.id,
            target_expected_updated_at=target.updated_at,
        )
        target.note = "Другой пользователь"
        target.save()
        self.auth(self.owner)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["conflicted_item_ids"], [str(conflict.id)])
        self.assertEqual(response.json()["status"], "open")
        conflict.refresh_from_db()
        self.assertEqual(conflict.action, "review")

    def test_patch_normalized_values_are_applied(self):
        self.auth(self.owner)
        response = self.client.patch(
            self.item_url, {"normalized_values": {"raw_name": "Исправлено"}, "action": "create_new"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(MasterProduct.objects.get(owner=self.owner).canonical_name, "Исправлено")

    def test_patch_rejects_arrays_and_contradictory_values(self):
        self.auth(self.owner)
        for data in ({"raw_values": []}, {"raw_values": {"raw_name": "А"}, "normalized_values": {"raw_name": "Б"}}):
            self.assertEqual(self.client.patch(self.item_url, data, format="json").status_code, 400)

    def test_unrelated_patch_does_not_accept_new_target_version(self):
        target = self._master()
        self.item.target_id = target.id
        self.item.target_expected_updated_at = target.updated_at
        self.item.action = "update_existing"
        self.item.save()
        expected = target.updated_at
        target.note = "Изменено"
        target.save()
        self.auth(self.owner)
        self.client.patch(self.item_url, {"normalized_values": {"note": "Примечание"}}, format="json")
        self.item.refresh_from_db()
        self.assertEqual(self.item.target_expected_updated_at, expected)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 409)

    def test_replace_preserves_card_id_and_changes_name_unit(self):
        target = self._master()
        self.auth(self.owner)
        response = self.client.patch(
            self.item_url,
            {
                "action": "replace",
                "target_id": str(target.id),
                "normalized_values": {"raw_name": "Новое", "unit_id": str(self.unit.id)},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        target.refresh_from_db()
        self.assertEqual(target.canonical_name, "Новое")
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner).count(), 1)

    def test_manager_can_continue_owner_session_and_loses_access_after_revoke(self):
        venue = Venue.objects.create(display_name="Компания")
        VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER)
        membership = VenueUser.objects.create(venue=venue, user=self.stranger, role=VenueUserRole.MANAGER)
        self.auth(self.stranger)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        membership.status = EntityStatus.ARCHIVED
        membership.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.patch(self.item_url, {"action": "skip"}, format="json").status_code, 404)

    def test_venue_commit_renders_real_result_and_preserves_missing_flags(self):
        venue = Venue.objects.create(display_name="Компания")
        VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER)
        self.session.domain, self.session.venue = "venue", venue
        self.session.save()
        self.item.normalized_values = {"raw_name": "Мука", "requested_unit_label": "кг"}
        self.item.save()
        self.auth(self.owner)
        # The external embedding model is replaced; DB/apply/renderer are real.
        with patch("api.v1.catalog.services.master_core.find_candidate_master_products", return_value=[]):
            response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        created = VenueProductMatrixItem.objects.get(venue=venue)
        self.assertFalse(created.missing_from_latest_import)
        self.assertEqual(response.json()["rows"][0]["matrix_item_id"], str(created.id))
        self.client.post(self.url, {}, format="json")
        self.assertEqual(VenueProductMatrixItem.objects.filter(venue=venue).count(), 1)

    def test_duplicate_master_names_within_session_create_one_card(self):
        CatalogImportItem.objects.create(
            session=self.session,
            row_number=3,
            raw_values={"raw_name": "Мука"},
            normalized_values={"raw_name": "Мука"},
            action="create_new",
        )
        self.auth(self.owner)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner).count(), 1)

    def test_large_master_batch_is_written_with_bounded_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        CatalogImportItem.objects.bulk_create(
            [
                CatalogImportItem(
                    session=self.session,
                    row_number=index + 3,
                    raw_values={"raw_name": f"Товар {index}"},
                    normalized_values={"raw_name": f"Товар {index}"},
                    action="create_new",
                )
                for index in range(1000)
            ]
        )
        self.auth(self.owner)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(MasterProduct.objects.filter(owner=self.owner).count(), 1001)
        self.assertLess(len(queries), 60)

    def test_status_only_does_not_serialize_items(self):
        self.auth(self.owner)
        with patch(
            "api.v1.catalog.serializers.import_sessions.CatalogImportSessionItemSerializer.to_representation",
            side_effect=AssertionError("Expensive items serialized"),
        ):
            response = self.client.get(self.url + "?include_items=false")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["items"], [])


@override_settings(
    IMPORT_FILE_STORAGE_ENABLED=True, VENUE_IMPORT_ASYNC_ENABLED=True, CATALOG_IMPORT_ASYNC_EXTRACTION_ENABLED=True
)
class CatalogDocumentAsyncRegressionTests(fixtures.TemporaryMediaRootMixin, APITestCase):
    def setUp(self):
        from api.v1.catalog.tests import _jwt_bearer
        from core.models import User

        self.owner = User.objects.create(email="document-import@example.com", role="venue_user")
        self.venue = Venue.objects.create(display_name="Документы")
        VenueUser.objects.create(venue=self.venue, user=self.owner, role="owner")
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(self.owner))

    def test_master_and_venue_documents_are_not_parsed_in_http(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        for url, data, task, parser in (
            (
                "/api/v1/catalog/master-products/import/preview",
                {"owner_id": str(self.owner.id)},
                "api.v1.catalog.services.master_catalog.process_master_product_import_upload_task.delay",
                "api.v1.catalog.views.master_catalog.parse_catalog_file",
            ),
            (
                f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/import/file/preview",
                {},
                "api.v1.catalog.views.venue_import.process_venue_import_session_task.delay",
                "api.v1.catalog.views.venue_import.parse_catalog_file",
            ),
        ):
            with (
                patch(parser, side_effect=AssertionError("Parsed in HTTP")),
                patch(task),
            ):
                response = self.client.post(
                    url, data | {"file": SimpleUploadedFile("items.docx", b"document")}, format="multipart"
                )
            self.assertEqual(response.status_code, 202, response.content)

    def test_docx_venue_worker_extracts_once_and_updates_total(self):
        from io import BytesIO

        from django.core.files.uploadedfile import SimpleUploadedFile
        from docx import Document

        from api.v1.catalog.tasks import process_venue_import_session_task
        from catalog.models import CatalogImportSession

        document = Document()
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text = "Наименование товара", "Ед. изм"
        table.cell(1, 0).text, table.cell(1, 1).text = "Мука", "кг"
        content = BytesIO()
        document.save(content)
        with patch("api.v1.catalog.views.venue_import.process_venue_import_session_task.delay"):
            response = self.client.post(
                f"/api/v1/catalog/venues/{self.venue.id}/product-matrix/import/file/preview",
                {"file": SimpleUploadedFile("items.docx", content.getvalue())},
                format="multipart",
            )
        self.assertEqual(response.status_code, 202, response.content)
        session = CatalogImportSession.objects.get(pk=response.json()["session_id"])
        self.assertEqual(session.rows_total, 0)
        with patch("api.v1.catalog.tasks.close_old_connections"):
            process_venue_import_session_task(session_id=str(session.id))
            process_venue_import_session_task(session_id=str(session.id))
        session.refresh_from_db()
        self.assertEqual((session.processing_status, session.rows_total, session.items.count()), ("ready", 1, 1))
        self.assertEqual(session.items.get().raw_values["requested_unit_label"], "кг")


@override_settings(VENUE_IMPORT_PREPARED_MATCHING_ENABLED=True)
class CatalogPreparedMatchingRegressionTests(APITestCase):
    setUp = fixtures.CatalogImportSessionAPITests.setUp
    auth = fixtures.CatalogImportSessionAPITests.auth

    def prepare(self):
        venue = Venue.objects.create(display_name="Батч")
        VenueUser.objects.create(venue=venue, user=self.owner, role="owner")
        self.session.domain, self.session.venue = "venue", venue
        self.session.save()
        self.item.normalized_values = {"raw_name": "Мука", "requested_unit_label": "кг"}
        self.item.save()
        vector = [1.0] + [0.0] * 383
        master = MasterProduct.objects.create(owner=self.owner, canonical_name="Мука", embedding=vector)
        return venue, master, vector

    def test_batch_embeddings_match_existing_master_without_model_in_transaction(self):
        venue, master, vector = self.prepare()
        self.auth(self.owner)
        with (
            patch("api.v1.catalog.embeddings.create_embeddings", return_value=[vector]) as embeddings,
            patch(
                "api.v1.catalog.services.master_core.find_candidate_master_products",
                side_effect=AssertionError("Model under locks"),
            ),
        ):
            response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        embeddings.assert_called_once_with(["Мука"])
        self.assertEqual(VenueProductMatrixItem.objects.get(venue=venue).master_product_id, master.id)

    def test_changed_candidate_is_left_unlinked_for_review(self):
        from types import SimpleNamespace

        from api.v1.catalog.import_sessions import prepare_venue_import_matching
        from api.v1.catalog.views import CatalogImportSessionDetailView

        venue, master, vector = self.prepare()
        with patch("api.v1.catalog.embeddings.create_embeddings", return_value=[vector]):
            prepared = prepare_venue_import_matching(self.session)
        master.canonical_name = "Чужая правка"
        master.save()
        response = CatalogImportSessionDetailView()._commit(SimpleNamespace(user=self.owner), self.session.id, prepared)
        self.assertEqual(response.status_code, 200)
        item = VenueProductMatrixItem.objects.get(venue=venue)
        self.assertIsNone(item.master_product_id)
        self.assertTrue(item.needs_review)
