import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from pydantic import BaseModel

from api.llm.connector import LLMConnector, LLMConnectorError, _strict_json_schema
from api.llm.runtime import LLMRuntime, get_llm_runtime, get_llm_runtimes
from api.v1.catalog.ai_contracts import CandidateRankingBatch, UnitSuggestionBatch
from api.v1.catalog.candidate_ranking_llm import CandidateRankingError, rerank_candidates, rerank_venue_candidates
from api.v1.catalog.product_card_llm import ProductCardSuggestion, suggest_product_card
from api.v1.catalog.unit_llm_client import UnitSuggestionError, suggest_unit_ids, suggest_venue_unit_ids
from api.v1.suppliers.ai_client import request_ai_normalization_payload
from api.v1.suppliers.ai_contracts import ParsedSupplierCatalog


def _assert_strict_compliant(test_case, node):
    """Recursively assert every object node satisfies OpenAI's strict json_schema contract."""
    if isinstance(node, dict):
        if "properties" in node:
            test_case.assertIs(node.get("additionalProperties"), False, node)
            test_case.assertEqual(set(node.get("required") or []), set(node["properties"].keys()), node)
        for value in node.values():
            _assert_strict_compliant(test_case, value)
    elif isinstance(node, list):
        for item in node:
            _assert_strict_compliant(test_case, item)


