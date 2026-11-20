from django.test import override_settings
from rest_framework.test import APITestCase

from api.v1.suppliers.services import issue_supplier_cabinet_tokens
from suppliers.models import Supplier, SupplierProductImportItem, SupplierProductImportSession

LIST_URL = "/api/v1/suppliers/products/import"


def _supplier(name):
    return Supplier.objects.create(legal_name=name, display_name=name)


def _bearer(supplier):
    return f"Bearer {issue_supplier_cabinet_tokens(supplier)['access']}"


def _session(supplier, rows=0, *, processing_status="ready", rows_done=None, status="open", name="price.xlsx"):
    session = SupplierProductImportSession.objects.create(
        supplier=supplier,
        source_file_name=name,
        rows_total=rows,
        rows_done=rows if rows_done is None else rows_done,
        processing_status=processing_status,
        status=status,
    )
    actions = ["review", "new", "replace", "skip"]
    for number in range(1, rows + 1):
        SupplierProductImportItem.objects.create(
            session=session,
            row_number=number,
            raw_name=f"Товар {number}",
            name=f"Товар {number}",
            action=actions[number % 4],
        )
    return session


class ItemsPaginationTests(APITestCase):
    def setUp(self):
        self.supplier = _supplier("Pagination LLC")
        self.client.credentials(HTTP_AUTHORIZATION=_bearer(self.supplier))

    def _get(self, session, **params):
        response = self.client.get(f"{LIST_URL}/{session.id}", params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_pages_follow_the_cursor_without_gaps_or_repeats(self):
        session = _session(self.supplier, rows=25)
        seen, after = [], None
        for _ in range(10):
            params = {"limit": 10}
            if after is not None:
                params["after_row_number"] = after
            data = self._get(session, **params)
            seen += [item["row_number"] for item in data["items"]]
            if not data["has_more"]:
                self.assertIsNone(data["next_after_row_number"])
                break
            self.assertEqual(data["next_after_row_number"], data["items"][-1]["row_number"])
            after = data["next_after_row_number"]
        self.assertEqual(seen, list(range(1, 26)))

    def test_exact_page_boundary_has_no_more(self):
        session = _session(self.supplier, rows=10)
        data = self._get(session, limit=10)
        self.assertEqual(len(data["items"]), 10)
        self.assertFalse(data["has_more"])
        self.assertIsNone(data["next_after_row_number"])

    @override_settings(SUPPLIER_PRODUCT_IMPORT_ITEMS_PAGE_SIZE=4, SUPPLIER_PRODUCT_IMPORT_ITEMS_MAX_PAGE_SIZE=6)
    def test_default_and_maximum_page_size(self):
        session = _session(self.supplier, rows=12)
        default = self._get(session)
        self.assertEqual((len(default["items"]), default["has_more"]), (4, True))
        capped = self._get(session, limit=500)
        self.assertEqual((len(capped["items"]), capped["has_more"]), (6, True))

    def test_field_meta_is_opt_in(self):
        session = _session(self.supplier, rows=3)
        default = self._get(session)
        self.assertEqual(len(default["items"]), 3)
        self.assertTrue(all("field_meta" not in item for item in default["items"]))
        # Остальная диагностика и поля строки на месте.
        self.assertTrue(
            all({"parse_issues", "business_issues", "validation_errors"} <= set(i) for i in default["items"])
        )
        with_meta = self._get(session, include_meta="true")
        self.assertTrue(all("field_meta" in item for item in with_meta["items"]))

    def test_include_meta_must_be_boolean(self):
        session = _session(self.supplier, rows=1)
        response = self.client.get(f"{LIST_URL}/{session.id}", {"include_meta": "maybe"})
        self.assertEqual(response.status_code, 400, response.content)

    def test_counts_cover_all_rows_not_the_page(self):
        session = _session(self.supplier, rows=20)
        data = self._get(session, limit=3)
        self.assertEqual(data["counts"]["all"], 20)
        self.assertEqual(sum(data["counts"][key] for key in ("review", "new", "replace", "skip")), 20)

    def test_action_filter_returns_only_that_action_and_pages_over_it(self):
        session = _session(self.supplier, rows=20)
        first = self._get(session, action="new", limit=3)
        self.assertTrue(first["items"])
        self.assertTrue(all(item["action"] == "new" for item in first["items"]))
        self.assertEqual(first["counts"]["new"], 5)
        self.assertTrue(first["has_more"])
        second = self._get(session, action="new", limit=3, after_row_number=first["next_after_row_number"])
        self.assertEqual(len(first["items"]) + len(second["items"]), 5)
        self.assertFalse(second["has_more"])

    def test_include_items_false_has_no_items_and_no_more(self):
        session = _session(self.supplier, rows=5)
        data = self._get(session, include_items="false")
        self.assertEqual((data["items"], data["has_more"], data["next_after_row_number"]), ([], False, None))

    def test_while_processing_only_finished_rows_are_paged(self):
        session = _session(self.supplier, rows=10, processing_status="processing", rows_done=4)
        data = self._get(session, limit=3)
        self.assertEqual([item["row_number"] for item in data["items"]], [1, 2, 3])
        self.assertTrue(data["has_more"])
        rest = self._get(session, limit=3, after_row_number=data["next_after_row_number"])
        self.assertEqual([item["row_number"] for item in rest["items"]], [4])
        self.assertFalse(rest["has_more"])

    def test_invalid_parameters_are_rejected(self):
        session = _session(self.supplier, rows=3)
        for params in ({"limit": 0}, {"limit": "abc"}, {"action": "delete"}):
            response = self.client.get(f"{LIST_URL}/{session.id}", params)
            self.assertEqual(response.status_code, 400, params)


class SessionListTests(APITestCase):
    def setUp(self):
        self.supplier = _supplier("List LLC")
        self.other = _supplier("Other LLC")
        self.client.credentials(HTTP_AUTHORIZATION=_bearer(self.supplier))

    def test_lists_only_own_open_sessions_newest_first_with_progress(self):
        old = _session(self.supplier, rows=10, name="old.xlsx")
        running = _session(self.supplier, rows=200, processing_status="processing", rows_done=100, name="big.xlsx")
        _session(self.supplier, rows=5, status="committed", name="done.xlsx")
        _session(self.other, rows=5, name="foreign.xlsx")
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["count"], 2)
        self.assertEqual([row["id"] for row in data["results"]], [str(running.id), str(old.id)])
        first = data["results"][0]
        self.assertEqual(
            (first["source_file_name"], first["processing_status"], first["percent"], first["rows_total"]),
            ("big.xlsx", "processing", 50, 200),
        )
        self.assertEqual(first["status_url"], f"/api/v1/suppliers/products/import/{running.id}")

    def test_failed_session_is_listed_with_its_error(self):
        failed = _session(self.supplier, rows=0, processing_status="failed", name="bad.pdf")
        SupplierProductImportSession.objects.filter(pk=failed.pk).update(error_message="Не найдена колонка.")
        row = self.client.get(LIST_URL).json()["results"][0]
        self.assertEqual((row["processing_status"], row["error_message"]), ("failed", "Не найдена колонка."))

    def test_status_filter(self):
        _session(self.supplier, rows=3, name="open.xlsx")
        _session(self.supplier, rows=3, status="committed", name="done.xlsx")

        def names(status):
            rows = self.client.get(LIST_URL, {"status": status}).json()["results"]
            return {row["source_file_name"] for row in rows}

        self.assertEqual(names("open"), {"open.xlsx"})
        self.assertEqual(names("committed"), {"done.xlsx"})
        self.assertEqual(names("all"), {"open.xlsx", "done.xlsx"})
        self.assertEqual(self.client.get(LIST_URL, {"status": "bogus"}).status_code, 400)

    def test_pagination_envelope(self):
        for number in range(3):
            _session(self.supplier, rows=1, name=f"f{number}.xlsx")
        data = self.client.get(LIST_URL, {"page_size": 2}).json()
        self.assertEqual((data["count"], len(data["results"])), (3, 2))
        self.assertIsNotNone(data["next"])
        self.assertIsNone(data["previous"])

    def test_requires_a_cabinet_session(self):
        self.client.credentials()
        self.assertEqual(self.client.get(LIST_URL).status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer wrong")
        self.assertIn(self.client.get(LIST_URL).status_code, (401, 403))
