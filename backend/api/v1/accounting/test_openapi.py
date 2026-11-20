from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator

TAG = ["Интеграция учётных систем"]
STATUS_LIST = "/api/v1/accounting/status"
VENUE_STATUS = "/api/v1/accounting/venues/{venue_id}/status"
SYNC = "/api/v1/accounting/venues/{venue_id}/connections/{connection_id}/sync"
JOB = "/api/v1/accounting/venues/{venue_id}/sync-jobs/{job_id}"
STATUS_STATES = {
    "not_connected", "needs_department_link", "first_sync_pending", "active", "stale", "needs_attention",
    "disconnected",
}  # fmt: skip


class AccountingOpenAPIContractTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)
        cls.operations = {
            (path, method): operation
            for path, item in cls.schema["paths"].items()
            if path.startswith("/api/v1/accounting/")
            for method, operation in item.items()
        }

    def component(self, name):
        return self.schema["components"]["schemas"][name]

    def body_schema(self, operation, status_code):
        return operation["responses"][str(status_code)]["content"]["application/json"]["schema"]

    def ref_name(self, schema):
        if "$ref" in schema:
            return schema["$ref"].rsplit("/", 1)[-1]
        if schema.get("type") == "array":
            return self.ref_name(schema["items"])
        if len(schema.get("allOf", [])) == 1:
            return self.ref_name(schema["allOf"][0])
        return None

    def test_all_four_routes_are_published_with_the_tag(self):
        self.assertEqual(
            set(self.operations),
            {(STATUS_LIST, "get"), (VENUE_STATUS, "get"), (SYNC, "post"), (JOB, "get")},
        )
        for operation in self.operations.values():
            self.assertEqual(operation["tags"], TAG)

    def test_status_schema_declares_state_enum_and_required_fields(self):
        status = self.component("VenueIntegrationStatus")
        required = {
            "venue_id", "state", "reason_code", "connection_id", "provider", "last_success_at", "stale_since",
            "warehouses_count", "can_manage",
        }  # fmt: skip
        self.assertEqual(set(status["required"]), required)
        self.assertEqual(set(status["properties"]), required)
        state = status["properties"]["state"]
        state = self.component(self.ref_name(state)) if self.ref_name(state) else state
        self.assertEqual(set(state["enum"]), STATUS_STATES)
        self.assertEqual(status["properties"]["venue_id"]["format"], "uuid")
        self.assertEqual(status["properties"]["last_success_at"].get("format"), "date-time")
        for nullable in ("reason_code", "connection_id", "provider", "last_success_at", "stale_since"):
            self.assertTrue(status["properties"][nullable].get("nullable") or "anyOf" in status["properties"][nullable])
        for name, prop in status["properties"].items():
            self.assertTrue(prop.get("readOnly"), name)
            self.assertTrue(prop.get("description"), name)

    def test_sync_job_schema_has_enums_and_required_fields(self):
        job = self.component("SyncJob")
        self.assertEqual(
            set(job["required"]),
            {
                "job_id",
                "connection_id",
                "kind",
                "trigger",
                "status",
                "error_code",
                "queued_at",
                "started_at",
                "finished_at",
            },
        )
        for field, expected in (
            ("kind", {"catalog", "stock", "consumption"}),
            ("trigger", {"schedule", "manual", "connect"}),
            ("status", {"queued", "running", "success", "partial", "failed", "skipped"}),
        ):
            prop = job["properties"][field]
            prop = self.component(self.ref_name(prop)) if self.ref_name(prop) else prop
            self.assertEqual(set(prop["enum"]), expected, field)

    def test_operation_ids_are_unique(self):
        ids = [operation["operationId"] for operation in self.operations.values()]
        self.assertEqual(len(ids), len(set(ids)))
        all_ids = [op["operationId"] for item in self.schema["paths"].values() for op in item.values()]
        self.assertTrue(set(ids).isdisjoint(set(all_ids) - set(ids)))

    def test_every_documented_error_code_has_a_body_schema(self):
        documented = {
            (VENUE_STATUS, "get"): {"200", "401", "403", "404"},
            (SYNC, "post"): {"202", "401", "403", "404", "409", "429"},
            (JOB, "get"): {"200", "401", "403", "404"},
            (STATUS_LIST, "get"): {"200", "401"},
        }
        for key, codes in documented.items():
            operation = self.operations[key]
            self.assertEqual(set(operation["responses"]), codes, key)
            for code in codes:
                self.assertIn(self.ref_name(self.body_schema(operation, code)), self.schema["components"]["schemas"])
        self.assertEqual(self.ref_name(self.body_schema(self.operations[(SYNC, "post")], 429)), "ThrottledError")
        self.assertIn("retry_after", self.component("ThrottledError")["properties"])
        self.assertEqual(
            self.ref_name(self.body_schema(self.operations[(STATUS_LIST, "get")], 200)), "VenueIntegrationStatus"
        )
        self.assertEqual(self.body_schema(self.operations[(STATUS_LIST, "get")], 200)["type"], "array")

    def test_path_parameters_are_uuids_with_descriptions(self):
        for (path, _), operation in self.operations.items():
            for parameter in operation.get("parameters", []):
                self.assertEqual(parameter["schema"]["format"], "uuid", (path, parameter["name"]))
                self.assertTrue(parameter.get("required"))
                self.assertTrue(parameter.get("description"), (path, parameter["name"]))
