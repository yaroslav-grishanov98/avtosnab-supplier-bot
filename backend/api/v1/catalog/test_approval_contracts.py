from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator


class ApprovalContractTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def resolve(self, schema):
        while "$ref" in schema:
            schema = self.schema["components"]["schemas"][schema["$ref"].split("/")[-1]]
        return schema

    def request(self, path, method="post"):
        return self.resolve(self.schema["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"])

    def test_submit_requires_both_fields(self):
        request = self.request("/api/v1/catalog/venues/{venue_id}/cart/submit")
        self.assertTrue({"command_id", "expected_settings_version"}.issubset(request["required"]))

    def test_command_fields_and_save_changes(self):
        base = "/api/v1/catalog/venues/{venue_id}/orders/{order_id}/approval"
        for op in ["approve", "save-and-approve", "return-for-rework", "resubmit", "cancel"]:
            request = self.request(base + "/" + op)
            self.assertTrue({"command_id", "expected_revision", "expected_cycle_id"}.issubset(request["required"]))
            self.assertNotIn("comment", request["required"])
            self.assertIn("409", self.schema["paths"][base + "/" + op]["post"]["responses"])
        request = self.request(base + "/save-and-approve")
        changes = self.resolve(request["properties"]["changes"])
        items = self.resolve(changes["properties"]["items"])
        self.assertTrue({"add", "update", "remove"}.issubset(items["properties"]))

    def test_shared_approval_summary_and_filters(self):
        detail = self.schema["components"]["schemas"]["VenueOrderDetail"]
        self.assertTrue({"revision", "author", "business_updated_at", "approval"}.issubset(detail["properties"]))
        params = self.schema["paths"]["/api/v1/catalog/venues/mine/approvals"]["get"]["parameters"]
        status = next(x["schema"] for x in params if x["name"] == "filter")
        self.assertEqual(set(self.resolve(status)["enum"]), {"pending", "rework", "completed", "all"})

    def response(self, path, method="get"):
        return self.resolve(
            self.schema["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        )

    def test_list_has_only_summary_fields(self):
        page = self.response("/api/v1/catalog/venues/mine/approvals")
        row = self.resolve(page["properties"]["results"]["items"])
        self.assertEqual(
            set(row["properties"]),
            {
                "id",
                "order_number",
                "venue_id",
                "venue_name",
                "author",
                "total_amount",
                "currency",
                "has_complete_prices",
                "created_at",
                "business_updated_at",
                "approval",
            },
        )

    def test_public_detail_and_commands_have_flat_and_grouped_items(self):
        base = "/api/v1/catalog/venues/{venue_id}"
        order = base + "/orders/{order_id}"
        routes = [(order, "get"), (base + "/cart/submit", "post"), (order + "/rework", "patch")]
        routes += [
            (order + "/approval/" + op, "post")
            for op in ["approve", "save-and-approve", "return-for-rework", "resubmit", "cancel"]
        ]
        for path, method in routes:
            with self.subTest(path=path):
                detail = self.response(path, method)
                self.assertIn("items", detail["properties"])
                self.assertIn("venue_name", detail["properties"])
                group = self.resolve(detail["properties"]["supplier_groups"]["items"])
                self.assertIn("items", group["properties"])