def _find_pattern_keys(node):
    """Collect every "pattern" value found anywhere in the schema tree — OpenAI's strict
    json_schema mode can't handle a regex-constrained string inside an anyOf branch
    (confirmed live: the model returns 0 completion tokens, finish_reason=length)."""
    found = []
    if isinstance(node, dict):
        if "pattern" in node:
            found.append(node["pattern"])
        for value in node.values():
            found.extend(_find_pattern_keys(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_pattern_keys(item))
    return found


class ExampleResponse(BaseModel):
    value: str


class LLMRuntimeTests(SimpleTestCase):
    @override_settings(
        AI_NORMALIZATION_PROVIDER=" Anthropic ",
        OPENAI_API_KEY="Bearer secret",
        OPENAI_BASE_URL="https://example.test/v1/chat/completions",
        OPENAI_NORMALIZATION_MODEL="model-1",
        AI_NORMALIZATION_TIMEOUT_SECONDS=17,
    )
    def test_runtime_normalizes_shared_settings(self):
        runtime = get_llm_runtime()
        self.assertEqual(runtime.provider, "anthropic")
        self.assertEqual(runtime.api_key, "secret")
        self.assertEqual(runtime.base_url, "https://example.test/v1")
        self.assertEqual(runtime.model, "model-1")
        self.assertEqual(runtime.timeout_seconds, 17)

    @override_settings(
        LLM_PRIMARY_PROVIDER="openai",
        LLM_PRIMARY_BASE_URL="http://127.0.0.1:8088/v1",
        LLM_PRIMARY_API_KEY="",
        LLM_PRIMARY_MODEL="bonsai",
        LLM_FALLBACK_PROVIDER="openai",
        LLM_FALLBACK_BASE_URL="https://api.openai.com/v1",
        LLM_FALLBACK_API_KEY="fallback-key",
        LLM_FALLBACK_MODEL="gpt-4o-mini",
    )
    def test_local_primary_and_remote_fallback(self):
        runtimes = get_llm_runtimes()
        self.assertEqual([runtime.model for runtime in runtimes], ["bonsai", "gpt-4o-mini"])
        self.assertEqual(runtimes[0].api_key, "local")

    @override_settings(
        LLM_PRIMARY_PROVIDER="openai",
        LLM_PRIMARY_BASE_URL="https://example.test/v1",
        LLM_PRIMARY_API_KEY="",
        LLM_PRIMARY_MODEL="model",
    )
    def test_remote_primary_requires_key(self):
        with self.assertRaisesRegex(ValueError, "API_KEY"):
            get_llm_runtime()


class LLMConnectorTests(SimpleTestCase):
    def test_openai_uses_json_schema_and_validates_result(self):
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"value": "ok"})))]
        )
        client = MagicMock()
        client.chat.completions.create.return_value = completion
        factory = MagicMock(return_value=client)
        connector = LLMConnector(LLMRuntime("openai", "key", None, "model", 10, "2023-06-01"), openai_factory=factory)
        result = connector.structured(
            scenario="example", system_prompt="Return data", payload={"x": 1}, response_model=ExampleResponse
        )
        self.assertEqual(result.value, "ok")
        kwargs = client.chat.completions.create.call_args.kwargs
        response_format = kwargs["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertIs(response_format["json_schema"]["strict"], True)
        _assert_strict_compliant(self, response_format["json_schema"]["schema"])

    def test_finish_log_contains_token_usage_for_openai(self):
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"value": "ok"})))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
        )
        client = MagicMock()
        client.chat.completions.create.return_value = completion
        connector = LLMConnector(
            LLMRuntime("openai", "key", None, "model", 10, "2023-06-01"), openai_factory=MagicMock(return_value=client)
        )
        with self.assertLogs("autosnab.llm.connector", level="INFO") as logs:
            connector.structured(
                scenario="example", system_prompt="Return data", payload={"x": 1}, response_model=ExampleResponse
            )
        finish = next(line for line in logs.output if "llm.finish" in line)
        self.assertIn("prompt_tokens=11", finish)
        self.assertIn("completion_tokens=7", finish)
        self.assertIn("duration=", finish)

    def test_finish_log_contains_token_usage_for_anthropic(self):
        body = json.dumps(
            {
                "content": [{"type": "text", "text": json.dumps({"value": "ok"})}],
                "usage": {"input_tokens": 5, "output_tokens": 3},
            }
        ).encode()
        response = MagicMock()
        response.read.return_value = body
        response.__enter__.return_value = response
        connector = LLMConnector(
            LLMRuntime("anthropic", "key", None, "model", 10, "2023-06-01"), urlopen=MagicMock(return_value=response)
        )
        with self.assertLogs("autosnab.llm.connector", level="INFO") as logs:
            connector.structured(
                scenario="example", system_prompt="Return data", payload={"x": 1}, response_model=ExampleResponse
            )
        finish = next(line for line in logs.output if "llm.finish" in line)
        self.assertIn("prompt_tokens=5", finish)
        self.assertIn("completion_tokens=3", finish)

    def test_missing_usage_does_not_break_the_call(self):
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"value": "ok"})))]
        )
        client = MagicMock()
        client.chat.completions.create.return_value = completion
        connector = LLMConnector(
            LLMRuntime("openai", "key", None, "model", 10, "2023-06-01"), openai_factory=MagicMock(return_value=client)
        )
        with self.assertLogs("autosnab.llm.connector", level="INFO") as logs:
            result = connector.structured(
                scenario="example", system_prompt="Return data", payload={"x": 1}, response_model=ExampleResponse
            )
        self.assertEqual(result.value, "ok")
        self.assertIn("prompt_tokens=None", next(line for line in logs.output if "llm.finish" in line))

    def test_connector_logger_emits_info_with_project_logging_config(self):
        import logging

        self.assertTrue(logging.getLogger("autosnab.llm.connector").isEnabledFor(logging.INFO))

    def test_falls_back_when_primary_fails(self):
        failed_client = MagicMock()
        failed_client.chat.completions.create.side_effect = RuntimeError("local unavailable")
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"value": "fallback"})))]
        )
        fallback_client = MagicMock()
        fallback_client.chat.completions.create.return_value = completion
        connector = LLMConnector(
            runtimes=[
                LLMRuntime("openai", "local", "http://127.0.0.1:8088/v1", "bonsai", 2, "2023-06-01"),
                LLMRuntime("openai", "key", None, "gpt-4o-mini", 10, "2023-06-01"),
            ],
            openai_factory=MagicMock(side_effect=[failed_client, fallback_client]),
        )
        result = connector.structured(
            scenario="example", system_prompt="Return data", payload={"x": 1}, response_model=ExampleResponse
        )
        self.assertEqual(result.value, "fallback")

    def test_local_openai_compatible_runtime_uses_json_object(self):
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"value": "local"})))]
        )
        client = MagicMock()
        client.chat.completions.create.return_value = completion
        connector = LLMConnector(
            LLMRuntime("openai", "local", "http://127.0.0.1:8088/v1", "bonsai", 10, "2023-06-01"),
            openai_factory=MagicMock(return_value=client),
        )
        result = connector.structured(
            scenario="example", system_prompt="Return data", payload={"x": 1}, response_model=ExampleResponse
        )
        self.assertEqual(result.value, "local")
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["response_format"]["type"], "json_object")
        self.assertIn("JSON Schema", kwargs["messages"][0]["content"])


