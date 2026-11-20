from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator


class OrdersOpenAPIContractTests(SimpleTestCase):
    tag = ["Заказы"]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def operation(self, path, method="get"):
        return self.schema["paths"][path][method]

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

    def response_schema(self, operation, status_code=200):
        return operation["responses"][str(status_code)]["content"]["application/json"]["schema"]

    def assert_list_contract(self, operation, operation_id, response_component, required_parameters):
        self.assertEqual(operation["operationId"], operation_id)
        self.assertEqual(operation["tags"], self.tag)
        parameters = {parameter["name"] for parameter in operation.get("parameters", [])}
        self.assertTrue(set(required_parameters).issubset(parameters))
        self.assertEqual(self.component_name(self.response_schema(operation)), response_component)
        page = self.resolve(self.response_schema(operation))
        self.assertTrue({"count", "limit", "offset", "results"}.issubset(page["properties"]))

    def test_venue_order_feed_contract(self):
        operation = self.operation("/api/v1/catalog/venues/{venue_id}/orders")
        self.assert_list_contract(
            operation,
            "venue_orders_list",
            "OrderPage",
            required_parameters={"status", "limit", "offset"},
        )

    def test_venue_mine_order_feed_contract(self):
        operation = self.operation("/api/v1/catalog/venues/mine/orders")
        self.assert_list_contract(
            operation,
            "venue_mine_orders_list",
            "VenueMineOrderPage",
            required_parameters={"status", "venue_id", "limit", "offset"},
        )
        self.assertIn("401", operation["responses"])

    def test_supplier_order_feed_contract(self):
        operation = self.operation("/api/v1/catalog/suppliers/{supplier_id}/orders")
        self.assert_list_contract(
            operation,
            "supplier_orders_list",
            "SupplierOrderRequestPage",
            required_parameters={"status", "venue_id", "limit", "offset"},
        )
        self.assertTrue({"401", "403", "404"}.issubset(operation["responses"]))

    def test_order_detail_contracts_use_different_response_models(self):
        venue = self.operation("/api/v1/catalog/venues/{venue_id}/orders/{order_id}")
        supplier = self.operation("/api/v1/catalog/suppliers/{supplier_id}/orders/{order_id}")

        self.assertEqual(venue["operationId"], "venue_order_retrieve")
        self.assertEqual(venue["tags"], self.tag)
        self.assertEqual(self.component_name(self.response_schema(venue)), "ApprovalOrderDetail")

        self.assertIn("items", self.resolve(self.response_schema(venue))["properties"])
        self.assertIn("items", self.resolve(self.response_schema(supplier))["properties"])

        self.assertEqual(supplier["operationId"], "supplier_order_retrieve")
        self.assertEqual(supplier["tags"], self.tag)
        self.assertEqual(self.component_name(self.response_schema(supplier)), "SupplierOrderRequestDetail")
        self.assertTrue({"401", "403", "404"}.issubset(supplier["responses"]))
