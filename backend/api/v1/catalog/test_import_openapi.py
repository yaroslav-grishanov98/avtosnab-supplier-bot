from django.conf import settings
from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator


class VenueProductImportOpenAPITests(SimpleTestCase):
    file_preview_path = "/api/v1/catalog/venues/{venue_id}/product-matrix/import/file/preview"
    apply_path = "/api/v1/catalog/venues/{venue_id}/product-matrix/import/apply"
    raw_preview_path = "/api/v1/catalog/venues/{venue_id}/product-matrix/import/preview"
    master_preview_path = "/api/v1/catalog/venues/{venue_id}/product-matrix/import/master-match/preview"
    master_apply_path = "/api/v1/catalog/venues/{venue_id}/product-matrix/import/master-match/apply"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def operation(self, path):
        return self.schema["paths"][path]["post"]

    def test_main_file_flow_is_separate_and_ordered(self):
        paths = list(self.schema["paths"])
        self.assertLess(paths.index(self.file_preview_path), paths.index(self.apply_path))

        file_preview = self.operation(self.file_preview_path)
        apply = self.operation(self.apply_path)

        self.assertEqual(file_preview["operationId"], "venue_import_file_preview")
        self.assertEqual(file_preview["tags"], ["Импорт справочника товаров компании"])
        self.assertTrue(file_preview["summary"].startswith("Шаг 1."))
        self.assertIn("multipart/form-data", file_preview["requestBody"]["content"])
        self.assertIn("examples", file_preview["responses"]["200"]["content"]["application/json"])

        self.assertEqual(apply["operationId"], "venue_import_apply")
        self.assertEqual(apply["tags"], ["Импорт справочника товаров компании"])
        self.assertTrue(apply["summary"].startswith("Шаг 2."))
        self.assertIn("examples", apply["requestBody"]["content"]["application/json"])

    def test_alternative_operations_are_marked_as_additional(self):
        expected_ids = {
            self.raw_preview_path: "venue_import_raw_names_preview",
            self.master_preview_path: "venue_import_master_match_preview",
            self.master_apply_path: "venue_import_master_match_apply",
        }
        for path, operation_id in expected_ids.items():
            with self.subTest(path=path):
                operation = self.operation(path)
                self.assertEqual(operation["operationId"], operation_id)
                self.assertEqual(operation["tags"], ["Импорт справочника компании — дополнительные операции"])
                self.assertTrue(operation["summary"].startswith("Дополнительно."))

    def test_import_tag_descriptions_explain_the_frontend_flow(self):
        tags = {item["name"]: item["description"] for item in settings.SPECTACULAR_SETTINGS["TAGS"]}
        self.assertIn("file/preview", tags["Импорт справочника товаров компании"])
        self.assertIn("import/apply", tags["Импорт справочника товаров компании"])
        self.assertIn("не нужны", tags["Импорт справочника компании — дополнительные операции"])