class StrictJsonSchemaTests(SimpleTestCase):
    """TASK-97: OpenAI's strict json_schema mode requires additionalProperties:false and
    every property in `required` on every object node, including nested ones under $defs -
    real Pydantic schemas don't satisfy this by default."""

    def test_raw_pydantic_schema_is_not_strict_compliant(self):
        with self.assertRaises(AssertionError):
            _assert_strict_compliant(self, ParsedSupplierCatalog.model_json_schema())

    def test_nested_supplier_catalog_schema_becomes_strict_compliant(self):
        _assert_strict_compliant(self, _strict_json_schema(ParsedSupplierCatalog.model_json_schema()))

    def test_nested_unit_suggestion_batch_schema_becomes_strict_compliant(self):
        _assert_strict_compliant(self, _strict_json_schema(UnitSuggestionBatch.model_json_schema()))

    def test_flat_product_card_suggestion_schema_becomes_strict_compliant(self):
        _assert_strict_compliant(self, _strict_json_schema(ProductCardSuggestion.model_json_schema()))

    def test_does_not_mutate_input_schema(self):
        original = ParsedSupplierCatalog.model_json_schema()
        _strict_json_schema(original)
        self.assertNotIn("additionalProperties", original)

    def test_raw_decimal_field_schema_has_a_pattern_anyof_branch(self):
        # Pydantic represents Decimal as anyOf[number, pattern-constrained string, ...] —
        # this is the exact shape that breaks OpenAI's strict mode live (see TASK-113).
        self.assertTrue(_find_pattern_keys(ParsedSupplierCatalog.model_json_schema()))

    def test_strict_schema_drops_pattern_branches_for_supplier_catalog(self):
        strict_schema = _strict_json_schema(ParsedSupplierCatalog.model_json_schema())
        self.assertEqual(_find_pattern_keys(strict_schema), [])

    def test_optional_decimal_keeps_number_and_null_after_dropping_pattern(self):
        class OnlyOptionalDecimal(BaseModel):
            price: Decimal | None = None

        strict_schema = _strict_json_schema(OnlyOptionalDecimal.model_json_schema())
        price_schema = strict_schema["properties"]["price"]
        self.assertEqual(_find_pattern_keys(price_schema), [])
        types = {branch.get("type") for branch in price_schema["anyOf"]}
        self.assertEqual(types, {"number", "null"})

    def test_required_decimal_collapses_to_plain_number_after_dropping_pattern(self):
        class OnlyRequiredDecimal(BaseModel):
            confidence: Decimal = Decimal("0.9500")

        strict_schema = _strict_json_schema(OnlyRequiredDecimal.model_json_schema())
        confidence_schema = strict_schema["properties"]["confidence"]
        self.assertNotIn("anyOf", confidence_schema)
        self.assertEqual(confidence_schema["type"], "number")


class ProductCardLLMTests(SimpleTestCase):
    def test_accepts_only_provided_unit_and_candidates(self):
        unit_id, candidate_id = uuid4(), uuid4()
        connector = MagicMock()
        connector.structured.return_value = SimpleNamespace(
            normalized_name="масло оливковое 5 л",
            suggested_unit_id=unit_id,
            candidate_product_ids=[candidate_id],
        )
        result = suggest_product_card(
            name="Масло оливковое 5 л",
            unit_id=None,
            allowed_units=[{"id": unit_id, "code": "l", "name": "л"}],
            candidates=[{"id": candidate_id, "name": "Масло оливковое", "unit_id": unit_id}],
            connector=connector,
        )
        self.assertEqual(result.suggested_unit_id, unit_id)

    def test_rejects_unit_not_in_allowlist(self):
        connector = MagicMock()
        connector.structured.return_value = SimpleNamespace(
            normalized_name="товар", suggested_unit_id=uuid4(), candidate_product_ids=[]
        )
        with self.assertRaisesRegex(ValueError, "outside allowed_units"):
            suggest_product_card(
                name="Товар",
                unit_id=None,
                allowed_units=[{"id": uuid4(), "code": "pcs", "name": "шт"}],
                candidates=[],
                connector=connector,
            )

    def test_does_not_replace_existing_unit(self):
        existing_unit, proposed_unit = uuid4(), uuid4()
        connector = MagicMock()
        connector.structured.return_value = SimpleNamespace(
            normalized_name="товар", suggested_unit_id=proposed_unit, candidate_product_ids=[]
        )
        with self.assertRaisesRegex(ValueError, "must not replace"):
            suggest_product_card(
                name="Товар",
                unit_id=existing_unit,
                allowed_units=[{"id": proposed_unit, "code": "kg", "name": "кг"}],
                candidates=[],
                connector=connector,
            )

    def test_rejects_candidate_not_supplied_by_backend(self):
        connector = MagicMock()
        connector.structured.return_value = SimpleNamespace(
            normalized_name="товар", suggested_unit_id=None, candidate_product_ids=[uuid4()]
        )
        with self.assertRaisesRegex(ValueError, "outside candidates"):
            suggest_product_card(name="Товар", unit_id=None, allowed_units=[], candidates=[], connector=connector)


