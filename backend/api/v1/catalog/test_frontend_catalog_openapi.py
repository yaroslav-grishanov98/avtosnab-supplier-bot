from django.conf import settings
from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator


class FrontendCatalogOpenAPIContractTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def resolve(self, schema):
        while "$ref" in schema:
            schema = self.schema["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
        return schema

    def component_name(self, schema):
        if "$ref" in schema:
            return schema["$ref"].rsplit("/", 1)[-1]
        if len(schema.get("allOf", [])) == 1:
            return self.component_name(schema["allOf"][0])
        return None

    def response_schema(self, operation, status_code):
        return operation["responses"][str(status_code)]["content"]["application/json"]["schema"]

    def request_schema(self, operation, content_type="application/json"):
        return operation["requestBody"]["content"][content_type]["schema"]

    def test_company_catalog_picker_operations_are_explicit(self):
        prefix = "/api/v1/catalog/venues/{venue_id}/product-matrix"
        master = self.schema["paths"][f"{prefix}/master-product-suggestions"]["get"]
        supplier = self.schema["paths"][f"{prefix}/supplier-product-suggestions"]["get"]
        apply = self.schema["paths"][f"{prefix}/{{item_id}}/master-product"]["put"]

        self.assertEqual(master["operationId"], "venue_company_products_master_suggestions")
        self.assertEqual(supplier["operationId"], "venue_company_products_supplier_suggestions")
        self.assertEqual(apply["operationId"], "venue_company_product_set_master_product")
        for operation in (master, supplier, apply):
            self.assertEqual(operation["tags"], ["Справочник товаров компании"])

    def test_company_file_apply_accepts_all_preview_fields(self):
        operation = self.schema["paths"]["/api/v1/catalog/venues/{venue_id}/product-matrix/import/apply"]["post"]
        request = self.resolve(operation["requestBody"]["content"]["application/json"]["schema"])
        decision = self.resolve(request["properties"]["decisions"]["items"])
        self.assertTrue(
            {"row_number", "raw_name", "requested_unit_label", "note", "action", "matrix_item_id"}.issubset(
                decision["properties"]
            )
        )

    def test_master_import_is_a_two_step_swagger_section(self):
        preview = self.schema["paths"]["/api/v1/catalog/master-products/import/preview"]["post"]
        apply = self.schema["paths"]["/api/v1/catalog/master-products/import/apply"]["post"]
        self.assertEqual(preview["operationId"], "master_catalog_import_preview")
        self.assertEqual(apply["operationId"], "master_catalog_import_apply")
        self.assertEqual(preview["tags"], ["Импорт Мастер-справочника"])
        self.assertEqual(apply["tags"], ["Импорт Мастер-справочника"])
        self.assertTrue(preview["summary"].startswith("Шаг 1."))
        self.assertTrue(apply["summary"].startswith("Шаг 2."))
        self.assertIn("multipart/form-data", preview["requestBody"]["content"])
        self.assertEqual(
            self.component_name(self.request_schema(preview, "multipart/form-data")),
            "MasterProductImportRequest",
        )
        self.assertEqual(self.component_name(self.response_schema(preview, 200)), "MasterProductImportPreviewResponse")
        self.assertEqual(self.component_name(self.request_schema(apply)), "MasterProductImportApplyRequest")
        self.assertEqual(self.component_name(self.response_schema(apply, 200)), "MasterProductImportApplyResponse")

    def test_company_product_matrix_crud_contract(self):
        collection = self.schema["paths"]["/api/v1/catalog/venues/{venue_id}/product-matrix"]
        detail = self.schema["paths"]["/api/v1/catalog/venues/{venue_id}/product-matrix/{item_id}"]

        get = collection["get"]
        post = collection["post"]
        patch = detail["patch"]
        delete = detail["delete"]

        expected = {
            "venue_company_products_list": get,
            "venue_company_products_create": post,
            "venue_company_products_update": patch,
            "venue_company_products_archive": delete,
        }
        for operation_id, operation in expected.items():
            with self.subTest(operation_id=operation_id):
                self.assertEqual(operation["operationId"], operation_id)
                self.assertEqual(operation["tags"], ["Справочник товаров компании"])

        parameter_names = {parameter["name"] for parameter in get["parameters"]}
        self.assertTrue({"filter", "q", "subdivision_id", "page", "page_size"}.issubset(parameter_names))
        self.assertEqual(self.component_name(self.response_schema(get, 200)), "VenueProductMatrixListResponse")
        self.assertEqual(self.component_name(self.request_schema(post)), "VenueProductMatrixItemCreate")
        self.assertEqual(self.component_name(self.response_schema(post, 201)), "VenueProductMatrixItem")
        self.assertIn("409", post["responses"])
        patch_request = self.resolve(self.request_schema(patch))
        self.assertTrue(
            {
                "requested_name",
                "requested_unit_label",
                "note",
                "comment",
                "supplier_product_id",
                "subdivision_id",
            }.issubset(patch_request["properties"])
        )
        self.assertEqual(self.component_name(self.response_schema(patch, 200)), "VenueProductMatrixItem")
        self.assertIn("204", delete["responses"])
        self.assertNotIn("content", delete["responses"]["204"])

    def test_master_catalog_collection_contract(self):
        collection = self.schema["paths"]["/api/v1/catalog/master-products"]
        get = collection["get"]
        post = collection["post"]

        self.assertEqual(get["operationId"], "master_catalog_list")
        self.assertEqual(post["operationId"], "master_catalog_create")
        self.assertEqual(get["tags"], ["Мастер-справочник"])
        self.assertEqual(post["tags"], ["Мастер-справочник"])

        parameters = {parameter["name"]: parameter for parameter in get["parameters"]}
        self.assertTrue({"owner_id", "q", "page", "page_size"}.issubset(parameters))
        self.assertTrue(parameters["owner_id"]["required"])
        self.assertEqual(self.component_name(self.response_schema(get, 200)), "MasterProductListResponse")
        self.assertEqual(self.component_name(self.request_schema(post)), "MasterProductCreate")
        self.assertEqual(self.component_name(self.response_schema(post, 201)), "MasterProduct")

    def test_master_catalog_detail_contract(self):
        detail = self.schema["paths"]["/api/v1/catalog/master-products/{master_product_id}"]
        get = detail["get"]
        patch = detail["patch"]
        delete = detail["delete"]

        self.assertEqual(get["operationId"], "master_catalog_retrieve")
        self.assertEqual(patch["operationId"], "master_catalog_update")
        self.assertEqual(delete["operationId"], "master_catalog_delete")
        for operation in (get, patch, delete):
            self.assertEqual(operation["tags"], ["Мастер-справочник"])

        self.assertEqual(self.component_name(self.response_schema(get, 200)), "MasterProduct")
        patch_request = self.resolve(self.request_schema(patch))
        self.assertTrue({"canonical_name", "unit_id", "net_value", "note"}.issubset(patch_request["properties"]))
        self.assertEqual(self.component_name(self.response_schema(patch, 200)), "MasterProduct")
        self.assertIn("204", delete["responses"])
        self.assertNotIn("content", delete["responses"]["204"])

    def test_catalog_supplier_detail_contract(self):
        operation = self.schema["paths"]["/api/v1/catalog/suppliers/{supplier_id}"]["get"]
        self.assertEqual(operation["operationId"], "catalog_supplier_retrieve")
        self.assertEqual(operation["tags"], ["Каталог"])
        self.assertEqual(self.component_name(self.response_schema(operation, 200)), "CatalogSupplierDetail")
        self.assertIn("404", operation["responses"])

    def test_master_company_batch_flow_has_stable_operation_ids(self):
        prefix = "/api/v1/catalog/master-products/{master_product_id}/venues"
        breakdown = self.schema["paths"][prefix]["get"]
        preview = self.schema["paths"][f"{prefix}/batch/preview"]["post"]
        apply = self.schema["paths"][f"{prefix}/batch/apply"]["post"]

        self.assertEqual(breakdown["operationId"], "master_catalog_venues_breakdown")
        self.assertEqual(preview["operationId"], "master_catalog_venues_batch_preview")
        self.assertEqual(apply["operationId"], "master_catalog_venues_batch_apply")
        self.assertIn("только компании", preview["description"])
        self.assertIn("только компании", apply["description"])

    def test_supplier_product_crud_operation_ids_do_not_collide(self):
        collection = self.schema["paths"]["/api/v1/suppliers/products"]
        detail = self.schema["paths"]["/api/v1/suppliers/products/{id}"]
        operation_ids = {
            collection["get"]["operationId"],
            collection["post"]["operationId"],
            collection["delete"]["operationId"],
            detail["patch"]["operationId"],
            detail["delete"]["operationId"],
        }
        self.assertEqual(
            operation_ids,
            {
                "supplier_products_list",
                "supplier_products_create",
                "supplier_products_delete_all",
                "supplier_products_update",
                "supplier_products_delete",
            },
        )

    def test_legacy_supplier_price_upload_flow_is_hidden(self):
        # 2026-09-30 (413c58d): старый поток загрузки прайса /suppliers/uploads скрыт из Swagger —
        # фронт работает через единые import-сессии; сами эндпоинты остаются рабочими.
        legacy = [path for path in self.schema["paths"] if path.startswith("/api/v1/suppliers/uploads")]
        self.assertEqual(legacy, [])

    def test_frontend_terms_are_registered_as_swagger_tags(self):
        tags = {item["name"] for item in settings.SPECTACULAR_SETTINGS["TAGS"]}
        self.assertTrue(
            {
                "Мастер-справочник",
                "Импорт Мастер-справочника",
                "Справочник товаров компании",
                "Импорт справочника товаров компании",
                "Товары поставщика",
                "Импорт прайса поставщика",
            }.issubset(tags)
        )
