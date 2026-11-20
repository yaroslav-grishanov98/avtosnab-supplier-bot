from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator

PROCESSING_STATUSES = {"pending", "processing", "ready", "failed"}


class SupplierProductImportOpenAPITests(SimpleTestCase):
    analyze_path = "/api/v1/suppliers/products/import/analyze"
    session_path = "/api/v1/suppliers/products/import/{session_id}"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def component(self, name):
        return self.schema["components"]["schemas"][name]

    def enum_values(self, field_schema):
        reference = field_schema["allOf"][0]["$ref"].split("/")[-1]
        return set(self.component(reference)["enum"])

    def test_analyze_always_answers_202_with_the_accepted_schema(self):
        operation = self.schema["paths"][self.analyze_path]["post"]

        self.assertEqual(set(operation["responses"]), {"202", "400", "401", "503"})
        self.assertIn(
            "SupplierProductImportSessionAcceptedResponse",
            operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
        )

    def test_accepted_response_declares_every_field_as_required_with_status_enum(self):
        accepted = self.component("SupplierProductImportSessionAcceptedResponse")

        self.assertEqual(set(accepted["required"]), {"id", "session_id", "status", "status_url"})
        self.assertEqual(self.enum_values(accepted["properties"]["status"]), PROCESSING_STATUSES)

    def test_session_get_documents_polling_query_parameters(self):
        operation = self.schema["paths"][self.session_path]["get"]
        parameters = {item["name"]: item for item in operation["parameters"] if item["in"] == "query"}

        self.assertEqual(
            set(parameters), {"include_items", "include_meta", "after_row_number", "limit", "action", "price_changed"}
        )
        self.assertIs(parameters["include_meta"]["schema"]["default"], False)
        for name in parameters:
            self.assertFalse(parameters[name].get("required", False), name)
            self.assertTrue(parameters[name]["description"], name)
        self.assertEqual(parameters["after_row_number"]["schema"]["minimum"], 0)
        self.assertEqual(parameters["limit"]["schema"]["minimum"], 1)
        self.assertEqual(set(parameters["action"]["schema"]["enum"]), {"review", "new", "replace", "skip"})
        self.assertIn("400", operation["responses"])

    def test_session_response_declares_pagination_fields(self):
        session = self.component("SupplierProductImportSessionResponse")

        self.assertIn("has_more", session["required"])
        self.assertIn("next_after_row_number", session["required"])
        self.assertEqual(session["properties"]["has_more"]["type"], "boolean")
        self.assertTrue(session["properties"]["next_after_row_number"].get("nullable"))

    def test_my_imports_list_is_documented_with_the_standard_envelope(self):
        path = self.session_path.rsplit("/", 1)[0]
        operation = self.schema["paths"][path]["get"]

        self.assertEqual(set(operation["responses"]), {"200", "400", "401", "403"})
        listing = self.component("SupplierProductImportSessionListResponse")
        self.assertEqual(set(listing["required"]), {"count", "next", "previous", "results"})

    def test_session_response_processing_status_includes_pending(self):
        session = self.component("SupplierProductImportSessionResponse")

        self.assertEqual(self.enum_values(session["properties"]["processing_status"]), PROCESSING_STATUSES)

    def test_defaults_operation_is_documented_with_availability_enum(self):
        operation = self.schema["paths"][self.session_path + "/items/defaults"]["patch"]

        self.assertEqual(set(operation["responses"]), {"200", "400", "401", "404", "409"})
        request = self.component("PatchedSupplierProductImportItemsDefaultsRequest")
        self.assertEqual(
            self.enum_values(request["properties"]["availability"]), {"in_stock", "on_order", "out_of_stock"}
        )
        response = self.component("SupplierProductImportItemsDefaultsResponse")
        self.assertEqual(response["required"], ["updated_count"])

    def test_item_field_sources_documents_every_allowed_source(self):
        field_sources = self.component("SupplierProductImportItem")["properties"]["field_sources"]
        sources = {"name", "section", "gs1_prefix", "llm", "user", "rule", "target"}

        self.assertTrue(field_sources["readOnly"])
        self.assertEqual(field_sources["type"], "object")
        values = field_sources["additionalProperties"]
        if "$ref" in values:
            values = self.component(values["$ref"].split("/")[-1])
        self.assertEqual(set(values["enum"]), sources)
        for source in sources:
            self.assertIn(f'"{source}"', field_sources["description"])
        self.assertIn('Отсутствие ключа = source "file"', field_sources["description"])
