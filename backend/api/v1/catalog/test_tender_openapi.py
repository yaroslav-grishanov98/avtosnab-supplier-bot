import json
import re

from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator


class TenderOpenAPIContractTests(SimpleTestCase):
    tag = ["Тендеры"]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def operation(self, path, method):
        return self.schema["paths"][path][method]

    def component_name(self, schema):
        if "$ref" in schema:
            return schema["$ref"].rsplit("/", 1)[-1]
        if len(schema.get("allOf", [])) == 1:
            return self.component_name(schema["allOf"][0])
        return None

    def response_schema(self, operation, status_code):
        return operation["responses"][str(status_code)]["content"]["application/json"]["schema"]

    def request_schema(self, operation):
        return operation["requestBody"]["content"]["application/json"]["schema"]

    def assert_operation(self, operation, operation_id, *, statuses):
        self.assertEqual(operation["operationId"], operation_id)
        self.assertEqual(operation["tags"], self.tag)
        self.assertTrue(set(map(str, statuses)).issubset(operation["responses"]))

    def assert_array_of(self, schema, component):
        self.assertEqual(schema["type"], "array")
        self.assertEqual(self.component_name(schema["items"]), component)

    def test_tender_collection_contract(self):
        path = "/api/v1/catalog/tenders"
        get = self.operation(path, "get")
        post = self.operation(path, "post")

        self.assert_operation(get, "tender_list", statuses={200, 400, 401, 403})
        self.assert_operation(post, "tender_create", statuses={201, 400, 401, 403})

        parameters = {parameter["name"]: parameter for parameter in get["parameters"]}
        self.assertTrue({"owner_id", "page", "page_size"}.issubset(parameters))
        self.assertTrue(parameters["owner_id"]["required"])
        self.assertEqual(self.component_name(self.response_schema(get, 200)), "TenderListResponse")
        self.assertEqual(self.component_name(self.request_schema(post)), "TenderCreate")
        self.assertEqual(self.component_name(self.response_schema(post, 201)), "Tender")

    def test_tender_detail_contract(self):
        operation = self.operation("/api/v1/catalog/tenders/{tender_id}", "get")

        self.assert_operation(operation, "tender_retrieve", statuses={200, 401, 403, 404})
        self.assertEqual(self.component_name(self.response_schema(operation, 200)), "Tender")

    def test_tender_editing_contract(self):
        tender = self.operation("/api/v1/catalog/tenders/{tender_id}", "patch")
        item = self.operation("/api/v1/catalog/tenders/{tender_id}/items/{item_id}", "patch")

        self.assert_operation(tender, "tender_update", statuses={200, 400, 401, 403, 404})
        self.assertEqual(self.component_name(self.request_schema(tender)), "PatchedTenderUpdate")
        self.assertEqual(self.component_name(self.response_schema(tender, 200)), "Tender")
        self.assert_operation(item, "tender_item_update", statuses={200, 400, 401, 403, 404, 409})
        self.assertEqual(self.component_name(self.request_schema(item)), "PatchedTenderItemUpdate")
        self.assertEqual(self.component_name(self.response_schema(item, 200)), "TenderItem")
        self.assertEqual(self.component_name(self.response_schema(item, 409)), "TenderActiveConflict")

        delete = self.operation("/api/v1/catalog/tenders/{tender_id}/items/{item_id}", "delete")
        self.assert_operation(delete, "tender_item_delete", statuses={204, 400, 401, 403, 404})

    def test_tender_counters_are_required_integers(self):
        tender = self.schema["components"]["schemas"]["Tender"]

        for field in ("items_count", "offers_count"):
            with self.subTest(field=field):
                self.assertIn(field, tender["required"])
                self.assertEqual(tender["properties"][field]["type"], "integer")
                self.assertNotIn("nullable", tender["properties"][field])

    def test_tender_item_counters_are_required_integers(self):
        item = self.schema["components"]["schemas"]["TenderItem"]

        for field in ("offers_count", "distributed_venues_count"):
            with self.subTest(field=field):
                self.assertIn(field, item["required"])
                self.assertEqual(item["properties"][field]["type"], "integer")

    def test_tender_contract_avoids_forbidden_term(self):
        """Raw ТЗ §23 forbids calling the chosen supplier a winner; generated
        hints carry these descriptions straight into the UI."""
        operations = [
            op
            for path, ops in self.schema["paths"].items()
            for op in ops.values()
            if isinstance(op, dict) and ("Тендеры" in op.get("tags", []) or "tender-offer" in path)
        ]
        seen, pending = set(), [json.dumps(operations, ensure_ascii=False)]
        texts = []
        while pending:
            text = pending.pop()
            texts.append(text)
            for name in re.findall(r"#/components/schemas/(\w+)", text):
                if name not in seen:
                    seen.add(name)
                    pending.append(json.dumps(self.schema["components"]["schemas"][name], ensure_ascii=False))

        text = " ".join(texts).lower()
        self.assertNotIn("побед", text)
        # ТЗ §55 (2026-09-25): the supplier enters the final price; no VAT on top.
        self.assertNotIn("с ндс", text)

    def test_offered_product_price_list_name_is_required_and_nullable(self):
        for component in ("TenderOffer", "TenderOfferAutofillSuggestion"):
            with self.subTest(component=component):
                schema = self.schema["components"]["schemas"][component]
                self.assertIn("supplier_product_raw_name", schema["required"])
                self.assertTrue(schema["properties"]["supplier_product_raw_name"].get("nullable"))

    def test_supplier_offer_withdraw_contract(self):
        path = "/api/v1/catalog/suppliers/{supplier_id}/tenders/{tender_id}/items/{item_id}/offer"
        operation = self.operation(path, "delete")

        self.assert_operation(operation, "supplier_tender_offer_withdraw", statuses={200, 400, 401, 403, 404})
        self.assertEqual(self.component_name(self.response_schema(operation, 200)), "TenderOffer")
        offer = self.schema["components"]["schemas"]["TenderOffer"]
        self.assertIn("withdrawn_at", offer["required"])
        self.assertTrue(offer["properties"]["withdrawn_at"].get("nullable"))

    def test_offer_table_fields(self):
        offer = self.schema["components"]["schemas"]["TenderOffer"]
        nullable = (
            "supplier_product_net_value",
            "supplier_product_net_unit",
            "supplier_product_min_order_qty",
            "supplier_product_delivery_multiple",
        )
        for field in ("supplier_name", "distributed_venue_ids", *nullable):
            with self.subTest(field=field):
                self.assertIn(field, offer["required"])
        for field in nullable:
            with self.subTest(field=field):
                self.assertTrue(offer["properties"][field].get("nullable"))
        self.assertEqual(offer["properties"]["distributed_venue_ids"]["type"], "array")

    def test_venue_availability_contract(self):
        operation = self.operation("/api/v1/catalog/tenders/venue-availability", "post")

        self.assert_operation(operation, "tender_venue_availability", statuses={200, 400, 401, 403})
        self.assertEqual(self.component_name(self.request_schema(operation)), "TenderVenueAvailabilityRequest")
        self.assert_array_of(self.response_schema(operation, 200), "TenderVenueAvailability")

    def test_item_entry_venue_ids_optional_in_both_create_paths(self):
        entry = self.schema["components"]["schemas"]["TenderItemCreateEntry"]
        self.assertNotIn("venue_ids", entry.get("required", []))
        self.assertTrue(entry["properties"]["venue_ids"].get("nullable"))
        for path in ("/api/v1/catalog/tenders", "/api/v1/catalog/tenders/{tender_id}/items"):
            with self.subTest(path=path):
                body = self.schema["components"]["schemas"][
                    self.component_name(self.request_schema(self.operation(path, "post")))
                ]
                self.assertEqual(self.component_name(body["properties"]["items"]["items"]), "TenderItemCreateEntry")

    def test_tender_items_contract(self):
        path = "/api/v1/catalog/tenders/{tender_id}/items"
        get = self.operation(path, "get")
        post = self.operation(path, "post")

        self.assert_operation(get, "tender_item_list", statuses={200, 401, 403, 404})
        self.assert_operation(post, "tender_item_create", statuses={201, 400, 401, 403, 404, 409})
        self.assert_array_of(self.response_schema(get, 200), "TenderItem")
        self.assertEqual(self.component_name(self.request_schema(post)), "TenderItemsCreate")
        self.assert_array_of(self.response_schema(post, 201), "TenderItem")

    def test_tender_item_visibility_contract(self):
        prefix = "/api/v1/catalog/tenders/{tender_id}/items/{item_id}"
        expected = {
            f"{prefix}/publish": "tender_item_publish",
            f"{prefix}/unpublish": "tender_item_unpublish",
        }

        for path, operation_id in expected.items():
            with self.subTest(path=path):
                operation = self.operation(path, "post")
                self.assert_operation(operation, operation_id, statuses={200, 400, 401, 403, 404})
                self.assertNotIn("requestBody", operation)
                self.assertEqual(self.component_name(self.response_schema(operation, 200)), "TenderItem")

    def test_tender_lifecycle_contract(self):
        publish = self.operation("/api/v1/catalog/tenders/{tender_id}/publish", "post")
        complete_path = "/api/v1/catalog/tenders/{tender_id}/complete"
        summary = self.operation(complete_path, "get")
        complete = self.operation(complete_path, "post")

        self.assert_operation(publish, "tender_publish", statuses={200, 400, 401, 403, 404, 409})
        self.assertNotIn("requestBody", publish)
        self.assertEqual(self.component_name(self.response_schema(publish, 200)), "Tender")

        self.assert_operation(summary, "tender_complete_summary", statuses={200, 401, 403, 404})
        self.assertEqual(self.component_name(self.response_schema(summary, 200)), "TenderCompletionSummary")

        self.assert_operation(complete, "tender_complete", statuses={200, 400, 401, 403, 404})
        self.assertNotIn("requestBody", complete)
        self.assertEqual(self.component_name(self.response_schema(complete, 200)), "Tender")

    def test_supplier_tender_offer_contract(self):
        path = "/api/v1/catalog/suppliers/{supplier_id}/tenders/{tender_id}/items/{item_id}/offer"
        operation = self.operation(path, "put")

        self.assert_operation(operation, "supplier_tender_offer_submit", statuses={200, 400, 401, 403, 404})
        self.assertEqual(self.component_name(self.request_schema(operation)), "TenderOfferUpsert")
        self.assertEqual(self.component_name(self.response_schema(operation, 200)), "TenderOffer")

    def test_supplier_tender_header_contract(self):
        operation = self.operation("/api/v1/catalog/suppliers/{supplier_id}/tenders/{tender_id}", "get")

        self.assert_operation(operation, "supplier_tender_retrieve", statuses={200, 401, 403, 404})
        self.assertEqual(self.component_name(self.response_schema(operation, 200)), "SupplierTender")
        header = self.schema["components"]["schemas"]["SupplierTender"]
        self.assertNotIn("invited_supplier_ids", header["properties"])
        for field in ("customer_venues", "published_items_count", "my_offers_count"):
            with self.subTest(field=field):
                self.assertIn(field, header["required"])
        self.assert_array_of(header["properties"]["customer_venues"], "TenderCustomerVenue")

    def test_supplier_tender_list_contract(self):
        operation = self.operation("/api/v1/catalog/suppliers/{supplier_id}/tenders", "get")

        self.assert_operation(operation, "supplier_tender_list", statuses={200, 400, 401, 403})
        self.assertEqual(self.component_name(self.response_schema(operation, 200)), "SupplierTenderListResponse")
        page = self.schema["components"]["schemas"]["SupplierTenderListResponse"]
        self.assert_array_of(page["properties"]["results"], "SupplierTender")
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
        self.assertTrue({"status", "q", "page", "page_size"}.issubset(parameters))
        self.assertEqual(parameters["status"]["schema"]["default"], "active")

    def test_tender_status_enums_keep_their_names(self):
        """A second component reusing TenderStatus.choices made drf-spectacular
        rename Tender's enum to a hash name — a breaking rename for generated
        frontend types. Supplier-side status has its own two-value set."""
        schemas = self.schema["components"]["schemas"]

        self.assertEqual(self.component_name(schemas["Tender"]["properties"]["status"]), "TenderStatusEnum")
        supplier_status = self.component_name(schemas["SupplierTender"]["properties"]["status"])
        self.assertEqual(supplier_status, "SupplierTenderStatusEnum")
        self.assertEqual(schemas[supplier_status]["enum"], ["active", "completed"])

    def test_supplier_tender_read_contracts(self):
        prefix = "/api/v1/catalog/suppliers/{supplier_id}/tenders/{tender_id}"
        items = self.operation(f"{prefix}/items", "get")
        autofill = self.operation(f"{prefix}/offer-autofill", "get")

        self.assert_operation(items, "supplier_tender_item_list", statuses={200, 401, 403, 404})
        self.assert_array_of(self.response_schema(items, 200), "TenderItemForSupplier")

        self.assert_operation(autofill, "supplier_tender_offer_autofill", statuses={200, 401, 403, 404})
        self.assert_array_of(self.response_schema(autofill, 200), "TenderOfferAutofillSuggestion")

    def test_tender_distribution_contract(self):
        path = "/api/v1/catalog/tenders/{tender_id}/items/{item_id}/offers/{offer_id}/distribution"
        preview = self.operation(path, "get")
        apply = self.operation(path, "post")

        self.assert_operation(preview, "tender_distribution_preview", statuses={200, 400, 401, 403, 404})
        parameters = {parameter["name"]: parameter for parameter in preview["parameters"]}
        self.assertIn("venue_id", parameters)
        venue_id = parameters["venue_id"]
        self.assertTrue(venue_id["required"])
        self.assertEqual(venue_id["schema"]["type"], "array")
        self.assertEqual(venue_id["schema"]["items"]["type"], "string")
        self.assertEqual(venue_id["schema"]["items"]["format"], "uuid")
        self.assert_array_of(self.response_schema(preview, 200), "MasterProductTenderOfferRow")

        self.assert_operation(apply, "tender_distribution_apply", statuses={200, 400, 401, 403, 404})
        self.assertEqual(self.component_name(self.request_schema(apply)), "TenderDistributionRequest")
        self.assert_array_of(self.response_schema(apply, 200), "MasterProductTenderOfferRow")

    def test_tender_item_offers_contract(self):
        path = "/api/v1/catalog/tenders/{tender_id}/items/{item_id}/offers"
        operation = self.operation(path, "get")

        self.assert_operation(operation, "tender_item_offers_list", statuses={200, 401, 403, 404})
        self.assert_array_of(self.response_schema(operation, 200), "TenderOffer")

    def test_venue_prices_contract(self):
        path = "/api/v1/catalog/suppliers/{supplier_id}/tenders/{tender_id}/items/{item_id}/venue-prices"
        operation = self.operation(path, "get")

        self.assert_operation(operation, "supplier_tender_item_venue_prices", statuses={200, 400, 401, 403, 404})
        self.assert_array_of(self.response_schema(operation, 200), "TenderVenuePrice")
        suggestion = self.schema["components"]["schemas"]["TenderOfferAutofillSuggestion"]
        self.assertIn("venue_prices", suggestion["required"])
        self.assert_array_of(suggestion["properties"]["venue_prices"], "TenderVenuePrice")
        price_source = self.schema["components"]["schemas"]["TenderVenuePrice"]["properties"]["price_source"]
        self.assertIn({"$ref": "#/components/schemas/PriceSourceEnum"}, price_source["oneOf"])

    def test_tender_history_contract(self):
        operation = self.operation("/api/v1/catalog/tenders/{tender_id}/history", "get")

        self.assert_operation(operation, "tender_history", statuses={200, 401, 403, 404})
        self.assert_array_of(self.response_schema(operation, 200), "TenderChange")
        change = self.schema["components"]["schemas"]["TenderChange"]
        self.assertEqual(self.component_name(change["properties"]["change_type"]), "ChangeTypeEnum")
        self.assertIn({"$ref": "#/components/schemas/ChangedFieldEnum"}, change["properties"]["changed_field"]["oneOf"])

    def test_tender_files_contract(self):
        create = self.operation("/api/v1/catalog/tenders/{tender_id}/files", "post")
        delete = self.operation("/api/v1/catalog/tenders/{tender_id}/files/{file_id}", "delete")

        self.assert_operation(create, "tender_file_create", statuses={201, 400, 401, 403, 404})
        self.assertIn("multipart/form-data", create["requestBody"]["content"])
        self.assertEqual(self.component_name(self.response_schema(create, 201)), "TenderFile")
        self.assert_operation(delete, "tender_file_delete", statuses={204, 400, 401, 403, 404})
        for component in ("Tender", "SupplierTender"):
            with self.subTest(component=component):
                schema = self.schema["components"]["schemas"][component]
                self.assertIn("files", schema["required"])
                self.assert_array_of(schema["properties"]["files"], "TenderFile")

    def test_tender_number_contract(self):
        schemas = self.schema["components"]["schemas"]
        for component, field in (
            ("Tender", "number"),
            ("SupplierTender", "number"),
            ("TenderActiveConflictEntry", "tender_number"),
        ):
            with self.subTest(component=component):
                self.assertIn(field, schemas[component]["required"])
                self.assertEqual(schemas[component]["properties"][field]["type"], "integer")
        busy = schemas["TenderVenueAvailabilityVenue"]["properties"]["busy_tender_number"]
        self.assertTrue(busy.get("nullable"))

    def test_supplier_tender_history_contract(self):
        path = "/api/v1/catalog/suppliers/{supplier_id}/tenders/{tender_id}/history"
        operation = self.operation(path, "get")

        self.assert_operation(operation, "supplier_tender_history", statuses={200, 401, 403, 404})
        self.assert_array_of(self.response_schema(operation, 200), "TenderChange")

    def test_tender_supplier_search_contract(self):
        operation = self.operation("/api/v1/catalog/tenders/supplier-search", "get")

        self.assert_operation(operation, "tender_supplier_search", statuses={200, 400, 401})
        self.assert_array_of(self.response_schema(operation, 200), "CatalogSupplier")
        self.assertIn("q", {parameter["name"] for parameter in operation["parameters"]})

    def test_offer_and_distribution_history_contract(self):
        change = self.schema["components"]["schemas"]["TenderChange"]
        for field in ("tender_offer_id", "supplier_name"):
            with self.subTest(field=field):
                self.assertIn(field, change["required"])
                self.assertTrue(change["properties"][field].get("nullable"))
        change_types = self.schema["components"]["schemas"]["ChangeTypeEnum"]["enum"]
        self.assertTrue({"offer_submitted", "offer_updated", "offer_withdrawn", "offer_returned"} <= set(change_types))

        path = "/api/v1/catalog/tenders/{tender_id}/items/{item_id}/distributions"
        operation = self.operation(path, "get")
        self.assert_operation(operation, "tender_item_distribution_history", statuses={200, 401, 403, 404})
        self.assert_array_of(self.response_schema(operation, 200), "TenderDistributionHistory")

    def test_supplier_mine_tender_list_contract(self):
        operation = self.operation("/api/v1/catalog/suppliers/mine/tenders", "get")

        self.assert_operation(operation, "supplier_mine_tender_list", statuses={200, 400, 401})
        self.assertEqual(self.component_name(self.response_schema(operation, 200)), "SupplierTenderListResponse")
        row = self.schema["components"]["schemas"]["SupplierTender"]
        self.assertIn("companies", row["required"])
        self.assertTrue(row["properties"]["companies"].get("nullable"))
        self.assertEqual(self.component_name(row["properties"]["companies"]["items"]), "SupplierTenderCompany")

    def test_offers_window_contract(self):
        offer = self.schema["components"]["schemas"]["TenderOffer"]
        self.assertIn("price_per_unit", offer["required"])
        self.assertTrue(offer["properties"]["price_per_unit"].get("nullable"))

        path = "/api/v1/catalog/tenders/{tender_id}/items/{item_id}/current-products"
        operation = self.operation(path, "get")
        self.assert_operation(operation, "tender_item_current_products", statuses={200, 401, 403, 404})
        self.assert_array_of(self.response_schema(operation, 200), "TenderItemCurrentProduct")
        current = self.schema["components"]["schemas"]["TenderItemCurrentProduct"]
        self.assertIn("requested_name", current["required"])
        self.assertTrue(current["properties"]["requested_name"].get("nullable"))
        self.assertIn({"$ref": "#/components/schemas/PriceSourceEnum"}, current["properties"]["price_source"]["oneOf"])

    def test_tender_list_filters_contract(self):
        buyer = self.operation("/api/v1/catalog/tenders", "get")
        self.assertTrue({"owner_id", "q"} <= {parameter["name"] for parameter in buyer["parameters"]})
        for path in ("/api/v1/catalog/suppliers/mine/tenders", "/api/v1/catalog/suppliers/{supplier_id}/tenders"):
            with self.subTest(path=path):
                names = {parameter["name"] for parameter in self.operation(path, "get")["parameters"]}
                self.assertTrue({"status", "q", "my_work"} <= names)

        kinds = self.operation("/api/v1/catalog/tenders/product-kinds", "get")
        self.assert_operation(kinds, "tender_product_kind_list", statuses={200, 401})
        self.assert_array_of(self.response_schema(kinds, 200), "TenderProductKind")

    def test_tender_published_at_contract(self):
        for component in ("Tender", "SupplierTender"):
            with self.subTest(component=component):
                schema = self.schema["components"]["schemas"][component]
                self.assertIn("published_at", schema["required"])
                self.assertTrue(schema["properties"]["published_at"].get("nullable"))