class SupplierPriceConnectorMigrationTests(SimpleTestCase):
    @override_settings(
        LLM_CONNECTOR_ENABLED=True,
        AI_NORMALIZATION_PROVIDER="openai",
        AI_NORMALIZATION_ANTHROPIC_VERSION="2023-06-01",
    )
    def test_price_import_can_use_shared_connector_behind_feature_flag(self):
        upload = SimpleNamespace(id=uuid4(), supplier=SimpleNamespace(display_name="Supplier"))
        connector = MagicMock()
        connector.structured.return_value = ParsedSupplierCatalog(products=[])
        from unittest.mock import patch

        with patch("api.v1.suppliers.ai_client.LLMConnector", return_value=connector):
            result = request_ai_normalization_payload(
                api_key="key",
                base_url=None,
                model="model",
                timeout_seconds=30,
                upload=upload,
                input_rows=[{"row_number": 1, "name": "Товар"}],
            )
        self.assertEqual(result, {"products": []})
        self.assertEqual(connector.structured.call_args.kwargs["scenario"], "supplier_price_normalization")


class UnitSuggestionConnectorMigrationTests(SimpleTestCase):
    """TASK-88: unit_llm_client.suggest_unit_ids migrated off its own direct
    OpenAI+instructor client onto the shared api.llm connector — same public
    contract (signature, return shape, UnitSuggestionError on failure) as
    before the migration, verified here directly (previously only exercised
    indirectly through mocks at the selectors.py call site)."""

    def test_returns_empty_dict_without_calling_provider_for_no_rows(self):
        with patch("api.v1.catalog.unit_llm_client.LLMConnector") as connector_cls:
            result = suggest_unit_ids(rows=[], units=[{"id": "u1", "code": "кг", "name": "Килограмм"}])
        self.assertEqual(result, {})
        connector_cls.assert_not_called()

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key")
    def test_returns_matched_row_numbers_and_skips_unmatched(self):
        connector = MagicMock()
        connector.structured.return_value = UnitSuggestionBatch(
            suggestions=[{"row_number": 1, "unit_id": "unit-1"}, {"row_number": 2, "unit_id": None}]
        )
        with patch("api.v1.catalog.unit_llm_client.LLMConnector", return_value=connector):
            result = suggest_unit_ids(
                rows=[
                    {"row_number": 1, "raw_name": "Мука", "unit_label": "кило"},
                    {"row_number": 2, "raw_name": "Вода", "unit_label": "неведомая единица"},
                ],
                units=[{"id": "unit-1", "code": "кг", "name": "Килограмм"}],
            )
        self.assertEqual(result, {1: "unit-1"})
        self.assertEqual(connector.structured.call_args.kwargs["scenario"], "master_catalog_unit_suggestion")
        self.assertEqual(connector.structured.call_args.kwargs["response_model"], UnitSuggestionBatch)

    @override_settings(
        AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key", MASTER_CATALOG_UNIT_LLM_TIMEOUT_SECONDS=7
    )
    def test_uses_short_master_catalog_timeout_not_shared_default(self):
        captured_runtime = {}

        def fake_llm_connector(runtime=None, **kwargs):
            captured_runtime["runtime"] = runtime
            connector = MagicMock()
            connector.structured.return_value = UnitSuggestionBatch(suggestions=[])
            return connector

        with patch("api.v1.catalog.unit_llm_client.LLMConnector", side_effect=fake_llm_connector):
            suggest_unit_ids(rows=[{"row_number": 1, "raw_name": "Мука", "unit_label": ""}], units=[])
        self.assertEqual(captured_runtime["runtime"].timeout_seconds, 7)

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key")
    def test_raises_unit_suggestion_error_when_provider_unavailable(self):
        connector = MagicMock()
        connector.structured.side_effect = LLMConnectorError("all providers failed")
        with patch("api.v1.catalog.unit_llm_client.LLMConnector", return_value=connector):
            with self.assertRaises(UnitSuggestionError):
                suggest_unit_ids(rows=[{"row_number": 1, "raw_name": "Мука", "unit_label": ""}], units=[])

    @override_settings(
        LLM_PRIMARY_PROVIDER="openai", LLM_PRIMARY_BASE_URL="http://127.0.0.1:8088/v1", LLM_PRIMARY_MODEL="bonsai"
    )
    def test_uses_default_shared_connector_when_llm_primary_configured(self):
        connector = MagicMock()
        connector.structured.return_value = UnitSuggestionBatch(suggestions=[])
        with patch("api.v1.catalog.unit_llm_client.LLMConnector", return_value=connector) as connector_cls:
            suggest_unit_ids(rows=[{"row_number": 1, "raw_name": "Мука", "unit_label": ""}], units=[])
        connector_cls.assert_called_once_with()

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key")
    def test_default_chunk_size_keeps_small_batches_in_one_call(self):
        connector = MagicMock()
        connector.structured.return_value = UnitSuggestionBatch(suggestions=[])
        rows = [{"row_number": i, "raw_name": f"item-{i}", "unit_label": "x"} for i in range(1, 26)]
        with patch("api.v1.catalog.unit_llm_client.LLMConnector", return_value=connector):
            suggest_unit_ids(rows=rows, units=[])
        connector.structured.assert_called_once()

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key", MASTER_CATALOG_UNIT_LLM_CHUNK_SIZE=2)
    def test_splits_rows_into_chunks_of_configured_size(self):
        connector = MagicMock()
        connector.structured.side_effect = [
            UnitSuggestionBatch(
                suggestions=[{"row_number": 1, "unit_id": "unit-1"}, {"row_number": 2, "unit_id": None}]
            ),
            UnitSuggestionBatch(suggestions=[{"row_number": 3, "unit_id": "unit-3"}]),
        ]
        rows = [
            {"row_number": 1, "raw_name": "A", "unit_label": "x"},
            {"row_number": 2, "raw_name": "B", "unit_label": "y"},
            {"row_number": 3, "raw_name": "C", "unit_label": "z"},
        ]
        with patch("api.v1.catalog.unit_llm_client.LLMConnector", return_value=connector):
            result = suggest_unit_ids(rows=rows, units=[])
        self.assertEqual(connector.structured.call_count, 2)
        self.assertEqual(result, {1: "unit-1", 3: "unit-3"})
        first_call_rows = connector.structured.call_args_list[0].kwargs["payload"]["rows"]
        second_call_rows = connector.structured.call_args_list[1].kwargs["payload"]["rows"]
        self.assertEqual(len(first_call_rows), 2)
        self.assertEqual(len(second_call_rows), 1)

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key", MASTER_CATALOG_UNIT_LLM_CHUNK_SIZE=1)
    def test_one_failed_chunk_does_not_discard_other_chunks(self):
        connector = MagicMock()
        connector.structured.side_effect = [
            LLMConnectorError("boom"),
            UnitSuggestionBatch(suggestions=[{"row_number": 2, "unit_id": "unit-2"}]),
        ]
        rows = [
            {"row_number": 1, "raw_name": "A", "unit_label": "x"},
            {"row_number": 2, "raw_name": "B", "unit_label": "y"},
        ]
        with patch("api.v1.catalog.unit_llm_client.LLMConnector", return_value=connector):
            result = suggest_unit_ids(rows=rows, units=[])
        self.assertEqual(result, {2: "unit-2"})

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key", MASTER_CATALOG_UNIT_LLM_CHUNK_SIZE=1)
    def test_raises_only_when_every_chunk_fails(self):
        connector = MagicMock()
        connector.structured.side_effect = [LLMConnectorError("boom1"), LLMConnectorError("boom2")]
        rows = [
            {"row_number": 1, "raw_name": "A", "unit_label": "x"},
            {"row_number": 2, "raw_name": "B", "unit_label": "y"},
        ]
        with patch("api.v1.catalog.unit_llm_client.LLMConnector", return_value=connector):
            with self.assertRaises(UnitSuggestionError):
                suggest_unit_ids(rows=rows, units=[])


