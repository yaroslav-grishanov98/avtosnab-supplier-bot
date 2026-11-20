from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from api.v1.catalog.tests import _jwt_bearer
from api.v1.suppliers.product_import_selectors import analyze_supplier_product_import
from api.v1.suppliers.tests import _make_supplier
from catalog.models import CatalogImportItem, CatalogImportSession, MasterProduct
from core.models import User
from suppliers.models import SupplierProductImportSession


class ImportConcurrencyRegressionTests(TransactionTestCase):
    def test_two_commits_create_one_master_card(self):
        owner = User.objects.create(email="concurrent-import@example.com", role="venue_user")
        session = CatalogImportSession.objects.create(owner=owner, domain="master")
        CatalogImportItem.objects.create(
            session=session,
            row_number=1,
            raw_values={"raw_name": "Мука"},
            normalized_values={"raw_name": "Мука"},
            action="create_new",
        )
        token, barrier = _jwt_bearer(owner), Barrier(2)

        def commit():
            try:
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=token)
                barrier.wait(timeout=10)
                return client.post(f"/api/v1/catalog/import-sessions/{session.id}", {}, format="json").status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: commit(), range(2)))
        self.assertEqual(sorted(results), [200, 400])
        self.assertEqual(MasterProduct.objects.filter(owner=owner).count(), 1)

    def test_two_analyzes_reuse_one_supplier_session(self):
        supplier, barrier = _make_supplier(), Barrier(2)

        def analyze():
            try:
                barrier.wait(timeout=10)
                session = analyze_supplier_product_import(
                    supplier=supplier,
                    file=SimpleUploadedFile("items.txt", "Мука\nСоль".encode()),
                    source_file_name="items.txt",
                )
                return session.id
            finally:
                connections.close_all()

        with (
            patch("api.v1.suppliers.tasks.process_supplier_product_import_session_task.delay"),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            identifiers = list(executor.map(lambda _: analyze(), range(2)))
        self.assertEqual(identifiers[0], identifiers[1])
        self.assertEqual(SupplierProductImportSession.objects.filter(supplier=supplier).count(), 1)
