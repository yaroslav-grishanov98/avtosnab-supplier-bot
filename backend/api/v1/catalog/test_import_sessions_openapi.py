from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator

PROCESSING_STATUSES = {"pending", "processing", "ready", "failed"}


class CatalogImportSessionOpenAPITests(SimpleTestCase):
    session_path = "/api/v1/catalog/import-sessions/{session_id}"
    item_path = "/api/v1/catalog/import-sessions/{session_id}/items/{item_id}"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def component(self, name):
        return self.schema["components"]["schemas"][name]

    def enum_values(self, field_schema):
        reference = field_schema["allOf"][0]["$ref"].split("/")[-1]
        return set(self.component(reference)["enum"])

    def test_get_session_has_a_typed_response_with_lifecycle_enum(self):
        operation = self.schema["paths"][self.session_path]["get"]
        self.assertIn(
            "CatalogImportSessionResponse",
            operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
        )
        session = self.component("CatalogImportSessionResponse")
        self.assertTrue(
            {"id", "domain", "status", "processing_status", "rows_total", "rows_done", "percent", "items"}
            <= set(session["required"])
        )
        self.assertEqual(self.enum_values(session["properties"]["processing_status"]), PROCESSING_STATUSES)
        self.assertEqual(self.enum_values(session["properties"]["domain"]), {"master", "venue"})
        self.assertTrue(session["properties"]["error_message"]["nullable"])

    def test_item_declares_action_enum_and_nullable_target(self):
        item = self.component("CatalogImportSessionItem")
        self.assertEqual(
            self.enum_values(item["properties"]["action"]),
            {"review", "create_new", "update_existing", "replace", "skip"},
        )
        self.assertTrue(item["properties"]["target_id"]["nullable"])

    def test_commit_documents_every_real_status_code(self):
        operation = self.schema["paths"][self.session_path]["post"]
        self.assertEqual(set(operation["responses"]), {"200", "400", "401", "404", "409"})
        self.assertIn(
            "CatalogImportSessionCommitResponse",
            operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
        )

    def test_patch_has_typed_request_and_response(self):
        operation = self.schema["paths"][self.item_path]["patch"]
        self.assertIn(
            "CatalogImportSessionItemPatchRequest",
            operation["requestBody"]["content"]["application/json"]["schema"]["$ref"],
        )
        self.assertEqual(set(operation["responses"]), {"200", "400", "401", "403", "404", "409"})

    def test_preview_responses_document_session_id(self):
        for name in ("MasterProductImportPreviewResponse", "VenueImportFilePreviewResponse"):
            self.assertIn("session_id", self.component(name)["properties"], name)

    def test_product_list_keeps_its_own_documentation(self):
        operation = self.schema["paths"]["/api/v1/catalog/products"]["get"]
        self.assertEqual(operation["summary"], "Список товаров каталога")