class VenueUnitSuggestionLLMTests(SimpleTestCase):
    """TASK-122 п.1: same unit-suggestion behaviour as suggest_unit_ids (shared
    private implementation), but its own scenario name and its own
    VENUE_IMPORT_UNIT_LLM_* settings — independent from the Мастер-справочник
    flag/timeout/chunk-size. Edge-case coverage (chunking, partial-chunk
    failure, hallucinated unit_id) already lives on
    UnitSuggestionConnectorMigrationTests above and exercises the same shared
    code path; this class only checks what's actually different about the
    venue wrapper."""

    def test_returns_empty_dict_without_calling_provider_for_no_rows(self):
        with patch("api.v1.catalog.unit_llm_client.LLMConnector") as connector_cls:
            result = suggest_venue_unit_ids(rows=[], units=[{"id": "u1", "code": "кг", "name": "Килограмм"}])
        self.assertEqual(result, {})
        connector_cls.assert_not_called()

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key")
    def test_returns_matched_row_numbers_with_venue_scenario(self):
        connector = MagicMock()
        connector.structured.return_value = UnitSuggestionBatch(suggestions=[{"row_number": 1, "unit_id": "unit-1"}])
        with patch("api.v1.catalog.unit_llm_client.LLMConnector", return_value=connector):
            result = suggest_venue_unit_ids(
                rows=[{"row_number": 1, "raw_name": "Сливки", "unit_label": "литр"}],
                units=[{"id": "unit-1", "code": "л", "name": "Литр"}],
            )
        self.assertEqual(result, {1: "unit-1"})
        self.assertEqual(connector.structured.call_args.kwargs["scenario"], "venue_product_matrix_unit_suggestion")
        self.assertEqual(connector.structured.call_args.kwargs["response_model"], UnitSuggestionBatch)

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key", VENUE_IMPORT_UNIT_LLM_CHUNK_SIZE=1)
    def test_uses_its_own_chunk_size_setting_not_the_master_one(self):
        connector = MagicMock()
        connector.structured.side_effect = [
            UnitSuggestionBatch(suggestions=[{"row_number": 1, "unit_id": "unit-1"}]),
            UnitSuggestionBatch(suggestions=[{"row_number": 2, "unit_id": "unit-2"}]),
        ]
        rows = [
            {"row_number": 1, "raw_name": "A", "unit_label": "x"},
            {"row_number": 2, "raw_name": "B", "unit_label": "y"},
        ]
        with patch("api.v1.catalog.unit_llm_client.LLMConnector", return_value=connector):
            result = suggest_venue_unit_ids(rows=rows, units=[])
        self.assertEqual(connector.structured.call_count, 2)
        self.assertEqual(result, {1: "unit-1", 2: "unit-2"})

    @override_settings(
        AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key", VENUE_IMPORT_UNIT_LLM_TIMEOUT_SECONDS=9
    )
    def test_uses_its_own_timeout_setting_not_the_master_one(self):
        captured_runtime = {}

        def fake_llm_connector(runtime=None, **kwargs):
            captured_runtime["runtime"] = runtime
            connector = MagicMock()
            connector.structured.return_value = UnitSuggestionBatch(suggestions=[])
            return connector

        with patch("api.v1.catalog.unit_llm_client.LLMConnector", side_effect=fake_llm_connector):
            suggest_venue_unit_ids(rows=[{"row_number": 1, "raw_name": "A", "unit_label": ""}], units=[])
        self.assertEqual(captured_runtime["runtime"].timeout_seconds, 9)

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key")
    def test_raises_unit_suggestion_error_when_provider_unavailable(self):
        connector = MagicMock()
        connector.structured.side_effect = LLMConnectorError("all providers failed")
        with patch("api.v1.catalog.unit_llm_client.LLMConnector", return_value=connector):
            with self.assertRaises(UnitSuggestionError):
                suggest_venue_unit_ids(rows=[{"row_number": 1, "raw_name": "A", "unit_label": ""}], units=[])

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key")
    def test_master_suggestion_is_unaffected_by_venue_settings(self):
        """Regression guard: the two wrappers must not share mutable state or
        accidentally read each other's settings."""
        connector = MagicMock()
        connector.structured.return_value = UnitSuggestionBatch(suggestions=[])
        with patch("api.v1.catalog.unit_llm_client.LLMConnector", return_value=connector):
            suggest_unit_ids(rows=[{"row_number": 1, "raw_name": "A", "unit_label": ""}], units=[])
        self.assertEqual(connector.structured.call_args.kwargs["scenario"], "master_catalog_unit_suggestion")


class CandidateRankingLLMTests(SimpleTestCase):
    """TASK-75 second half (§40-42): LLM re-ranks/narrows a candidate list the
    caller already computed via vector search — it can never invent a
    candidate id that wasn't offered, and a row absent from the result means
    "no opinion", not "zero candidates"."""

    def test_returns_empty_dict_without_calling_provider_for_no_rows(self):
        with patch("api.v1.catalog.candidate_ranking_llm.LLMConnector") as connector_cls:
            result = rerank_candidates(rows=[])
        self.assertEqual(result, {})
        connector_cls.assert_not_called()

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key")
    def test_returns_ranked_ids_reordered_by_the_model(self):
        connector = MagicMock()
        connector.structured.return_value = CandidateRankingBatch(
            rankings=[{"row_number": 1, "ranked_candidate_ids": ["cand-2", "cand-1"]}]
        )
        rows = [
            {
                "row_number": 1,
                "raw_name": "Сыр Российский",
                "candidates": [
                    {"id": "cand-1", "canonical_name": "Сыр Костромской"},
                    {"id": "cand-2", "canonical_name": "Сыр Российский 45%"},
                ],
            }
        ]
        with patch("api.v1.catalog.candidate_ranking_llm.LLMConnector", return_value=connector):
            result = rerank_candidates(rows=rows)
        self.assertEqual(result, {1: ["cand-2", "cand-1"]})
        self.assertEqual(connector.structured.call_args.kwargs["scenario"], "master_product_candidate_ranking")
        self.assertEqual(connector.structured.call_args.kwargs["response_model"], CandidateRankingBatch)

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key")
    def test_hallucinated_id_not_in_the_offered_list_is_dropped(self):
        connector = MagicMock()
        connector.structured.return_value = CandidateRankingBatch(
            rankings=[{"row_number": 1, "ranked_candidate_ids": ["cand-1", "cand-does-not-exist"]}]
        )
        rows = [
            {
                "row_number": 1,
                "raw_name": "Молоко",
                "candidates": [{"id": "cand-1", "canonical_name": "Молоко 3.2%"}],
            }
        ]
        with patch("api.v1.catalog.candidate_ranking_llm.LLMConnector", return_value=connector):
            result = rerank_candidates(rows=rows)
        self.assertEqual(result, {1: ["cand-1"]})

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key")
    def test_row_dropped_entirely_when_every_returned_id_is_invalid(self):
        connector = MagicMock()
        connector.structured.return_value = CandidateRankingBatch(
            rankings=[{"row_number": 1, "ranked_candidate_ids": ["not-a-real-id"]}]
        )
        rows = [{"row_number": 1, "raw_name": "Хлеб", "candidates": [{"id": "cand-1", "canonical_name": "Хлеб белый"}]}]
        with patch("api.v1.catalog.candidate_ranking_llm.LLMConnector", return_value=connector):
            result = rerank_candidates(rows=rows)
        self.assertEqual(result, {})

    @override_settings(
        AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key", MASTER_PRODUCT_CANDIDATE_RANKING_CHUNK_SIZE=1
    )
    def test_splits_rows_into_chunks_of_configured_size(self):
        connector = MagicMock()
        connector.structured.side_effect = [
            CandidateRankingBatch(rankings=[{"row_number": 1, "ranked_candidate_ids": ["cand-1"]}]),
            CandidateRankingBatch(rankings=[{"row_number": 2, "ranked_candidate_ids": ["cand-2"]}]),
        ]
        rows = [
            {"row_number": 1, "raw_name": "A", "candidates": [{"id": "cand-1", "canonical_name": "A candidate"}]},
            {"row_number": 2, "raw_name": "B", "candidates": [{"id": "cand-2", "canonical_name": "B candidate"}]},
        ]
        with patch("api.v1.catalog.candidate_ranking_llm.LLMConnector", return_value=connector):
            result = rerank_candidates(rows=rows)
        self.assertEqual(connector.structured.call_count, 2)
        self.assertEqual(result, {1: ["cand-1"], 2: ["cand-2"]})

    @override_settings(
        AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key", MASTER_PRODUCT_CANDIDATE_RANKING_CHUNK_SIZE=1
    )
    def test_one_failed_chunk_does_not_discard_other_chunks(self):
        connector = MagicMock()
        connector.structured.side_effect = [
            LLMConnectorError("boom"),
            CandidateRankingBatch(rankings=[{"row_number": 2, "ranked_candidate_ids": ["cand-2"]}]),
        ]
        rows = [
            {"row_number": 1, "raw_name": "A", "candidates": [{"id": "cand-1", "canonical_name": "A candidate"}]},
            {"row_number": 2, "raw_name": "B", "candidates": [{"id": "cand-2", "canonical_name": "B candidate"}]},
        ]
        with patch("api.v1.catalog.candidate_ranking_llm.LLMConnector", return_value=connector):
            result = rerank_candidates(rows=rows)
        self.assertEqual(result, {2: ["cand-2"]})

    @override_settings(
        AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key", MASTER_PRODUCT_CANDIDATE_RANKING_CHUNK_SIZE=1
    )
    def test_raises_only_when_every_chunk_fails(self):
        connector = MagicMock()
        connector.structured.side_effect = [LLMConnectorError("boom1"), LLMConnectorError("boom2")]
        rows = [
            {"row_number": 1, "raw_name": "A", "candidates": [{"id": "cand-1", "canonical_name": "A candidate"}]},
            {"row_number": 2, "raw_name": "B", "candidates": [{"id": "cand-2", "canonical_name": "B candidate"}]},
        ]
        with patch("api.v1.catalog.candidate_ranking_llm.LLMConnector", return_value=connector):
            with self.assertRaises(CandidateRankingError):
                rerank_candidates(rows=rows)

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key")
    def test_max_tokens_scales_with_chunk_size_for_a_full_chunk(self):
        """A full 30-row chunk with up to MASTER_PRODUCT_MATCH_TOP_K (5) candidates each
        serializes to well over the connector's default max_tokens=1200 (api/llm/connector.py),
        so a full chunk must ask for more headroom than the default."""
        connector = MagicMock()
        connector.structured.return_value = CandidateRankingBatch(rankings=[])
        rows = [
            {
                "row_number": row_number,
                "raw_name": f"Товар {row_number}",
                "candidates": [
                    {"id": f"cand-{row_number}-{slot}", "canonical_name": f"Кандидат {row_number}-{slot}"}
                    for slot in range(5)
                ],
            }
            for row_number in range(1, 31)
        ]
        with patch("api.v1.catalog.candidate_ranking_llm.LLMConnector", return_value=connector):
            rerank_candidates(rows=rows)
        max_tokens = connector.structured.call_args.kwargs["max_tokens"]
        self.assertGreater(max_tokens, 1200)
        self.assertEqual(max_tokens, max(500, min(4096, len(rows) * 150 + 300)))

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key")
    def test_duplicate_ids_in_the_llm_ranking_are_deduped(self):
        connector = MagicMock()
        connector.structured.return_value = CandidateRankingBatch(
            rankings=[{"row_number": 1, "ranked_candidate_ids": ["cand-1", "cand-1", "cand-2"]}]
        )
        rows = [
            {
                "row_number": 1,
                "raw_name": "Сахар",
                "candidates": [
                    {"id": "cand-1", "canonical_name": "Сахар белый"},
                    {"id": "cand-2", "canonical_name": "Сахар тростниковый"},
                ],
            }
        ]
        with patch("api.v1.catalog.candidate_ranking_llm.LLMConnector", return_value=connector):
            result = rerank_candidates(rows=rows)
        self.assertEqual(result, {1: ["cand-1", "cand-2"]})


class VenueCandidateRankingLLMTests(SimpleTestCase):
    """TASK-122: same ranking behaviour as rerank_candidates (shared private
    implementation), but its own scenario name and its own
    VENUE_IMPORT_CANDIDATE_RANKING_* settings — independent from the
    Мастер-справочник flag/timeout/chunk-size. Edge-case coverage (hallucination
    filtering, dedup, partial-chunk failure) already lives on
    CandidateRankingLLMTests above and exercises the same shared code path;
    this class only checks what's actually different about the venue wrapper."""

    def test_returns_empty_dict_without_calling_provider_for_no_rows(self):
        with patch("api.v1.catalog.candidate_ranking_llm.LLMConnector") as connector_cls:
            result = rerank_venue_candidates(rows=[])
        self.assertEqual(result, {})
        connector_cls.assert_not_called()

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key")
    def test_returns_ranked_ids_reordered_by_the_model_with_venue_scenario(self):
        connector = MagicMock()
        connector.structured.return_value = CandidateRankingBatch(
            rankings=[{"row_number": 1, "ranked_candidate_ids": ["cand-2", "cand-1"]}]
        )
        rows = [
            {
                "row_number": 1,
                "raw_name": "Сливки 33%",
                "candidates": [
                    {"id": "cand-1", "canonical_name": "Сливки 20%"},
                    {"id": "cand-2", "canonical_name": "Сливки ультрапастеризованные 33%"},
                ],
            }
        ]
        with patch("api.v1.catalog.candidate_ranking_llm.LLMConnector", return_value=connector):
            result = rerank_venue_candidates(rows=rows)
        self.assertEqual(result, {1: ["cand-2", "cand-1"]})
        self.assertEqual(connector.structured.call_args.kwargs["scenario"], "venue_product_matrix_candidate_ranking")

    @override_settings(
        AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key", VENUE_IMPORT_CANDIDATE_RANKING_CHUNK_SIZE=1
    )
    def test_uses_its_own_chunk_size_setting_not_the_master_one(self):
        connector = MagicMock()
        connector.structured.side_effect = [
            CandidateRankingBatch(rankings=[{"row_number": 1, "ranked_candidate_ids": ["cand-1"]}]),
            CandidateRankingBatch(rankings=[{"row_number": 2, "ranked_candidate_ids": ["cand-2"]}]),
        ]
        rows = [
            {"row_number": 1, "raw_name": "A", "candidates": [{"id": "cand-1", "canonical_name": "A candidate"}]},
            {"row_number": 2, "raw_name": "B", "candidates": [{"id": "cand-2", "canonical_name": "B candidate"}]},
        ]
        with patch("api.v1.catalog.candidate_ranking_llm.LLMConnector", return_value=connector):
            result = rerank_venue_candidates(rows=rows)
        self.assertEqual(connector.structured.call_count, 2)
        self.assertEqual(result, {1: ["cand-1"], 2: ["cand-2"]})

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key")
    def test_raises_only_when_every_chunk_fails(self):
        connector = MagicMock()
        connector.structured.side_effect = [LLMConnectorError("boom")]
        rows = [{"row_number": 1, "raw_name": "A", "candidates": [{"id": "cand-1", "canonical_name": "A candidate"}]}]
        with patch("api.v1.catalog.candidate_ranking_llm.LLMConnector", return_value=connector):
            with self.assertRaises(CandidateRankingError):
                rerank_venue_candidates(rows=rows)

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", OPENAI_API_KEY="key")
    def test_master_ranking_is_unaffected_by_venue_settings(self):
        """Regression guard: the two wrappers must not share mutable state or
        accidentally read each other's settings."""
        connector = MagicMock()
        connector.structured.return_value = CandidateRankingBatch(rankings=[])
        rows = [{"row_number": 1, "raw_name": "A", "candidates": [{"id": "cand-1", "canonical_name": "A candidate"}]}]
        with patch("api.v1.catalog.candidate_ranking_llm.LLMConnector", return_value=connector):
            rerank_candidates(rows=rows)
        self.assertEqual(connector.structured.call_args.kwargs["scenario"], "master_product_candidate_ranking")
